import fcntl
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tarfile
from datetime import timedelta
from types import SimpleNamespace

import pytest

import bot
from deploy import update as deploy


@pytest.fixture
def host(tmp_path, monkeypatch):
    base = tmp_path / "host"
    initial = base / "app"
    initial.mkdir(parents=True)
    (initial / "REVISION").write_text("old\n")
    app = base / "current"
    app.symlink_to(initial, target_is_directory=True)
    (base / "DEPLOYED_REVISION").write_text("old\n")
    releases = base / "releases"
    releases.mkdir()
    journal = tmp_path / "state" / "us-live.sqlite"
    journal.parent.mkdir()
    journal.with_suffix(".lock").touch()
    with sqlite3.connect(journal) as database:
        database.execute("CREATE TABLE trades (slug TEXT PRIMARY KEY, data TEXT NOT NULL)")
    for name, value in {"BASE": base, "APP": app, "RELEASES": releases, "JOURNAL": journal}.items():
        monkeypatch.setattr(deploy, name, value)

    result = SimpleNamespace(
        base=base, app=app, initial=initial, journal=journal, calls=[], state="active"
    )
    monkeypatch.setattr(deploy, "main_sha", lambda: "a" * 40)
    monkeypatch.setattr(deploy, "ci_passed", lambda _: True)
    monkeypatch.setattr(deploy, "quiet_window", lambda: True)
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)

    def run(command, **kwargs):
        result.calls.append(command)
        output = ""
        if command[:2] == ["systemctl", "show"]:
            output = result.state
        elif command[:2] == ["systemctl", "stop"]:
            result.state = "inactive"
        elif command[:2] == ["systemctl", "start"]:
            result.state = "active"
        elif command[0] == "runuser":
            output = '{"flat": true}'
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(deploy, "run", run)

    def prepare(sha):
        path = releases / (sha + "-prepared")
        path.mkdir()
        (path / "REVISION").write_text(sha + "\n")
        (path / "logs").symlink_to(journal.parent, target_is_directory=True)
        return path

    result.prepare = prepare
    monkeypatch.setattr(deploy, "prepare", prepare)
    return result


def test_successful_release_keeps_state_and_backs_up_journal(host):
    deploy.update()
    assert host.app.is_symlink()
    assert (host.app / "REVISION").read_text().strip() == "a" * 40
    assert (host.app / "logs").resolve() == host.journal.parent
    assert host.state == "active"
    assert len(list((host.base / "backups").glob("*.sqlite"))) == 1
    assert [command[1] for command in host.calls if command[0] == "systemctl"] == [
        "show",
        "stop",
        "show",
        "start",
        "show",
    ]


def test_ci_failure_leaves_current_process_untouched(host, monkeypatch):
    monkeypatch.setattr(deploy, "ci_passed", lambda _: False)
    deploy.update()
    assert host.calls == []
    assert host.app.resolve() == host.initial


def test_closed_entry_window_does_not_stop_current_process(host, monkeypatch):
    monkeypatch.setattr(deploy, "quiet_window", lambda: False)
    deploy.update()
    assert host.calls == []


@pytest.mark.parametrize(
    "seconds,quiet",
    [
        (60, False),
        (100, False),
        (105, False),
        (119.999, False),
        (120, True),
        (239.999, True),
        (240, False),
        (300, False),
        (400, False),
        (420, True),
        (539.999, True),
        (540, False),
        (700, False),
        (720, False),
        (840, False),
        (870, False),
    ],
)
def test_update_quiet_window_respects_both_horizons(clock, monkeypatch, seconds, quiet):
    clock.current = clock.current.replace(minute=30, second=0, microsecond=0) + timedelta(
        seconds=seconds
    )
    monkeypatch.setattr(deploy, "datetime", clock)
    assert deploy.quiet_window() is quiet


def test_preparation_failure_does_not_stop_current_process(host, monkeypatch):
    def fail(_):
        raise RuntimeError("dependency build failed")

    monkeypatch.setattr(deploy, "prepare", fail)
    with pytest.raises(RuntimeError, match="dependency build failed"):
        deploy.update()
    assert not any(command[:2] == ["systemctl", "stop"] for command in host.calls)
    assert host.state == "active"


def test_newer_main_during_preparation_does_not_stop_or_install_stale_release(host, monkeypatch):
    heads = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr(deploy, "main_sha", lambda: next(heads))
    deploy.update()
    assert host.state == "active"
    assert host.app.resolve() == host.initial
    assert not list(deploy.RELEASES.iterdir())


def test_inactive_service_is_updated_without_starting_it(host):
    host.state = "inactive"
    deploy.update()
    assert host.app.is_symlink()
    assert host.state == "inactive"
    assert not any(
        command[1] in {"start", "stop"} for command in host.calls if command[0] == "systemctl"
    )


def test_failed_service_is_not_revived(host):
    host.state = "failed"
    deploy.update()
    assert host.app.resolve() == host.initial
    assert host.state == "failed"


def test_order_reserved_between_preflight_and_stop_blocks_switch_and_restart(host, monkeypatch):
    original = deploy.run

    def run(command, **kwargs):
        value = original(command, **kwargs)
        if command[:2] == ["systemctl", "stop"]:
            with sqlite3.connect(host.journal) as database:
                row = {
                    "state": "reserved",
                    "quantity": "0",
                    "pending": {"request": {}},
                    "order_id": None,
                }
                database.execute("INSERT INTO trades VALUES (?, ?)", ("in-flight", json.dumps(row)))
        return value

    monkeypatch.setattr(deploy, "run", run)
    with pytest.raises(RuntimeError, match="Post-stop gate failed"):
        deploy.update()
    assert host.app.resolve() == host.initial
    assert host.state == "inactive"
    assert not any(command[:2] == ["systemctl", "start"] for command in host.calls)


def test_process_still_holding_lifetime_lock_blocks_deployment(host):
    with host.journal.with_suffix(".lock").open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            deploy.update()
    assert host.app.resolve() == host.initial
    assert host.state == "inactive"


def test_account_read_error_or_open_position_blocks_stop(host, monkeypatch):
    original = deploy.run

    def run(command, **kwargs):
        if command[0] == "runuser":
            return subprocess.CompletedProcess(command, 0, '{"flat": false}', "")
        return original(command, **kwargs)

    monkeypatch.setattr(deploy, "run", run)
    deploy.update()
    assert host.state == "active"
    assert host.app.resolve() == host.initial


def test_missing_journal_is_not_silently_created(host):
    host.journal.unlink()
    with pytest.raises(sqlite3.OperationalError):
        deploy.update()
    assert not host.journal.exists()
    assert host.state == "active"


def test_backup_includes_uncheckpointed_wal_rows(host):
    connection = sqlite3.connect(host.journal)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO trades VALUES ('closed', '{}')")
        connection.commit()
        deploy.backup_journal("a" * 40)
        path = next((host.base / "backups").glob("*.sqlite"))
        with sqlite3.connect(path) as backup:
            assert backup.execute("SELECT slug FROM trades").fetchall() == [("closed",)]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("event", "pull_request"),
        ("head_sha", "b" * 40),
        ("head_branch", "other"),
        ("conclusion", "failure"),
        ("status", "in_progress"),
        ("head_repository", {"full_name": "attacker/fork"}),
    ],
)
def test_ci_only_accepts_own_completed_successful_main_push(field, value, monkeypatch):
    run = {
        "head_sha": "a" * 40,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_repository": {"full_name": deploy.REPO},
    }
    monkeypatch.setattr(deploy, "fetch", lambda _: json.dumps({"workflow_runs": [run]}).encode())
    assert deploy.ci_passed("a" * 40)
    run[field] = value
    assert not deploy.ci_passed("a" * 40)


@pytest.mark.parametrize(
    "name,kind",
    [
        ("root/../../etc/passwd", tarfile.REGTYPE),
        ("root/.env.us", tarfile.REGTYPE),
        ("root/logs/us-live.sqlite", tarfile.REGTYPE),
        ("root/.venv/bin/python", tarfile.REGTYPE),
        ("root/.source.tar.gz", tarfile.REGTYPE),
        ("root/link", tarfile.SYMTYPE),
        ("root/hardlink", tarfile.LNKTYPE),
    ],
)
def test_unsafe_source_archive_rejected_before_extraction(tmp_path, name, kind):
    path = tmp_path / "source.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = "/etc/passwd" if kind != tarfile.REGTYPE else ""
        archive.addfile(info, io.BytesIO())
    with pytest.raises(ValueError):
        deploy.validate_archive(path)


def test_check_mode_does_not_build_stop_or_change_files(host):
    deploy.update(check=True)
    assert host.app.resolve() == host.initial
    assert not list(deploy.RELEASES.iterdir())
    assert not any(command[0] == "systemctl" for command in host.calls)


def test_frozen_release_is_readable_but_not_build_owned_and_symlinks_are_not_followed(
    tmp_path, monkeypatch
):
    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    package = release / "package"
    package.mkdir(mode=0o700)
    data = package / "module.py"
    data.write_text("pass\n")
    data.chmod(0o600)
    executable = release / "python"
    executable.touch(mode=0o700)
    outside = tmp_path / "system-python"
    outside.touch(mode=0o700)
    (release / "python-link").symlink_to(outside)
    chowns = []
    monkeypatch.setattr(os, "chown", lambda *args, **kwargs: chowns.append((args, kwargs)))

    deploy.freeze_release(release)

    assert stat.S_IMODE(release.stat().st_mode) == 0o755
    assert stat.S_IMODE(package.stat().st_mode) == 0o755
    assert stat.S_IMODE(data.stat().st_mode) == 0o644
    assert stat.S_IMODE(executable.stat().st_mode) == 0o755
    assert stat.S_IMODE(outside.stat().st_mode) == 0o700
    assert all(
        args[1:] == (0, 0) and kwargs == {"follow_symlinks": False} for args, kwargs in chowns
    )
    assert outside not in [args[0] for args, _ in chowns]


@pytest.mark.parametrize("signum", [bot.signal.SIGINT, bot.signal.SIGTERM])
def test_shutdown_signal_finishes_tick_before_closing_journal(signum, tmp_path, monkeypatch):
    handlers = {}
    phases = []
    exchange = SimpleNamespace(
        account=lambda: SimpleNamespace(positions={}, orders=[]),
        market_stream=lambda: SimpleNamespace(start=lambda: None, close=lambda: None),
        close=lambda: phases.append("closed"),
    )
    monkeypatch.setattr(bot.PolymarketClient, "connect", lambda _: exchange)
    monkeypatch.setattr(bot, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["bot.py"])

    def install(number, handler):
        handlers[number] = handler
        return bot.signal.SIG_DFL

    monkeypatch.setattr(bot.signal, "signal", install)

    class Trader:
        finished = False

        def __init__(self, *args, **kwargs):
            pass

        def tick(self):
            phases.append("submit-started")
            handlers[signum](signum, None)
            phases.append("accounting-finished")

    monkeypatch.setattr(bot, "TradingBot", Trader)
    assert bot.main() == 0
    assert phases == ["submit-started", "accounting-finished", "closed"]


def test_selected_model_override_loads_paired_artifact_in_a_distinct_continuous_cohort(
    venue, journal, books, tmp_path
):
    import shlex
    from pathlib import Path

    from test_model_bot import saved_model

    from bot import TradingBot
    from exit_candidate import ExitCandidate
    from probability_model import load_model

    root = Path(__file__).resolve().parents[1]
    commands = [
        line.partition("=")[2]
        for line in (root / "deploy/exit-aware-model.conf").read_text().splitlines()
        if line.startswith("ExecStart=")
    ]
    assert commands[0] == ""
    args = shlex.split(commands[1])
    model_path = root / Path(args[args.index("--model") + 1]).relative_to(
        "/opt/polymarket-us/current"
    )
    model = load_model(model_path)
    assert isinstance(model, ExitCandidate)
    assert "--live" in args and "--simultaneous" in args
    assert "--duration" not in args and "--one-trade" not in args and "--no-kelly" not in args
    old = TradingBot(
        venue.client,
        journal,
        books,
        model=saved_model(tmp_path),
        experiment_id="btc-five-20261022-a0201afd",
        simultaneous=True,
    )
    identifier = args[args.index("--experiment-id") + 1]
    selected = TradingBot(
        venue.client, journal, books, model=model, experiment_id=identifier, simultaneous=True
    )
    assert selected.experiment["id"] != old.experiment["id"]
    assert selected._model_settings["entry_bankroll_fraction"] == "0.04"
    assert selected._model_settings["kelly_multiplier"] == "0.25"
    assert selected._model_settings["exit_policy"] == "model-loss-15-v1"
    assert selected._model_settings["entry_filter"] == "expected-net-proceeds-positive-v1"
    assert journal.saved_experiment(old.experiment["id"]) == old.experiment
    assert journal.saved_experiment(selected.experiment["id"]) == selected.experiment
