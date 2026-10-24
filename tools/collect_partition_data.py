"""Continuously archive one fixed-cutoff hourly BTC Range/Above/Above partition.

Independent output/process; no orders, account reads, strategy or reference feeds.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import signal
import time
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from tools import collect_research_data as base


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(collect(args.env_file, args.output))
    except Exception as exc:
        print(json.dumps({"partition_collector_stopped": type(exc).__name__}), flush=True)
        return 1
    return 0


async def collect(env_file: Path, output: Path) -> None:
    archive = base.Archive(output)
    exchange = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        exchange = base.PolymarketClient.connect(env_file)
        exchange.client._http.event_hooks["request"].append(base.require_get)
        archive.emit(
            "collector_started",
            source="hourly_partition_collector",
            code_sha256=base.file_hash(Path(__file__)),
            shared_collector_sha256=base.file_hash(Path(base.__file__)),
            selection_policy="lower_median_interior_at_expiry_minus_1800",
            book_market_limit=3,
            subscription_types=[base.BOOK_SUBSCRIPTION, base.TRADE_SUBSCRIPTION],
            true_brti_available=False,
        )
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as public:
            collector = PartitionCollector(public, exchange.client.ws.markets, archive, stop)
            tasks = [
                asyncio.create_task(collector.metadata_loop()),
                asyncio.create_task(collector.book_loop()),
                asyncio.create_task(collector.monitor_loop()),
                asyncio.create_task(stop.wait()),
            ]
            try:
                finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in finished:
                    task.result()
            finally:
                stop.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        stop.set()
        try:
            try:
                if exchange is not None:
                    exchange.close()
            finally:
                try:
                    archive.emit("collector_stopped", source="hourly_partition_collector")
                finally:
                    archive.close()
        finally:
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(signum)


class PartitionCollector(base.Collector):
    def __init__(self, public, socket_factory, archive, stop):
        super().__init__(public, socket_factory, archive, stop)
        self.state_path = archive.directory / "partition-state.json"
        self.state = (
            json.loads(self.state_path.read_text())
            if self.state_path.exists()
            else {"schema_version": 1, "last_cutoff": None, "selection": None, "tracked": {}}
        )
        validate_state(self.state)
        self.cache = None
        self.series = {}
        self.next_discovery = self.next_refresh = 0
        self.last_discovery = None
        self.last_selection_error = None
        selected = self.state["selection"]
        # A durable selected identity must survive a lost pending-cache write.
        for slug in self.state["tracked"]:
            self.pending.setdefault(slug, base.utc())
        if set(self.pending) - set(self.state["tracked"]):
            raise ValueError("pending_identity_missing_from_checkpoint")
        if selected:
            receipt = self.archive.emit("partition_selection_resumed", source=SOURCE, **selected)
            if receipt is None:
                raise SelectionArchiveError("selection_resume_archive_rejected")
            self.markets = {
                m["slug"]: m for m in selected["markets"] if m["slug"] in self.state["tracked"]
            }

    def desired_markets(self, now):
        selected = self.state["selection"]
        if selected is None or selected["expiry"] <= now:
            return set()
        return {
            slug
            for slug, market in self.markets.items()
            if base.stamp(market["assetPriceTerms"]["windowEnd"]) > now
            and market.get("status") != "MARKET_STATUS_RESOLVED"
        }

    async def metadata_loop(self):
        delay = 1
        while not self.stop.is_set():
            self.archive.check()
            try:
                await self.cycle()
                delay = 1
                await base.pause(self.stop, 0.5)
            except (OSError, SelectionArchiveError):
                raise  # A failed durable checkpoint must stop this process.
            except Exception as exc:
                self.archive.emit("source_error", source=SOURCE, error_class=type(exc).__name__)
                await base.pause(self.stop, delay)
                delay = min(60, delay * 2)

    async def cycle(self):
        now = time.time()
        expiry = (int(now) // 3600 + 1) * 3600
        cutoff = expiry - 1800
        if self.state["last_cutoff"] is not None and self.state["last_cutoff"] >= cutoff:
            expiry += 3600
            cutoff += 3600
        if now >= cutoff:
            await self.freeze_selection(expiry)
        elif cutoff - 60 <= now and now >= self.next_discovery:
            self.next_discovery = now + 15
            await self.discover(expiry)
            # Do not let a slow HTTP request postpone the causal cutoff decision
            # until a later refresh; its post-cutoff result never replaces cache.
            if time.time() >= cutoff:
                await self.freeze_selection(expiry)
        if time.time() >= self.next_refresh:
            self.next_refresh = time.time() + 15
            await self.refresh_selected()

    async def discover(self, expiry):
        if not self.series:
            body, _ = await self.get_evidence(
                "/v1/series",
                {"limit": 100, "params.seriesFilters.query": "bitcoin"},
                "discovery_response",
            )
            pairs = [(s["slug"], str(s["id"])) for s in body["series"] if s.get("slug") in SERIES]
            if len(pairs) != 2 or {s for s, _ in pairs} != set(SERIES):
                raise ValueError("hourly_series_not_unique")
            self.series = dict(pairs)
        events, proof = {}, []
        for slug in SERIES:
            found = []
            for offset in (0, 1):
                params = {
                    "limit": 1,
                    "offset": offset,
                    "seriesId": self.series[slug],
                    "endDateMin": iso(expiry - 1),
                    "endDateMax": iso(expiry + 1),
                }
                body, source = await self.get_evidence("/v1/events", params, "discovery_response")
                proof.append(source)
                rows = body["events"]
                if not isinstance(rows, list) or len(rows) > 1:
                    raise ValueError("unexpected_event_page")
                found.extend(rows)
                if not rows:
                    break
            if len(found) != 1 or rows:
                raise ValueError("incomplete_or_ambiguous_event_pagination")
            event = found[0]
            if event.get("seriesSlug") != slug or not exact_expiry(event.get("endDate"), expiry):
                raise ValueError("hourly_event_identity_mismatch")
            events[slug] = event
        complete_ns = max(row["received_ns"] for row in proof)
        self.last_discovery = iso_ns(complete_ns)
        if complete_ns <= (expiry - 1800) * SECOND:
            self.cache = {
                "expiry": expiry,
                "received_ns": complete_ns,
                "events": events,
                "proof": proof,
            }

    async def freeze_selection(self, expiry):
        cutoff = expiry - 1800
        if time.time_ns() < cutoff * SECOND:
            raise ValueError("selection_before_cutoff")
        if self.state["last_cutoff"] is not None and cutoff <= self.state["last_cutoff"]:
            return
        cache = self.cache
        result = None
        reason = "no_complete_pre_cutoff_discovery"
        if cache and cache["expiry"] == expiry and cache["received_ns"] <= cutoff * SECOND:
            try:
                result = select_partition(cache["events"], expiry)
                if len(self.state["tracked"]) + 3 > MAX_PENDING:
                    raise ValueError("pending_resolution_capacity")
                retained_ids = {row["id"] for row in self.state["tracked"].values()}
                if any(
                    m["slug"] in self.state["tracked"] or m["id"] in retained_ids
                    for m in result["markets"]
                ):
                    raise ValueError("retained_market_identity_collision")
            except ValueError as exc:
                reason = str(exc)
                result = None
        if result is not None:
            selection = {
                **result,
                "expiry": expiry,
                "cutoff": cutoff,
                "metadata_received_ns": cache["received_ns"],
                "source_proof": cache["proof"],
            }
            receipt = self.archive.emit("partition_selection_prepared", source=SOURCE, **selection)
            if receipt is None:
                raise SelectionArchiveError("selection_archive_rejected")
            await self.archive.flush(receipt)
            selection["preparation_source"] = {
                "session_id": self.archive.session,
                "sequence": receipt,
            }
            updated = deepcopy(self.state)
            updated["selection"] = selection
            for market in selection["markets"]:
                updated["tracked"][market["slug"]] = {"id": str(market["id"]), "expiry": expiry}
            updated["last_cutoff"] = cutoff
            durable_json(self.state_path, updated)
            self.state = updated
            committed = self.archive.emit("partition_selected", source=SOURCE, **selection)
            if committed is None:
                raise SelectionArchiveError("selection_commit_archive_rejected")
            await self.archive.flush(committed)
            await self.record_metadata({m["slug"]: m for m in selection["markets"]})
            self.markets = {m["slug"]: m for m in selection["markets"]}
            self.last_selection_error = None
        else:
            receipt = self.archive.emit(
                "partition_selection_missed",
                source=SOURCE,
                expiry=expiry,
                cutoff=cutoff,
                reason=reason,
            )
            if receipt is None:
                raise SelectionArchiveError("selection_archive_rejected")
            await self.archive.flush(receipt)
            updated = {**self.state, "last_cutoff": cutoff}
            durable_json(self.state_path, updated)
            self.state = updated
            self.last_selection_error = reason
        self.cache = None

    async def refresh_selected(self):
        current = sorted(self.desired_markets(time.time()))
        overdue = sorted(set(self.pending) - set(current), key=lambda slug: self.pending[slug])[:2]
        for slug in current + overdue:
            try:
                body, _ = await self.get_evidence(
                    f"/v1/market/slug/{slug}", {}, "metadata_response"
                )
                market = body["market"]
                if (
                    market.get("slug") != slug
                    or str(market.get("id")) != self.state["tracked"][slug]["id"]
                ):
                    raise ValueError("selected_market_identity_changed")
                await self.record_metadata({slug: market})
                # Retire identities only after both the resolution archive and
                # its retry cache are durable; a power loss cannot resurrect an
                # unknown pending identity after checkpoint pruning.
                durable_json(self.pending_path, self.pending)
                if slug in self.markets:
                    self.markets[slug] = market
                if slug not in self.pending:
                    updated = deepcopy(self.state)
                    updated["tracked"].pop(slug, None)
                    durable_json(self.state_path, updated)
                    self.state = updated
                self.last_metadata = base.utc()
            except OSError:
                raise
            except Exception as exc:
                self.archive.emit(
                    "source_error", source=SOURCE, slug=slug, error_class=type(exc).__name__
                )
            finally:
                if slug in self.pending:
                    self.pending[slug] = base.utc()
                base.atomic_json(self.pending_path, self.pending)
        self.fingerprints = {
            slug: value
            for slug, value in self.fingerprints.items()
            if slug in self.markets or slug in self.pending
        }

    async def get_evidence(self, path, params, kind):
        started = time.time_ns()
        request_params = {**params, "_ts": started}
        async with self.public.stream("GET", base.BASE + path, params=request_params) as response:
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE:
                    self.archive.emit(
                        "source_error",
                        source=SOURCE,
                        path=path,
                        error_class="ResponseSizeLimit",
                        request_started_ns=started,
                    )
                    raise ValueError("metadata_response_size_limit")
                chunks.append(chunk)
            raw = b"".join(chunks)
            received = time.time_ns()
            received_monotonic = time.monotonic_ns()
            try:
                raw_frame = {"raw_text": raw.decode("utf-8")}
            except UnicodeDecodeError:
                raw_frame = {"raw_bytes_base64": base64.b64encode(raw).decode("ascii")}
            receipt = self.archive.emit(
                kind,
                source=SOURCE,
                path=path,
                request_params=request_params,
                request_started_ns=started,
                received_ns=received,
                received_at=iso_ns(received),
                received_monotonic_ns=received_monotonic,
                http_status=response.status_code,
                headers={
                    k: response.headers[k]
                    for k in ("date", "content-type", "etag", "last-modified", "retry-after")
                    if k in response.headers
                },
                **raw_frame,
                body_sha256=hashlib.sha256(raw).hexdigest(),
            )
            if receipt is None:
                raise RuntimeError("metadata_archive_rejected")
            if response.status_code == 429:
                try:
                    retry = float(response.headers.get("retry-after", 30))
                except ValueError:
                    retry = 30
                await base.pause(self.stop, max(30, min(300, retry)))
            response.raise_for_status()
            payload = json.loads(raw, parse_float=str, parse_constant=base.reject_json_constant)
            return payload, {
                "session_id": self.archive.session,
                "sequence": receipt,
                "received_ns": received,
                "body_sha256": hashlib.sha256(raw).hexdigest(),
            }

    async def monitor_loop(self):
        while not self.stop.is_set():
            self.archive.check()
            active = self.desired_markets(time.time())
            selected = self.state["selection"]
            status = {
                "session_id": self.archive.session,
                "checked_at": base.utc(),
                "source": "hourly_partition_collector",
                "connected": self.connected,
                "generation": self.generation,
                "active_market_slugs": sorted(active),
                "active_market_limit": 3,
                "expected_subscription_count": len(active) * 2,
                "subscription_semantics": "SDK sent, not provider acknowledgement",
                "selection_expiry": iso(selected["expiry"]) if selected else None,
                "selection_cutoff": iso(selected["cutoff"]) if selected else None,
                "selection_metadata_received_at": iso_ns(selected["metadata_received_ns"])
                if selected
                else None,
                "last_processed_cutoff": self.state["last_cutoff"],
                "last_selection_error": self.last_selection_error,
                "last_discovery_received_at": self.last_discovery,
                "last_metadata_refresh_at": self.last_metadata,
                "last_book_received_at": {s: t for s, t in self.last_book.items() if s in active},
                "last_market_trade_received_at": {
                    s: t for s, t in self.last_market_trade.items() if s in active
                },
                "last_market_trade_provider_timestamp": {
                    s: t for s, t in self.last_market_trade_timestamp.items() if s in active
                },
                "unresolved_market_count": len(self.pending),
                "pending_market_limit": MAX_PENDING,
                "true_brti_available": False,
                "disk_reserve_bytes": base.DISK_RESERVE,
                **self.archive.status(),
            }
            base.atomic_json(self.archive.directory / "status.json", status)
            self.last_book = {s: t for s, t in self.last_book.items() if s in active}
            self.last_market_trade = {
                s: t for s, t in self.last_market_trade.items() if s in active
            }
            self.last_market_trade_timestamp = {
                s: t for s, t in self.last_market_trade_timestamp.items() if s in active
            }
            await base.pause(self.stop, 5)


def select_partition(events, expiry):
    ranges = events[SERIES[0]]["markets"]
    above = events[SERIES[1]]["markets"]
    for rows in (ranges, above):
        if len({str(m.get("id")) for m in rows}) != len(rows) or len(
            {m.get("slug") for m in rows}
        ) != len(rows):
            raise ValueError("duplicate_market_identity")
    interiors = []
    for market in ranges:
        terms = market["assetPriceTerms"]
        if terms.get("rangeKind") != "ASSET_PRICE_RANGE_KIND_INTERIOR":
            continue
        validate_market(market, expiry, "ASSET_PRICE_MARKET_TYPE_RANGE")
        lower, upper = money(terms["rangeLower"]), money(terms["rangeUpper"])
        if lower > upper or lower % UNIT or upper % UNIT:
            raise ValueError("invalid_interior_bounds")
        interiors.append((lower, upper, str(market["id"]), market))
    if not interiors:
        raise ValueError("no_interior_ranges")
    interiors.sort(key=lambda row: row[:3])
    if len({row[:2] for row in interiors}) != len(interiors):
        raise ValueError("duplicate_interior_bounds")
    index = (len(interiors) - 1) // 2
    lower, upper, _, chosen = interiors[index]
    matches = []
    for threshold in (lower, upper + UNIT):
        found = []
        for market in above:
            validate_market(market, expiry, "ASSET_PRICE_MARKET_TYPE_ABOVE_BELOW")
            if money(market["assetPriceTerms"]["strike"]) == threshold:
                found.append(market)
        if len(found) != 1:
            raise ValueError("matching_above_threshold_not_unique")
        matches.append(found[0])
    triple = [chosen, *matches]
    if len({str(m["id"]) for m in triple}) != 3 or len({m["slug"] for m in triple}) != 3:
        raise ValueError("partition_identity_collision")
    return {
        "interior_count": len(interiors),
        "selected_index": index,
        "range_lower": str(lower),
        "range_upper": str(upper),
        "upper_above_threshold": str(upper + UNIT),
        "markets": triple,
    }


def validate_market(market, expiry, market_type):
    terms = market["assetPriceTerms"]
    if (
        not isinstance(market.get("slug"), str)
        or not market["slug"]
        or not isinstance(market.get("id"), str)
        or not market["id"]
        or terms.get("asset") != {"assetClass": "ASSET_CLASS_CRYPTO", "symbol": "btc"}
        or terms.get("indexSymbol") != "BRTI"
        or terms.get("horizon") != "hourly"
        or terms.get("marketType") != market_type
        or type(terms.get("priceDecimals")) is not int
        or terms["priceDecimals"] != 2
        or terms.get("direction") not in (None, "ASSET_PRICE_DIRECTION_UNSPECIFIED")
        or market.get("rulesDisclaimer") not in (None, "")
        or not exact_expiry(terms.get("windowEnd"), expiry)
    ):
        raise ValueError("incompatible_hourly_market")
    # Bind the complete observed provider template, not isolated phrases that
    # could coexist with a contradictory strike, date or settlement paragraph.
    # Template changes fail closed and remain in the raw discovery archive.
    if market.get("description") != settlement_description(terms, expiry, market_type):
        raise ValueError("incompatible_settlement_description")


def settlement_description(terms, expiry, market_type):
    local = datetime.fromtimestamp(expiry, ZoneInfo("America/New_York"))
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    display = (
        f"{(local.hour - 1) % 12 + 1}:{local.minute:02d}"
        f"{'AM' if local.hour < 12 else 'PM'} ET on "
        f"{months[local.month - 1]} {local.day}, {local.year}"
    )
    if market_type == "ASSET_PRICE_MARKET_TYPE_RANGE":
        lower, upper = (display_usd(money(terms[key])) for key in ("rangeLower", "rangeUpper"))
        payoff = f"between {lower} and {upper}, inclusive of both bounds"
        unique = "Only one outcome in this event will settle to Yes. "
        equality = ""
    else:
        strike = display_usd(money(terms["strike"]))
        payoff = f"at or above {strike}"
        unique = ""
        equality = (
            f"The market settles to Yes if the settlement value is greater than or equal to {strike}. "
            f"A settlement value exactly equal to {strike} settles to Yes. "
        )
    return (
        f"This market will settle to Yes if the price of Bitcoin is {payoff} at {display}. "
        f"Otherwise, this market will settle to No. {unique}"
        "Outcome sourced from CF Benchmarks' Bitcoin Real-Time Index (BRTI), available at "
        "https://www.cfbenchmarks.com/data/indices/BRTI. The settlement value is determined "
        f"as follows: in the last minute before {display}, 60 BRTI prices are collected. "
        "The official and final value is the simple average of these prices, rounded to "
        f"the nearest 2 decimal places. {equality}"
        "Please note that this market is about the price according to CF Benchmarks' BRTI, "
        "not according to other sources or spot markets."
    )


def display_usd(value):
    text = f"{value:,.2f}"
    return "$" + (text[:-3] if text.endswith(".00") else text)


def exact_expiry(value, expiry):
    # Do not let datetime/float rounding turn a nonzero nanosecond into an hour.
    if not isinstance(value, str):
        return False
    normalized = re.sub(r"\.0+(?=Z$|\+00:00$)", "", value).replace("Z", "+00:00")
    return normalized == iso(expiry)


def money(value):
    if (
        not isinstance(value, dict)
        or value.get("currency") != "USD"
        or isinstance(value.get("value"), bool)
    ):
        raise ValueError("invalid_usd_amount")
    try:
        text = str(value["value"])
        if len(text) > 64:
            raise ValueError("invalid_usd_amount")
        result = Decimal(text)
        if not result.is_finite() or not 0 <= result <= 10**12:
            raise ValueError("invalid_usd_amount")
        if not -32 <= result.as_tuple().exponent <= 12:
            raise ValueError("invalid_usd_amount")
        # Accept only exact cents, without context-dependent Decimal rounding.
        numerator, denominator = result.as_integer_ratio()
        cents, remainder = divmod(numerator * 100, denominator)
        if remainder:
            raise ValueError("usd_amount_off_cent_grid")
        return Decimal(cents).scaleb(-2)
    except (InvalidOperation, KeyError) as exc:
        raise ValueError("invalid_usd_amount") from exc


def validate_state(state):
    if (
        not isinstance(state, dict)
        or type(state.get("schema_version")) is not int
        or state["schema_version"] != 1
        or not isinstance(state.get("tracked"), dict)
        or len(state["tracked"]) > MAX_PENDING
        or state.get("last_cutoff") is not None
        and (type(state["last_cutoff"]) is not int or state["last_cutoff"] % 3600 != 1800)
    ):
        raise ValueError("invalid_partition_checkpoint")
    selection = state.get("selection")
    if selection is not None:
        if (
            not isinstance(selection, dict)
            or type(selection.get("expiry")) is not int
            or selection["expiry"] % 3600
            or selection.get("cutoff") != selection["expiry"] - 1800
            or type(selection.get("metadata_received_ns")) is not int
            or selection["metadata_received_ns"] > selection["cutoff"] * SECOND
            or len(selection.get("markets", [])) != 3
            or state["last_cutoff"] is None
            or selection["cutoff"] > state["last_cutoff"]
        ):
            raise ValueError("invalid_partition_checkpoint")
        for index, market in enumerate(selection["markets"]):
            validate_market(
                market,
                selection["expiry"],
                "ASSET_PRICE_MARKET_TYPE_RANGE"
                if index == 0
                else "ASSET_PRICE_MARKET_TYPE_ABOVE_BELOW",
            )
        if (
            len({m["slug"] for m in selection["markets"]}) != 3
            or len({m["id"] for m in selection["markets"]}) != 3
        ):
            raise ValueError("invalid_partition_checkpoint")
        for market in selection["markets"]:
            tracked = state["tracked"].get(market["slug"])
            if tracked is not None and tracked != {
                "id": market["id"],
                "expiry": selection["expiry"],
            }:
                raise ValueError("invalid_partition_checkpoint")
    for slug, identity in state["tracked"].items():
        if (
            not isinstance(slug, str)
            or not slug
            or not isinstance(identity, dict)
            or not isinstance(identity.get("id"), str)
            or not identity["id"]
            or type(identity.get("expiry")) is not int
            or identity["expiry"] % 3600
        ):
            raise ValueError("invalid_partition_checkpoint")


def durable_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        stream.write(base.encoded(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def iso_ns(value):
    return (
        datetime.fromtimestamp(value // SECOND, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        + f".{value % SECOND:09d}+00:00"
    )


class SelectionArchiveError(RuntimeError):
    """Stop rather than activate a choice whose transition could not be archived."""


SOURCE = "hourly_partition_metadata"
SERIES = ("btc-range-hourly", "btc-strike-hourly")
UNIT = Decimal(".01")
SECOND = 1_000_000_000
MAX_RESPONSE = 1024 * 1024
MAX_PENDING = 256


if __name__ == "__main__":
    raise SystemExit(main())
