"""Fixed-cutoff selection and recovery, using generated markets and transport fakes."""

import asyncio
import base64
import gzip
import hashlib
import json
import signal
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from tools import collect_partition_data as partition
from tools import collect_research_data as base


def test_lower_median_is_numeric_and_independent_of_input_order():
    events = event_pair()
    events[partition.SERIES[0]]["markets"].reverse()
    chosen = partition.select_partition(events, EXPIRY)
    assert chosen["interior_count"] == 4
    assert chosen["selected_index"] == 1
    assert chosen["range_lower"] == "100.00"
    assert chosen["range_upper"] == "199.99"
    assert chosen["upper_above_threshold"] == "200.00"
    assert [m["slug"] for m in chosen["markets"]] == ["range-100", "above-100", "above-200"]
    assert all(m["assetPriceTerms"]["windowStart"] is None for m in chosen["markets"])


@pytest.mark.parametrize(
    "mutation",
    [
        lambda e: e[partition.SERIES[1]]["markets"].pop(2),
        lambda e: e[partition.SERIES[1]]["markets"].append(
            deepcopy(e[partition.SERIES[1]]["markets"][1])
        ),
        lambda e: e[partition.SERIES[0]]["markets"][1]["assetPriceTerms"].update(
            indexSymbol="OTHER"
        ),
        lambda e: e[partition.SERIES[0]]["markets"][1]["assetPriceTerms"].update(horizon="daily"),
        lambda e: e[partition.SERIES[0]]["markets"][1]["assetPriceTerms"].update(priceDecimals=2.0),
        lambda e: e[partition.SERIES[0]]["markets"][1]["assetPriceTerms"].update(
            windowEnd=partition.iso(EXPIRY + 1)
        ),
        lambda e: e[partition.SERIES[0]]["markets"][1]["assetPriceTerms"].update(
            asset={"assetClass": "ASSET_CLASS_CRYPTO", "symbol": "eth"}
        ),
        lambda e: e[partition.SERIES[0]]["markets"][1].update(
            description="different averaging convention"
        ),
        lambda e: e[partition.SERIES[1]]["markets"][1].update(
            description="in the last minute before; 60 BRTI prices are collected; simple average; rounded to the nearest 2 decimal places; at or above"
        ),
        lambda e: e[partition.SERIES[0]]["markets"][1].update(id=None),
        lambda e: e[partition.SERIES[0]]["markets"][1]["assetPriceTerms"].update(
            rangeUpper=usd("199.999")
        ),
        lambda e: e[partition.SERIES[0]]["markets"][1]["assetPriceTerms"].update(
            rangeUpper=usd("99.99")
        ),
    ],
)
def test_incompatible_or_ambiguous_partition_is_rejected(mutation):
    events = event_pair()
    mutation(events)
    with pytest.raises(ValueError):
        partition.select_partition(events, EXPIRY)


@pytest.mark.parametrize(
    "value", [True, "NaN", "Infinity", "-1", "1e-999999999", "1000000000001", "0.001"]
)
def test_amount_validation_has_no_rounding_or_unbounded_exponent(value):
    with pytest.raises(ValueError):
        partition.money(usd(value))


def test_exact_cent_normalization_does_not_round_at_default_decimal_precision():
    assert partition.money(usd("100.000000000000000000000000000000")) == partition.Decimal("100")
    with pytest.raises(ValueError):
        partition.money(usd("100.000000000000000000000000000001"))


@pytest.mark.parametrize(
    "old,new",
    [
        ("between $100 and $199.99", "between $101 and $199.99"),
        ("inclusive of both bounds", "exclusive of both bounds"),
        ("last minute before", "last five minutes before"),
        ("60 BRTI prices", "59 BRTI prices"),
        ("simple average", "weighted average"),
        ("nearest 2 decimal places", "nearest 3 decimal places"),
        ("ET on Oct 24", "ET on Oct 25"),
    ],
)
def test_exact_template_rejects_payoff_clock_and_settlement_contradictions(old, new):
    item = market("range", 100)
    assert old in item["description"]
    item["description"] = item["description"].replace(old, new, 1)
    with pytest.raises(ValueError, match="settlement_description"):
        partition.validate_market(item, EXPIRY, "ASSET_PRICE_MARKET_TYPE_RANGE")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.replace("at or above $100", "at or above $200", 1),
        lambda d: d.replace("greater than or equal to $100", "greater than $100", 1),
        lambda d: d.replace(
            "exactly equal to $100 settles to Yes", "exactly equal to $100 settles to No", 1
        ),
        lambda d: d + " Exception: use spot price instead.",
    ],
)
def test_above_template_binds_all_strike_and_equality_sentences(mutation):
    item = market("above", 100)
    item["description"] = mutation(item["description"])
    with pytest.raises(ValueError, match="settlement_description"):
        partition.validate_market(item, EXPIRY, "ASSET_PRICE_MARKET_TYPE_ABOVE_BELOW")


@pytest.mark.parametrize("expiry", [1792818000, 1780290000, 1798783200])
def test_description_matches_timezone_aware_utc_expiry(expiry):
    item = market("above", 81000, expiry)
    partition.validate_market(item, expiry, "ASSET_PRICE_MARKET_TYPE_ABOVE_BELOW")
    item["assetPriceTerms"]["windowEnd"] = partition.iso(expiry + 3600)
    with pytest.raises(ValueError):
        partition.validate_market(item, expiry + 3600, "ASSET_PRICE_MARKET_TYPE_ABOVE_BELOW")


@pytest.mark.parametrize("field", ["windowEnd", "endDate"])
def test_submicrosecond_expiry_mismatch_is_not_rounded_away(tmp_path, monkeypatch, field):
    bad = partition.iso(EXPIRY).replace("+00:00", ".000000001Z")
    if field == "windowEnd":
        item = market("range", 100)
        item["assetPriceTerms"][field] = bad
        with pytest.raises(ValueError):
            partition.validate_market(item, EXPIRY, "ASSET_PRICE_MARKET_TYPE_RANGE")
    else:
        set_clock(monkeypatch, CUTOFF - 10)
        normal = discovery_handler([])

        def response(request):
            result = normal(request)
            if request.url.path == "/v1/events" and request.url.params["offset"] == "0":
                data = result.json()
                data["events"][0][field] = bad
                return httpx.Response(200, json=data)
            return result

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
                instance = make_collector(tmp_path, http)
                with pytest.raises(ValueError, match="identity_mismatch"):
                    await instance.discover(EXPIRY)
                assert instance.cache is None

        asyncio.run(scenario())
    assert partition.exact_expiry(partition.iso(EXPIRY).replace("+00:00", ".000000000Z"), EXPIRY)


def test_parent_market_schedule_is_unchanged_and_hourly_override_keeps_null_start(tmp_path):
    archive = MemoryArchive(tmp_path)
    parent = base.Collector(None, None, archive, asyncio.Event())
    parent.markets = {
        "active": {
            "assetPriceTerms": {
                "windowStart": partition.iso(CUTOFF - 1),
                "windowEnd": partition.iso(EXPIRY),
            }
        },
        "future": {
            "assetPriceTerms": {
                "windowStart": partition.iso(CUTOFF + 61),
                "windowEnd": partition.iso(EXPIRY),
            }
        },
        "expired": {
            "assetPriceTerms": {
                "windowStart": partition.iso(CUTOFF - 3600),
                "windowEnd": partition.iso(CUTOFF),
            }
        },
    }
    assert parent.desired_markets(CUTOFF) == {"active"}
    child = make_collector(tmp_path)
    child.state = selected_state()
    child.markets = {m["slug"]: m for m in child.state["selection"]["markets"]}
    assert len(child.desired_markets(CUTOFF)) == 3
    assert child.desired_markets(EXPIRY) == set()


def test_discovery_requires_complete_unique_event_pages_and_keeps_exact_raw(tmp_path, monkeypatch):
    clock = set_clock(monkeypatch, CUTOFF - 30)
    requests = []
    transport = httpx.MockTransport(discovery_handler(requests))

    async def scenario():
        async with httpx.AsyncClient(transport=transport) as http:
            collector = make_collector(tmp_path, http)
            await collector.discover(EXPIRY)
            assert collector.cache["received_ns"] == clock.ns
            assert len(collector.cache["proof"]) == 4
            assert len(requests) == 5
            assert all(r.method == "GET" for r in requests)
            assert all("_ts" in r.url.params for r in requests)
            for row in collector.archive.rows:
                assert row["kind"] == "discovery_response"
                assert hashlib.sha256(row["raw_text"].encode()).hexdigest() == row["body_sha256"]
                assert "set-cookie" not in row["headers"]
                assert row["received_ns"] == clock.ns

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["extra_event", "wrong_series", "wrong_expiry", "http_error"])
def test_discovery_failure_never_replaces_prior_complete_cache(tmp_path, monkeypatch, failure):
    set_clock(monkeypatch, CUTOFF - 10)
    baseline = discovery_handler([])

    def handler(request):
        if request.url.path == "/v1/events" and request.url.params["seriesId"] == "377":
            if failure == "http_error":
                return httpx.Response(503, json={"error": "unavailable"})
            event = deepcopy(event_pair()[partition.SERIES[1]])
            if failure == "wrong_series":
                event["seriesSlug"] = "wrong"
            if failure == "wrong_expiry":
                event["endDate"] = partition.iso(EXPIRY + 1)
            if failure == "extra_event" or request.url.params["offset"] == "0":
                return httpx.Response(200, json={"events": [event]})
        return baseline(request)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            collector = make_collector(tmp_path, http)
            collector.cache = cache()
            before = deepcopy(collector.cache)
            with pytest.raises((ValueError, httpx.HTTPStatusError)):
                await collector.discover(EXPIRY)
            assert collector.cache == before

    asyncio.run(scenario())


def test_post_cutoff_response_does_not_replace_pre_cutoff_cache(tmp_path, monkeypatch):
    set_clock(monkeypatch, CUTOFF + 1)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(discovery_handler([]))) as http:
            collector = make_collector(tmp_path, http)
            collector.cache = cache()
            before = deepcopy(collector.cache)
            await collector.discover(EXPIRY)
            assert collector.cache == before
            await collector.freeze_selection(EXPIRY)
            assert collector.state["selection"]["metadata_received_ns"] == before["received_ns"]

    asyncio.run(scenario())


def test_cutoff_is_durable_once_only_and_restart_does_not_reselect(tmp_path, monkeypatch):
    set_clock(monkeypatch, CUTOFF)

    async def scenario():
        collector = make_collector(tmp_path)
        collector.cache = cache()
        await collector.freeze_selection(EXPIRY)
        rows = deepcopy(collector.archive.rows)
        assert collector.archive.flushed == [1, 2]
        assert len(collector.pending) == 3
        assert len(collector.desired_markets(CUTOFF)) == 3
        persisted = json.loads((tmp_path / "partition-state.json").read_text())
        assert persisted == collector.state
        restarted = make_collector(tmp_path)
        assert restarted.state == persisted
        assert restarted.markets == collector.markets
        restarted.cache = cache()
        restarted.cache["events"][partition.SERIES[0]]["markets"].pop(0)
        await restarted.freeze_selection(EXPIRY)
        assert [r["kind"] for r in restarted.archive.rows] == ["partition_selection_resumed"]
        assert collector.archive.rows == rows

    asyncio.run(scenario())


def test_late_startup_records_miss_without_get_or_backdating(tmp_path, monkeypatch):
    set_clock(monkeypatch, CUTOFF + 10)

    async def scenario():
        collector = make_collector(tmp_path)
        await collector.cycle()
        assert collector.state["selection"] is None
        assert collector.state["last_cutoff"] == CUTOFF
        assert collector.archive.rows[0]["kind"] == "partition_selection_missed"
        assert collector.archive.rows[0]["reason"] == "no_complete_pre_cutoff_discovery"
        await collector.cycle()
        assert len(collector.archive.rows) == 1

    asyncio.run(scenario())


def test_before_cutoff_cannot_activate(tmp_path, monkeypatch):
    set_clock(monkeypatch, CUTOFF - 1)
    collector = make_collector(tmp_path)
    collector.cache = cache()
    with pytest.raises(ValueError, match="selection_before_cutoff"):
        asyncio.run(collector.freeze_selection(EXPIRY))
    assert collector.markets == {}


def test_checkpoint_failure_is_fatal_and_does_not_publish_selection(tmp_path, monkeypatch):
    set_clock(monkeypatch, CUTOFF)
    collector = make_collector(tmp_path)
    collector.cache = cache()
    monkeypatch.setattr(
        partition, "durable_json", lambda *_: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(OSError):
        asyncio.run(collector.metadata_loop())
    assert collector.markets == {}
    assert collector.state["last_cutoff"] is None
    assert [r["kind"] for r in collector.archive.rows] == ["partition_selection_prepared"]


def test_checkpoint_without_commit_marker_resumes_same_choice(tmp_path, monkeypatch):
    set_clock(monkeypatch, CUTOFF)
    collector = make_collector(tmp_path)
    collector.cache = cache()
    emit = collector.archive.emit
    collector.archive.emit = lambda kind, **fields: (
        None if kind == "partition_selected" else emit(kind, **fields)
    )
    with pytest.raises(partition.SelectionArchiveError, match="commit_archive_rejected"):
        asyncio.run(collector.freeze_selection(EXPIRY))
    assert collector.markets == {}
    checkpoint = json.loads(collector.state_path.read_text())
    restarted = make_collector(tmp_path)
    assert restarted.state == checkpoint
    assert len(restarted.desired_markets(CUTOFF)) == 3
    resumed = restarted.archive.rows[0]
    assert resumed["kind"] == "partition_selection_resumed"
    assert resumed["source_proof"] == cache()["proof"]
    assert resumed["preparation_source"]["session_id"] == collector.archive.session
    assert resumed["preparation_source"]["session_id"] != restarted.archive.session
    assert checkpoint["selection"]["cutoff"] == CUTOFF


def test_prepared_only_crash_is_explicit_miss_without_reselection(tmp_path, monkeypatch):
    set_clock(monkeypatch, CUTOFF + 1)
    # No durable checkpoint survived. Old archive rows are not scanned/repaired.
    collector = make_collector(tmp_path)
    collector.archive.emit("partition_selection_prepared", **selected_state()["selection"])
    restarted = make_collector(tmp_path)
    asyncio.run(restarted.cycle())
    assert restarted.state["selection"] is None
    assert restarted.archive.rows[0]["kind"] == "partition_selection_missed"


@pytest.mark.parametrize("collision", ["slug", "id"])
def test_new_selection_cannot_replace_unresolved_identity(tmp_path, monkeypatch, collision):
    set_clock(monkeypatch, CUTOFF)
    collector = make_collector(tmp_path)
    collector.cache = cache()
    slug, identity = (
        ("range-100", "old-id") if collision == "slug" else ("old-slug", "id-range-100")
    )
    collector.state["tracked"] = {slug: {"id": identity, "expiry": EXPIRY - 3600}}
    before = deepcopy(collector.state["tracked"])
    asyncio.run(collector.freeze_selection(EXPIRY))
    assert collector.state["tracked"] == before
    assert collector.last_selection_error == "retained_market_identity_collision"
    assert collector.markets == {}


def test_rejected_archive_selection_never_activates(tmp_path, monkeypatch):
    set_clock(monkeypatch, CUTOFF)
    collector = make_collector(tmp_path)
    collector.cache = cache()
    collector.archive.emit = lambda *a, **kw: None
    with pytest.raises(RuntimeError, match="selection_archive_rejected"):
        asyncio.run(collector.freeze_selection(EXPIRY))
    assert collector.markets == {}
    assert not collector.state_path.exists()


def test_resolution_retired_only_after_durable_metadata_and_restart(tmp_path, monkeypatch):
    set_clock(monkeypatch, CUTOFF)

    async def scenario():
        collector = make_collector(tmp_path)
        collector.cache = cache()
        await collector.freeze_selection(EXPIRY)
        selected = deepcopy(collector.markets)
        for m in selected.values():
            m["status"] = "MARKET_STATUS_RESOLVED"

        async def response(path, params, kind):
            slug = path.rsplit("/", 1)[-1]
            return {"market": selected[slug]}, {}

        collector.get_evidence = response
        await collector.refresh_selected()
        assert collector.pending == {}
        assert collector.state["tracked"] == {}
        assert len(collector.archive.flushed) == 5
        assert collector.desired_markets(CUTOFF) == set()
        restarted = make_collector(tmp_path)
        assert restarted.markets == {}

    asyncio.run(scenario())


def test_failed_resolution_flush_retains_retry_identity(tmp_path, monkeypatch):
    set_clock(monkeypatch, CUTOFF)

    async def scenario():
        collector = make_collector(tmp_path)
        collector.cache = cache()
        await collector.freeze_selection(EXPIRY)

        async def response(path, params, kind):
            market = deepcopy(collector.markets[path.rsplit("/", 1)[-1]])
            market["status"] = "MARKET_STATUS_RESOLVED"
            return {"market": market}, {}

        async def fail(receipt):
            raise OSError("fsync")

        collector.get_evidence = response
        collector.archive.flush = fail
        with pytest.raises(OSError):
            await collector.refresh_selected()
        assert len(collector.pending) == 3
        assert len(collector.state["tracked"]) == 3

    asyncio.run(scenario())


def test_pending_limit_misses_new_hour_without_eviction(tmp_path, monkeypatch):
    set_clock(monkeypatch, CUTOFF)
    collector = make_collector(tmp_path)
    collector.cache = cache()
    collector.state["tracked"] = {
        f"old-{n}": {"id": str(n), "expiry": EXPIRY - 3600} for n in range(254)
    }
    before = deepcopy(collector.state["tracked"])
    asyncio.run(collector.freeze_selection(EXPIRY))
    assert collector.state["tracked"] == before
    assert collector.last_selection_error == "pending_resolution_capacity"
    assert collector.markets == {}


@pytest.mark.parametrize("body", [b"\xff\xfe", b'{"value":NaN}', b"{truncated"])
def test_invalid_response_is_preserved_exactly_before_parse_rejection(tmp_path, body):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
        ) as http:
            collector = make_collector(tmp_path, http)
            with pytest.raises((ValueError, UnicodeError)):
                await collector.get_evidence("/fixture", {}, "metadata_response")
            row = collector.archive.rows[0]
            restored = (
                row["raw_text"].encode()
                if "raw_text" in row
                else base64.b64decode(row["raw_bytes_base64"])
            )
            assert restored == body
            assert row["body_sha256"] == hashlib.sha256(body).hexdigest()

    asyncio.run(scenario())


def test_oversize_response_has_explicit_error_and_no_partial_candidate(tmp_path):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=b"x" * (partition.MAX_RESPONSE + 1))
            )
        ) as http:
            collector = make_collector(tmp_path, http)
            with pytest.raises(ValueError, match="size_limit"):
                await collector.get_evidence("/fixture", {}, "discovery_response")
            assert collector.archive.rows[0]["error_class"] == "ResponseSizeLimit"
            assert collector.cache is None

    asyncio.run(scenario())


@pytest.mark.parametrize("retry,expected", [("1", 30), ("600", 300), ("nonsense", 30)])
def test_rate_limit_preserves_response_and_obeys_bounded_backoff(
    tmp_path, monkeypatch, retry, expected
):
    waits = []

    async def pause(stop, seconds):
        waits.append(seconds)

    monkeypatch.setattr(base, "pause", pause)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    429, json={"error": "rate"}, headers={"retry-after": retry}
                )
            )
        ) as http:
            collector = make_collector(tmp_path, http)
            with pytest.raises(httpx.HTTPStatusError):
                await collector.get_evidence("/fixture", {}, "metadata_response")
            assert collector.archive.rows[0]["http_status"] == 429
            assert waits == [expected]

    asyncio.run(scenario())


def test_real_archive_keeps_selection_metadata_and_checkpoint(tmp_path, monkeypatch):
    set_clock(monkeypatch, CUTOFF)

    async def scenario():
        archive = base.Archive(tmp_path)
        try:
            collector = partition.PartitionCollector(None, None, archive, asyncio.Event())
            collector.cache = cache()
            await collector.freeze_selection(EXPIRY)
        finally:
            archive.close()
        rows = [
            json.loads(line)
            for path in tmp_path.glob("*.jsonl.gz")
            for line in gzip.decompress(path.read_bytes()).splitlines()
        ]
        assert [r["kind"] for r in rows] == [
            "partition_selection_prepared",
            "partition_selected",
            "market_metadata",
            "market_metadata",
            "market_metadata",
        ]
        assert [r["sequence"] for r in rows] == [1, 2, 3, 4, 5]
        assert all(m["assetPriceTerms"]["windowStart"] is None for m in rows[0]["markets"])
        assert (
            json.loads((tmp_path / "partition-state.json").read_text())["selection"]["cutoff"]
            == CUTOFF
        )

    asyncio.run(scenario())


def test_corrupt_or_unknown_pending_identity_fails_closed(tmp_path):
    (tmp_path / "pending-markets.json").write_text('{"unknown":"2026-10-24T00:00:00Z"}')
    with pytest.raises(ValueError, match="pending_identity"):
        make_collector(tmp_path)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(last_cutoff=True),
        lambda s: s["selection"].update(metadata_received_ns=(CUTOFF + 1) * partition.SECOND),
        lambda s: s["selection"]["markets"].append(s["selection"]["markets"][0]),
        lambda s: s["tracked"]["range-100"].update(id="different"),
    ],
)
def test_invalid_checkpoint_rejected(tmp_path, mutate):
    state = selected_state()
    mutate(state)
    (tmp_path / "partition-state.json").write_text(json.dumps(state))
    with pytest.raises(ValueError):
        make_collector(tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("rulesDisclaimer", "Override: settle with spot price."),
        ("direction", "ASSET_PRICE_DIRECTION_BELOW"),
    ],
)
def test_overriding_disclaimer_or_direction_rejected(field, value):
    item = market("above", 100)
    (item if field == "rulesDisclaimer" else item["assetPriceTerms"])[field] = value
    with pytest.raises(ValueError, match="incompatible_hourly_market"):
        partition.validate_market(item, EXPIRY, "ASSET_PRICE_MARKET_TYPE_ABOVE_BELOW")


def test_discovery_only_last_minute_at_fifteen_second_cadence(tmp_path, monkeypatch):
    clock = set_clock(monkeypatch, CUTOFF - 61)
    calls = []
    collector = make_collector(tmp_path)

    async def discover(expiry):
        calls.append((expiry, clock.ns))

    collector.discover = discover

    async def scenario():
        await collector.cycle()
        assert calls == []
        for offset in (-60, -59, -46, -45, -30, -15):
            clock.ns = (CUTOFF + offset) * partition.SECOND
            await collector.cycle()
        assert [ns // partition.SECOND - CUTOFF for _, ns in calls] == [-60, -45, -30, -15]
        assert all(expiry == EXPIRY for expiry, _ in calls)

    asyncio.run(scenario())


def test_slow_discovery_freezes_earlier_cache_immediately_after_return(tmp_path, monkeypatch):
    clock = set_clock(monkeypatch, CUTOFF - 1)
    collector = make_collector(tmp_path)
    collector.cache = cache()

    async def discover(expiry):
        clock.ns = (CUTOFF + 1) * partition.SECOND

    async def refresh():
        assert collector.state["last_cutoff"] == CUTOFF

    collector.discover = discover
    collector.refresh_selected = refresh
    asyncio.run(collector.cycle())
    assert collector.state["selection"]["metadata_received_ns"] == (CUTOFF - 10) * partition.SECOND


def test_book_loop_binds_three_actual_markets_and_rolls_off_both_subscriptions(
    tmp_path, monkeypatch
):
    clock = set_clock(monkeypatch, CUTOFF)
    collector = make_collector(tmp_path)
    collector.state = selected_state()
    collector.markets = {m["slug"]: m for m in collector.state["selection"]["markets"]}
    socket = Socket()
    collector.socket_factory = lambda: socket
    pauses = 0

    async def pause(stop, seconds):
        nonlocal pauses
        pauses += 1
        if pauses == 1:
            assert len(socket.subscriptions) == 6
            assert set(collector.last_book) == set(collector.markets)
            clock.ns = EXPIRY * partition.SECOND
        else:
            stop.set()

    monkeypatch.setattr(base, "pause", pause)
    asyncio.run(collector.book_loop())
    assert socket.closed
    assert socket.subscriptions == {}
    assert len(socket.unsubscribed) == 6
    messages = [
        r for r in collector.archive.rows if r["kind"] in ("market_message", "market_trade_message")
    ]
    assert len(messages) == 6
    assert all(r["binding_status"] == "valid" and r["generation"] == 1 for r in messages)
    assert all(
        r["payload"]["marketData"]["bids"] == [] for r in messages if r["kind"] == "market_message"
    )
    assert all(
        r["payload"]["trade"]["quantity"] == "0.123456789012345678901"
        for r in messages
        if r["kind"] == "market_trade_message"
    )


def test_monitor_reports_receipts_and_quiet_trade_absence_truthfully(tmp_path, monkeypatch):
    set_clock(monkeypatch, CUTOFF)
    collector = make_collector(tmp_path)
    collector.state = selected_state()
    collector.markets = {m["slug"]: m for m in collector.state["selection"]["markets"]}
    collector.last_book = {"range-100": "actual-receipt", "old": "prior"}
    collector.archive.session = "fixture-session"
    collector.archive.status = lambda: {"written": 8, "dropped": 0, "durable_sequence": 8}

    async def pause(stop, seconds):
        stop.set()

    monkeypatch.setattr(base, "pause", pause)
    asyncio.run(collector.monitor_loop())
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["expected_subscription_count"] == 6
    assert status["last_book_received_at"] == {"range-100": "actual-receipt"}
    assert status["last_market_trade_received_at"] == {}
    assert "last_book_provider_timestamp" not in status
    assert status["written"] == 8 and status["dropped"] == 0
    assert status["disk_reserve_bytes"] == 512 * 1024 * 1024


@pytest.mark.parametrize("trigger", ["signal", "task_failure", "close_failure"])
def test_collect_supervises_only_metadata_book_monitor_and_closes(tmp_path, monkeypatch, trigger):
    set_clock(monkeypatch, CUTOFF - 30)

    async def scenario():
        blocked, cancelled = asyncio.Event(), asyncio.Event()
        callbacks, closed = {}, []
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop, "add_signal_handler", lambda signum, cb: callbacks.update({signum: cb})
        )
        monkeypatch.setattr(loop, "remove_signal_handler", lambda signum: callbacks.pop(signum))

        async def http_handler(request):
            assert request.method == "GET" and request.url.path == "/v1/series"
            blocked.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        def close_exchange():
            closed.append("exchange")
            if trigger == "close_failure":
                raise RuntimeError("SDK close failed")

        exchange = SimpleNamespace(
            client=SimpleNamespace(
                _http=SimpleNamespace(event_hooks={"request": []}),
                ws=SimpleNamespace(markets=Socket),
            ),
            close=close_exchange,
        )
        monkeypatch.setattr(base.PolymarketClient, "connect", lambda _: exchange)
        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            partition.httpx,
            "AsyncClient",
            lambda **kw: real_client(transport=httpx.MockTransport(http_handler), **kw),
        )

        async def forbidden(self):
            raise AssertionError("unrelated source task started")

        for name in ("reference_loop", "spot_loop", "futures_loop"):
            monkeypatch.setattr(base.Collector, name, forbidden)
        if trigger == "task_failure":

            async def failing(self):
                await blocked.wait()
                raise RuntimeError("supervised failure")

            monkeypatch.setattr(partition.PartitionCollector, "monitor_loop", failing)
        task = asyncio.create_task(partition.collect(tmp_path / "unused.env", tmp_path))
        await asyncio.wait_for(blocked.wait(), 1)
        if trigger in ("signal", "close_failure"):
            callbacks[signal.SIGTERM]()
            if trigger == "close_failure":
                with pytest.raises(RuntimeError, match="SDK close failed"):
                    await asyncio.wait_for(task, 2)
            else:
                await asyncio.wait_for(task, 2)
        else:
            with pytest.raises(RuntimeError, match="supervised failure"):
                await asyncio.wait_for(task, 2)
        assert cancelled.is_set() and closed == ["exchange"] and not callbacks
        assert exchange.client._http.event_hooks["request"] == [base.require_get]
        rows = [
            json.loads(line)
            for path in tmp_path.glob("*.jsonl.gz")
            for line in gzip.decompress(path.read_bytes()).splitlines()
        ]
        assert rows[-1]["kind"] == "collector_stopped"
        assert not any(
            "spot" in r["kind"] or "futures" in r["kind"] or "reference" in r["kind"] for r in rows
        )

    asyncio.run(scenario())


def make_collector(path, http=None):
    return partition.PartitionCollector(http, None, MemoryArchive(path), asyncio.Event())


def set_clock(monkeypatch, seconds):
    clock = SimpleNamespace(ns=int(seconds * partition.SECOND))
    monkeypatch.setattr(partition.time, "time", lambda: clock.ns / partition.SECOND)
    monkeypatch.setattr(partition.time, "time_ns", lambda: clock.ns)
    return clock


def discovery_handler(requests):
    def respond(request):
        requests.append(request)
        if request.url.path == "/v1/series":
            data = {
                "series": [{"slug": s, "id": str(376 + i)} for i, s in enumerate(partition.SERIES)]
            }
        else:
            series = partition.SERIES[int(request.url.params["seriesId"]) - 376]
            data = {"events": [event_pair()[series]] if request.url.params["offset"] == "0" else []}
        return httpx.Response(
            200, json=data, headers={"set-cookie": "not archived", "date": "fixture"}
        )

    return respond


def cache():
    return {
        "expiry": EXPIRY,
        "received_ns": (CUTOFF - 10) * partition.SECOND,
        "events": event_pair(),
        "proof": [
            {
                "session_id": "discovery-session",
                "sequence": 1,
                "received_ns": (CUTOFF - 10) * partition.SECOND,
                "body_sha256": "fixture",
            }
        ],
    }


def selected_state():
    result = partition.select_partition(event_pair(), EXPIRY)
    selection = {
        **result,
        "expiry": EXPIRY,
        "cutoff": CUTOFF,
        "metadata_received_ns": (CUTOFF - 10) * partition.SECOND,
        "source_proof": [],
    }
    return {
        "schema_version": 1,
        "last_cutoff": CUTOFF,
        "selection": selection,
        "tracked": {m["slug"]: {"id": m["id"], "expiry": EXPIRY} for m in selection["markets"]},
    }


def event_pair():
    return {
        partition.SERIES[0]: {
            "seriesSlug": partition.SERIES[0],
            "endDate": partition.iso(EXPIRY),
            "markets": [market("range", n) for n in (0, 100, 200, 300)],
        },
        partition.SERIES[1]: {
            "seriesSlug": partition.SERIES[1],
            "endDate": partition.iso(EXPIRY),
            "markets": [market("above", n) for n in (0, 100, 200, 300, 400)],
        },
    }


def market(kind, strike, expiry=None):
    expiry = EXPIRY if expiry is None else expiry
    terms = {
        "asset": {"assetClass": "ASSET_CLASS_CRYPTO", "symbol": "btc"},
        "horizon": "hourly",
        "indexSymbol": "BRTI",
        "priceDecimals": 2,
        "windowStart": None,
        "windowEnd": partition.iso(expiry),
    }
    if kind == "range":
        terms.update(
            marketType="ASSET_PRICE_MARKET_TYPE_RANGE",
            rangeKind="ASSET_PRICE_RANGE_KIND_INTERIOR",
            rangeLower=usd(str(strike)),
            rangeUpper=usd(str(partition.Decimal(strike) + partition.Decimal("99.99"))),
        )
    else:
        terms.update(marketType="ASSET_PRICE_MARKET_TYPE_ABOVE_BELOW", strike=usd(str(strike)))
    return {
        "id": f"id-{kind}-{strike}",
        "slug": f"{kind}-{strike}",
        "assetPriceTerms": terms,
        "status": "MARKET_STATUS_OPEN",
        "description": fixture_description(kind, strike, expiry),
    }


def fixture_description(kind, strike, expiry):
    local = datetime.fromtimestamp(expiry, ZoneInfo("America/New_York"))
    date = (
        local.strftime("%I:%M%p").lstrip("0")
        + f" ET on {local.strftime('%b')} {local.day}, {local.year}"
    )
    low = f"${strike:,}"
    high = f"${partition.Decimal(strike) + partition.Decimal('99.99'):,.2f}"
    prefix = (
        f"between {low} and {high}, inclusive of both bounds"
        if kind == "range"
        else f"at or above {low}"
    )
    only = "Only one outcome in this event will settle to Yes. " if kind == "range" else ""
    equal = (
        ""
        if kind == "range"
        else f"The market settles to Yes if the settlement value is greater than or equal to {low}. A settlement value exactly equal to {low} settles to Yes. "
    )
    return (
        f"This market will settle to Yes if the price of Bitcoin is {prefix} at {date}. Otherwise, this market will settle to No. {only}"
        "Outcome sourced from CF Benchmarks' Bitcoin Real-Time Index (BRTI), available at https://www.cfbenchmarks.com/data/indices/BRTI. "
        f"The settlement value is determined as follows: in the last minute before {date}, 60 BRTI prices are collected. "
        f"The official and final value is the simple average of these prices, rounded to the nearest 2 decimal places. {equal}"
        "Please note that this market is about the price according to CF Benchmarks' BRTI, not according to other sources or spot markets."
    )


def usd(value):
    return {"value": value, "currency": "USD"}


class MemoryArchive:
    def __init__(self, directory):
        self.directory = directory
        self.session = f"memory-session-{id(self)}"
        self.rows, self.flushed = [], []

    def emit(self, kind, **fields):
        self.rows.append({"kind": kind, **deepcopy(fields)})
        return len(self.rows)

    async def flush(self, receipt):
        self.flushed.append(receipt)

    def check(self):
        pass


class Socket:
    def __init__(self):
        self.callbacks, self.subscriptions = {}, {}
        self.closed, self.unsubscribed = False, []

    def on(self, name, callback):
        self.callbacks[name] = callback

    async def connect(self):
        pass

    async def subscribe_market_data(self, request, slugs):
        self.subscribe(request, slugs[0], base.BOOK_SUBSCRIPTION)

    async def subscribe_trades(self, request, slugs):
        self.subscribe(request, slugs[0], base.TRADE_SUBSCRIPTION)

    def subscribe(self, request, slug, kind):
        self.subscriptions[request] = (slug, kind)
        field = "marketData" if kind == base.BOOK_SUBSCRIPTION else "trade"
        payload = (
            {"marketSlug": slug, "bids": [], "offers": []}
            if field == "marketData"
            else {
                "marketSlug": slug,
                "tradeTime": "2026-10-24T04:30:00.123456789Z",
                "quantity": "0.123456789012345678901",
            }
        )
        self.callbacks["message"]({"requestId": request, "subscriptionType": kind, field: payload})

    async def unsubscribe(self, request):
        self.unsubscribed.append(request)
        del self.subscriptions[request]

    async def close(self):
        self.closed = True


EXPIRY = 1792818000  # Whole-hour synthetic UTC expiry.
CUTOFF = EXPIRY - 1800
