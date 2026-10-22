"""Pull a passing main release on this Droplet; no GitHub credential is needed."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report readiness without updating")
    args = parser.parse_args()
    try:
        with (BASE / "update.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("Deferred: another deployment is running", flush=True)
                return 0
            update(check=args.check)
        return 0
    except Exception as error:
        # Account errors are sanitized by the unprivileged account probe.
        print(f"Deployment stopped: {type(error).__name__}: {error}", flush=True)
        return 1


def update(*, check: bool = False) -> None:
    sha = main_sha()
    current = (BASE / "DEPLOYED_REVISION").read_text().strip()
    if sha == current:
        print(f"Up to date: {sha}", flush=True)
        return
    if not ci_passed(sha):
        print(f"Deferred: main {sha} has no successful push CI", flush=True)
        return
    if not ready():
        print("Deferred: wait for a flat account/journal and a quiet trading window", flush=True)
        return
    if check:
        print(f"Ready to deploy {sha}; check made no changes", flush=True)
        return

    release = prepare(sha)
    # Downloads/builds may outlast the initial gate or be overtaken by a new push.
    if main_sha() != sha or not ready():
        print("Deferred after preparation: main or trading state changed", flush=True)
        shutil.rmtree(release)
        return
    state = service_state()
    if state not in {"active", "inactive"}:
        print(f"Deferred: service is {state}; preserve its existing stop", flush=True)
        shutil.rmtree(release)
        return
    if state == "active":
        run(["systemctl", "stop", "polymarket-us.service"], timeout=200)
    if service_state() != "inactive":
        raise RuntimeError("Service did not stop; release was not switched")

    # This is the same lifetime lock used by the bot, not a separate deploy lock.
    # Never restart through a reservation left by an interrupted/ambiguous create.
    with JOURNAL.with_suffix(".lock").open("r+") as journal_lock:
        fcntl.flock(journal_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if main_sha() != sha or not ready():
            raise RuntimeError(
                "Post-stop gate failed; old release retained and service left stopped"
            )
        backup_journal(sha)
        if not APP.is_symlink():
            raise RuntimeError("Current release must be a symlink; release was not switched")
        pointer = BASE / "app.next"
        pointer.unlink(missing_ok=True)
        pointer.symlink_to(release, target_is_directory=True)
        pointer.replace(APP)
        revision = BASE / "DEPLOYED_REVISION.next"
        revision.write_text(sha + "\n")
        revision.replace(BASE / "DEPLOYED_REVISION")

    # Preserve an operator's inactive state. Never revive a failed service.
    if state == "active":
        run(["systemctl", "start", "polymarket-us.service"])
        time.sleep(10)
        if service_state() != "active":
            raise RuntimeError(
                "New release stopped; inspect logs before restarting or rolling back"
            )
    prune_releases(release)
    print(
        f"Deployed {sha}; service {'running' if state == 'active' else 'left inactive'}", flush=True
    )


def main_sha() -> str:
    output = run(
        ["git", "-c", "credential.helper=", "ls-remote", "--exit-code", REPO_URL, "refs/heads/main"]
    ).stdout.strip()
    sha, ref = output.split()
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or ref != "refs/heads/main":
        raise ValueError("Unexpected main reference")
    return sha


def ci_passed(sha: str) -> bool:
    query = urlencode({"branch": "main", "event": "push", "head_sha": sha, "per_page": 10})
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/ci.yml/runs?{query}"
    try:
        data = json.loads(fetch(url))
    except HTTPError as error:
        if error.code == 404:
            return False
        raise
    return any(
        row["head_sha"] == sha
        and row["head_branch"] == "main"
        and row["event"] == "push"
        and row["status"] == "completed"
        and row["conclusion"] == "success"
        and row["head_repository"]["full_name"] == REPO
        for row in data["workflow_runs"]
    )


def ready() -> bool:
    # The baseline admits entries only during minutes 12-14:30. Allow a
    # generous margin for shutdown, account reads, and the new process's startup.
    if not quiet_window() or not journal_flat():
        return False
    probe = run(
        [
            "runuser",
            "-u",
            "polymarket-us",
            "--",
            str(APP / ".venv/bin/python"),
            "-c",
            ACCOUNT_PROBE,
            str(CREDENTIALS),
        ],
        cwd=APP,
    )
    return json.loads(probe.stdout) == {"flat": True} and quiet_window()


def quiet_window() -> bool:
    offset = datetime.now(timezone.utc).timestamp() % 900
    return 60 <= offset < 660


def journal_flat() -> bool:
    # Missing/corrupt state is not an empty journal. Read WAL state without edits.
    with sqlite3.connect(JOURNAL.as_uri() + "?mode=ro", uri=True) as database:
        rows = [json.loads(row[0]) for row in database.execute("SELECT data FROM trades")]
    return all(
        row["state"] in {"closed", "unfilled"}
        and Decimal(row["quantity"]) == 0
        and row["pending"] is None
        and row["order_id"] is None
        for row in rows
    )


def prepare(sha: str) -> Path:
    # Only this function handles release contents. Dependency code runs as a
    # non-login build user with no access to account credentials or journal.
    release = Path(tempfile.mkdtemp(prefix=sha + "-", dir=RELEASES))
    try:
        user = pwd.getpwnam("polymarket-build")
        release.chmod(0o755)
        os.chown(release, user.pw_uid, user.pw_gid)
        archive = release / ".source.tar.gz"
        archive.write_bytes(fetch(f"https://codeload.github.com/{REPO}/tar.gz/{sha}"))
        archive.chmod(0o644)  # Public source only; no credentials.
        validate_archive(archive)
        build(release)
        # These names were excluded from the archive. Refuse unexpected build
        # output instead of following a package-created symlink as root.
        (release / "logs").symlink_to(JOURNAL.parent, target_is_directory=True)
        with os.fdopen(
            os.open(release / "REVISION", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644), "w"
        ) as revision:
            revision.write(sha + "\n")
        freeze_release(release)
        return release
    except BaseException:
        shutil.rmtree(release)
        raise


def validate_archive(path: Path) -> None:
    with tarfile.open(path) as archive:
        prefix = None
        size = 0
        for count, member in enumerate(archive, 1):
            size += member.size
            if count > 2000 or size > MAX_BYTES:
                raise ValueError("Expanded release exceeds size limit")
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts:
                raise ValueError("Unsafe archive path")
            prefix = prefix or name.parts[0]
            if name.parts[0] != prefix or not (member.isfile() or member.isdir()):
                raise ValueError("Unsupported archive member")
            if any(
                part in {".git", ".venv", "logs", "REVISION", ".source.tar.gz"}
                or part.startswith(".env")
                for part in name.parts[1:]
            ):
                raise ValueError("Release contains reserved runtime data")


def backup_journal(sha: str) -> None:
    destination = BASE / "backups"
    destination.mkdir(mode=0o700, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"-{sha}.sqlite"
    with sqlite3.connect(JOURNAL.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(destination / name) as backup:
            source.backup(backup)
    (destination / name).chmod(0o600)


def prune_releases(current: Path) -> None:
    releases = sorted(RELEASES.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True)
    others = [path for path in releases if path != current]
    for path in others[2:]:
        shutil.rmtree(path)
    backups = sorted((BASE / "backups").glob("*.sqlite"), reverse=True)
    for path in backups[3:]:
        path.unlink()


def service_state() -> str:
    return run(
        ["systemctl", "show", "polymarket-us.service", "-p", "ActiveState", "--value"]
    ).stdout.strip()


def build(release: Path) -> None:
    # One transient unit contains package build subprocesses. systemd reaps its
    # whole control group before we freeze/publish any of its output as root.
    run(
        [
            "systemd-run",
            "--quiet",
            "--wait",
            "--pipe",
            "--collect",
            "--unit=polymarket-build",
            "--uid=polymarket-build",
            "--gid=polymarket-build",
            f"--working-directory={release}",
            "--property=UMask=0022",
            "--property=KillMode=control-group",
            "--property=RuntimeMaxSec=600",
            "--property=NoNewPrivileges=yes",
            "--property=ProtectSystem=strict",
            "--property=ProtectHome=yes",
            "--property=PrivateTmp=yes",
            f"--property=ReadWritePaths={release}",
            "/bin/sh",
            "-ec",
            BUILD_SCRIPT,
        ],
        timeout=660,
    )


def freeze_release(release: Path) -> None:
    # Runtime can read the release; neither the runtime nor a later build can
    # rewrite it. Never follow venv symlinks when changing ownership or mode.
    for directory, directories, files in os.walk(release, followlinks=False):
        for path in [Path(directory), *(Path(directory) / name for name in directories + files)]:
            os.chown(path, 0, 0, follow_symlinks=False)
            if path.is_symlink():
                continue
            mode = path.stat().st_mode
            if stat.S_ISDIR(mode):
                path.chmod(0o755)
            elif stat.S_ISREG(mode):
                path.chmod(0o755 if mode & 0o111 else 0o644)
            else:
                raise ValueError("Unsupported build output")


def run(
    command: list[str], *, cwd: Path | None = None, timeout: int = 45
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, cwd=cwd, timeout=timeout, check=True, capture_output=True, text=True
    )


def fetch(url: str) -> bytes:
    request = Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": "polymarket-us-deploy"}
    )
    with urlopen(request, timeout=30) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Release response exceeds size limit")
    return data


REPO = "AryanJ-NYC/Polymarket-BTC-15-Minute-Trading-Bot"
REPO_URL = f"https://github.com/{REPO}.git"
BASE = Path("/opt/polymarket-us")
APP = BASE / "current"
RELEASES = BASE / "releases"
JOURNAL = Path("/var/lib/polymarket-us/us-live.sqlite")
CREDENTIALS = Path("/etc/polymarket-us/.env.us")
MAX_BYTES = 8 * 1024 * 1024
BUILD_SCRIPT = """
tar -xzf .source.tar.gz --strip-components=1 --no-same-owner --no-same-permissions
rm .source.tar.gz
python3 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt
.venv/bin/python -m pip check
"""
ACCOUNT_PROBE = """
import json, sys
from pathlib import Path
from execution.polymarket_client import PolymarketClient, position_quantity
client = None
try:
    client = PolymarketClient.connect(Path(sys.argv[1]))
    account = client.account()
    print(json.dumps({'flat': not account.orders and not any(position_quantity(p) != 0 for p in account.positions.values())}))
except Exception:
    print(json.dumps({'flat': False}))
finally:
    if client is not None:
        client.close()
"""


if __name__ == "__main__":
    raise SystemExit(main())
