import base64
import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from polymarket_us import PolymarketUS

import bot
import core.strategy_brain.strategies.btc_15min_strategy as strategy_module
import execution.polymarket_client as client_module
import execution.trade_journal as journal_module
from execution.polymarket_client import PolymarketClient


@pytest.fixture
def clock(monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 10, 21, 18, 43, 10, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    for module in (bot, client_module, journal_module, strategy_module):
        monkeypatch.setattr(module, "datetime", Clock)
    return Clock


@pytest.fixture
def market_row(clock):
    return {
        "slug": "cpc-btc-updown-15m-2026-10-21-1830z",
        "active": True,
        "closed": False,
        "minimumTradeQty": 0.01,
        "orderPriceMinTickSize": 0.01,
        "feeCoefficient": 0.0695,
        "marketSides": [{"long": True, "tradable": True}, {"long": False, "tradable": True}],
        "assetPriceTerms": {
            "asset": {"symbol": "btc"},
            "horizon": "15m",
            "marketType": "ASSET_PRICE_MARKET_TYPE_UP_DOWN",
            "windowStart": (clock.current - timedelta(minutes=13, seconds=10)).isoformat(),
            "windowEnd": (clock.current + timedelta(minutes=1, seconds=50)).isoformat(),
        },
    }


@pytest.fixture
def venue(market_row, clock):
    class Venue:
        def __init__(self):
            self.calls = []
            self.market = copy.deepcopy(market_row)
            self.markets = {}
            self.positions = {}
            self.position_pages = None
            self.balance = {
                "currency": "USD",
                "buyingPower": 100.0,
                "currentBalance": 100.0,
                "assetNotional": 0.0,
                "assetAvailable": 0.0,
                "openOrders": 0.0,
                "marginRequirement": 0.0,
                "unsettledFunds": 0.0,
                "pendingCredit": 0.0,
                "balanceReservation": 0.0,
            }
            self.balance_snapshots = []
            self.activities = []
            self.activity_pages = None
            self.open_orders = []
            self.order_state = "ORDER_STATE_CANCELED"
            self.cancel_ack_only = False
            self.fill_quantity = 0.50
            self.fill_price = "0.6400"
            self.order = None
            self.fail_create = False
            self.preview_mismatch = False
            self.preview_delay = 0
            self.settlement = None
            self.timestamp = clock.current.isoformat()
            self.bid = "0.63"
            self.ask = "0.65"
            self.pages = None
            self.book_missing = False
            self.book_limited = False
            self.book_request_ids = []
            self.failures = {}
            self.error_body = "Unavailable"
            self.sdk = PolymarketUS(
                key_id="00000000-0000-0000-0000-000000000001",
                secret_key=base64.b64encode(bytes(32)).decode(),
                max_retries=0,
            )
            self.sdk._http.close()
            self.sdk._http = httpx.Client(transport=httpx.MockTransport(self.handle))
            self.client = PolymarketClient(self.sdk)

        def handle(self, request):
            payload = json.loads(request.content) if request.content else None
            self.calls.append((request.method, request.url.path, payload))
            path = request.url.path
            failures = self.failures.get((request.method, path), [])
            if failures:
                failure = failures.pop(0)
                if failure == "timeout":
                    raise httpx.ReadTimeout("fake lost read response", request=request)
                if failure is not None:
                    return httpx.Response(
                        failure, headers={"Retry-After": "60"}, json={"error": self.error_body}
                    )
            if request.url.host == "api.polymarket.us":
                assert request.headers.get("X-PM-Access-Key")
                assert request.headers.get("X-PM-Signature")
            if path == "/v1/account/balances":
                if self.balance_snapshots:
                    self.balance = self.balance_snapshots.pop(0)
                body = {"balances": [self.balance]}
            elif path == "/v1/portfolio/positions":
                body = (
                    self.position_pages[request.url.params.get("cursor", "first")]
                    if self.position_pages
                    else {"positions": self.positions, "eof": True}
                )
            elif path == "/v1/portfolio/activities":
                if self.activity_pages:
                    body = self.activity_pages[request.url.params.get("cursor", "first")]
                else:
                    body = {"activities": self.activities, "eof": True}
            elif path == "/v1/orders/open":
                body = {"orders": self.open_orders}
            elif path == "/v1/markets":
                offset = int(request.url.params.get("offset", 0))
                body = {"markets": self.pages[offset] if self.pages else [self.market]}
            elif path.startswith("/v1/market/slug/"):
                body = {"market": self.markets.get(path.rsplit("/", 1)[-1], self.market)}
            elif path.endswith("/book"):
                self.book_request_ids.append(request.url.params.get("_ts"))
                if self.book_limited:
                    return httpx.Response(
                        429, headers={"Retry-After": "60"}, json={"error": "Rate limited"}
                    )
                if self.book_missing:
                    return httpx.Response(404, json={"error": "Book not available yet"})
                body = {
                    "marketData": {
                        "marketSlug": self.markets.get(path.split("/")[-2], self.market)["slug"],
                        "state": "MARKET_STATE_OPEN",
                        "transactTime": self.timestamp,
                        "bids": [{"px": {"value": self.bid, "currency": "USD"}, "qty": "100"}],
                        "offers": [{"px": {"value": self.ask, "currency": "USD"}, "qty": "40"}],
                    }
                }
            elif path == "/v1/order/preview":
                clock.current += timedelta(seconds=self.preview_delay)
                body = {"order": copy.deepcopy(payload["request"])}
                if self.preview_mismatch:
                    body["order"]["quantity"] = 999
            elif path == "/v1/orders" and request.method == "POST":
                if self.fail_create:
                    raise httpx.ReadTimeout("fake lost response", request=request)
                self.order = {
                    **payload,
                    "id": "bot-order-1",
                    "state": self.order_state,
                    "cumQuantity": self.fill_quantity,
                    "leavesQuantity": 0,
                    "avgPx": {"value": self.fill_price, "currency": "USD"},
                    "commissionNotionalTotalCollected": {"value": "0.01", "currency": "USD"},
                }
                body = {"id": "bot-order-1", "executions": []}
            elif path == "/v1/order/bot-order-1/cancel":
                if not self.cancel_ack_only:
                    self.order["state"] = "ORDER_STATE_CANCELED"
                body = {}
            elif path == "/v1/order/bot-order-1":
                body = {"order": self.order}
            elif path.endswith("/settlement"):
                if self.settlement is None:
                    return httpx.Response(404, json={"error": "Not settled"})
                body = {"slug": self.market["slug"], "settlement": self.settlement}
            else:
                raise AssertionError(f"Unexpected request: {request.method} {path}")
            return httpx.Response(200, json=body)

    result = Venue()
    yield result
    result.client.close()
