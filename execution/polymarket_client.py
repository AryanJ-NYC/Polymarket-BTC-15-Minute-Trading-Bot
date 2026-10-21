"""Polymarket US protocol, contract validation, and bounded IOC execution."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import time_ns

from dotenv import dotenv_values
from polymarket_us import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    NotFoundError,
    PolymarketUS,
)


class PolymarketClient:
    def __init__(self, client: PolymarketUS):
        self.client = client

    @classmethod
    def connect(cls, env_file: Path) -> PolymarketClient:
        """Read only the explicitly selected US credential file."""
        config = dotenv_values(env_file)
        key_id = _credential(config, "POLYMARKET_US_KEY_ID", "POLYMARKET_KEY_ID")
        secret = _credential(
            config, "POLYMARKET_US_SECRET_KEY", "POLYMARKET_SECRET_KEY", "POLYMARKET_SECRET"
        )
        return cls(PolymarketUS(key_id=key_id, secret_key=secret, timeout=10, max_retries=0))

    def close(self) -> None:
        self.client.close()

    def account(self) -> Account:
        balances = self._fresh_get("/v1/account/balances")["balances"]
        usd = [row for row in balances if row["currency"] == "USD"]
        if len(usd) != 1:
            raise ValueError("Expected exactly one USD account balance")
        buying_power = number(usd[0]["buyingPower"])
        positions = {}
        cursor = None
        for _ in range(100):
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = self._fresh_get("/v1/portfolio/positions", params)
            positions.update(page["positions"])
            if page.get("eof") is True:
                break
            next_cursor = page.get("nextCursor")
            if not next_cursor or next_cursor == cursor:
                raise ValueError("Incomplete positions pagination")
            cursor = next_cursor
        else:
            raise ValueError("Positions pagination limit reached")
        orders = self.client.orders.list()["orders"]
        # Validate quantity fields rather than treating missing data as zero.
        for position in positions.values():
            position_quantity(position)
        bankroll = self._bankroll(usd[0], positions)
        return Account(buying_power, positions, orders, bankroll)

    def discover_market(self) -> Market | None:
        """Discover by typed asset terms and measurement windows, never slug guesses."""
        now = datetime.now(timezone.utc)
        params = {
            "categories": ["crypto"],
            "active": True,
            "closed": False,
            "endDateMin": now.isoformat(),
            "endDateMax": (now + timedelta(hours=2)).isoformat(),
            "limit": 100,
        }
        candidates = []
        for offset in range(0, 2000, 100):
            rows = self.client.markets.list({**params, "offset": offset})["markets"]
            for row in rows:
                terms = row.get("assetPriceTerms") or {}
                if (
                    terms.get("asset", {}).get("symbol") == "btc"
                    and terms.get("horizon") == "15m"
                    and terms.get("marketType") == "ASSET_PRICE_MARKET_TYPE_UP_DOWN"
                ):
                    market = parse_market(row)
                    if market.start <= now < market.end:
                        candidates.append(market)
            if len(rows) < 100:
                break
        else:
            raise ValueError("Market discovery pagination limit reached")
        unique = {market.slug: market for market in candidates}
        if len(unique) > 1:
            raise ValueError("Multiple current BTC 15-minute contracts; refusing ambiguity")
        return next(iter(unique.values()), None)

    def market(self, slug: str) -> Market:
        return parse_market(self.client.markets.retrieve_by_slug(slug)["market"])

    def book(self, market: Market) -> Book:
        book = self._book(market.slug)
        if not market.start <= datetime.now(timezone.utc) < market.end:
            raise ValueError("Contract is outside its measurement window")
        return book

    def buy_plan(self, market: Market, book: Book, outcome: str, budget: Decimal) -> Plan:
        _validate_book(market, book)
        if outcome not in {"up", "down"} or not budget.is_finite() or budget <= ZERO:
            raise ValueError("Expected Up/Down and a finite positive entry budget")
        yes_price, available = book.asks[0] if outcome == "up" else book.bids[0]
        price = yes_price if outcome == "up" else ONE - yes_price
        # The maximum fee per share bounds cost even when a fill improves in price.
        quantity = _floor(
            min(available, budget / (price + market.fee_coefficient / 4)), market.increment
        )
        fee_bound = estimate_fee(market.fee_coefficient, quantity, Decimal("0.5"))
        while quantity > ZERO and quantity * price + fee_bound > budget:
            quantity -= market.increment
            fee_bound = estimate_fee(market.fee_coefficient, quantity, Decimal("0.5"))
        if quantity < market.increment:
            raise ValueError("Budget or visible depth is below the minimum trade quantity")
        return _plan(market, book, outcome, "BUY", yes_price, quantity, fee_bound)

    def sell_plan(self, market: Market, book: Book, outcome: str, quantity: Decimal) -> Plan:
        _validate_book(market, book)
        if outcome not in {"up", "down"}:
            raise ValueError("Expected Up or Down")
        yes_price, available = book.bids[0] if outcome == "up" else book.asks[0]
        quantity = _floor(min(quantity, available), market.increment)
        if quantity < market.increment:
            raise ValueError("Position or visible depth is below the minimum trade quantity")
        fee = estimate_fee(market.fee_coefficient, quantity, Decimal("0.5"))
        return _plan(market, book, outcome, "SELL", yes_price, quantity, fee)

    def submit(self, plan: Plan) -> str:
        """Preview and submit exactly once, returning the exchange-assigned ID.

        The caller must durably reserve the attempt before entering this method.
        Expiry before create is explicitly non-submitted; create failures remain unknown.
        """
        if datetime.now(timezone.utc) >= plan.expires_at:
            raise OrderNotSubmitted("Order plan has expired")
        preview = self.client.orders.preview({"request": plan.request})["order"]
        for field in ("marketSlug", "intent", "type", "tif"):
            if preview.get(field) != plan.request[field]:
                raise ValueError("Order preview changed the requested contract or order terms")
        if (
            number(preview["quantity"]) != plan.quantity
            or amount(preview["price"]) != plan.yes_price
        ):
            raise ValueError("Order preview changed the requested price or quantity")
        if datetime.now(timezone.utc) >= plan.expires_at:
            raise OrderNotSubmitted("Order plan expired during preview")
        result = self.client.orders.create(plan.request)
        order_id = result["id"]
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("Order response did not include an order ID")
        return order_id

    def order(self, order_id: str) -> dict:
        """Read a known ID with bounded recovery; never replay order creation."""
        for attempt in range(3):
            try:
                order = self.client.orders.retrieve(order_id)["order"]
                break
            except (APIConnectionError, APIStatusError) as exc:
                status = getattr(exc, "status_code", None)
                if attempt == 2 or (
                    status is not None and status not in {404, 429} and not 500 <= status < 600
                ):
                    raise
                time.sleep(_order_read_delay(exc, attempt))
        if order["id"] != order_id:
            raise ValueError("Order readback ID mismatch")
        return order

    def cancel(self, order_id: str, slug: str) -> dict:
        """Cancel only a known order and require independent terminal readback."""
        order = self.order(order_id)
        if order["marketSlug"] != slug:
            raise ValueError("Cancellation contract mismatch")
        if order["state"] not in TERMINAL_STATES:
            self.client.orders.cancel(order_id, {"marketSlug": slug})
            order = self.order(order_id)
        if order["state"] not in TERMINAL_STATES:
            raise ValueError("Cancellation is not confirmed; trading must stop")
        return order

    def settlement(self, slug: str) -> Decimal | None:
        try:
            result = self._fresh_get(f"/v1/markets/{slug}/settlement")
        except NotFoundError:
            return None
        if result["slug"] != slug:
            raise ValueError("Settlement contract mismatch")
        value = number(result["settlement"])
        if not ZERO <= value <= ONE:
            raise ValueError("Invalid settlement value")
        return value

    def resolution(self, slug: str, expected_quantity: Decimal) -> dict | None:
        """Return matching provider evidence without interpreting its fee/cost fields."""
        cursor = None
        resolutions = []
        for _ in range(100):
            params = {"marketSlug": slug, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = self.client.portfolio.activities(params)
            for activity in page["activities"]:
                if activity["type"] == "ACTIVITY_TYPE_TRADE":
                    trade = activity["trade"]
                    if trade["marketSlug"] != slug:
                        raise ValueError("Activity contract mismatch")
                    if trade["state"] == "TRADE_STATE_BUSTED":
                        raise RuntimeError(
                            "A bot-market trade was busted; reconcile the live journal"
                        )
                elif activity["type"] == "ACTIVITY_TYPE_POSITION_RESOLUTION":
                    resolution = activity["positionResolution"]
                    if resolution["marketSlug"] != slug:
                        raise ValueError("Resolution contract mismatch")
                    resolutions.append(resolution)
            if page.get("eof") is True:
                break
            next_cursor = page.get("nextCursor")
            if not next_cursor or next_cursor == cursor:
                raise ValueError("Incomplete settlement activity pagination")
            cursor = next_cursor
        else:
            raise ValueError("Settlement activity pagination limit reached")
        if not resolutions:
            return None
        if len(resolutions) != 1:
            raise RuntimeError("Ambiguous account resolution history; reconcile the live journal")
        before, after = resolutions[0]["beforePosition"], resolutions[0]["afterPosition"]
        if position_quantity(before) != expected_quantity:
            raise RuntimeError(
                "Resolved quantity differs from bot ownership; reconcile the live journal"
            )
        if position_quantity(after) != 0:
            raise RuntimeError("Account resolution did not close the bot position")
        return resolutions[0]

    def confirm_resolution(self, slug: str, expected_quantity: Decimal) -> bool:
        """Require account activity proving ownership immediately before payout."""
        return self.resolution(slug, expected_quantity) is not None

    def _book(self, slug: str) -> Book:
        # The authenticated route serves current contracts even when the public
        # gateway returns a cached 404 for a newly opened measurement window.
        # A unique query keeps intermediary caches from replaying a 30s-old book;
        # the provider timestamp is still independently validated below.
        data = self._fresh_get(f"/v1/markets/{slug}/book")["marketData"]
        now = datetime.now(timezone.utc)
        if data["marketSlug"] != slug or data["state"] != "MARKET_STATE_OPEN":
            raise ValueError("Market book is not open or belongs to another contract")
        timestamp = parse_time(data["transactTime"])
        age = (now - timestamp).total_seconds()
        # Public REST books are cached for up to 30 seconds by the exchange.
        if not -5 <= age <= 35:
            raise ValueError("Market book timestamp is stale or in the future")
        bids = _levels(data["bids"], reverse=True)
        asks = _levels(data["offers"], reverse=False)
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            raise ValueError("Missing or crossed order book")
        return Book(slug, bids, asks, timestamp)

    def _bankroll(self, balance: dict, positions: dict) -> Decimal | None:
        # currentBalance is credited fiat, including short-sale proceeds. A short
        # is a signed YES liability, so adding positive NO value would count its
        # collateral twice. Pending credits/reservations are never added to cash.
        try:
            equity = number(balance["currentBalance"])
            for slug, position in positions.items():
                quantity = position_quantity(position)
                if quantity:
                    if position.get("expired"):
                        return None  # Wait for cash/quantity settlement reconciliation.
                    equity += quantity * self._book(slug).mid
            return equity if equity >= ZERO else None
        except (KeyError, ValueError, APIError, APIConnectionError, APITimeoutError):
            # An unavailable valuation must block entries without blocking exits.
            return None

    def _fresh_get(self, path: str, query: dict | None = None) -> dict:
        return self.client.get(
            path, query={**(query or {}), "_ts": str(time_ns())}, authenticated=True
        )


def parse_market(row: dict) -> Market:
    terms = row["assetPriceTerms"]
    if (
        terms["asset"]["symbol"] != "btc"
        or terms["horizon"] != "15m"
        or terms["marketType"] != "ASSET_PRICE_MARKET_TYPE_UP_DOWN"
    ):
        raise ValueError("Not a BTC 15-minute Up/Down contract")
    start, end = parse_time(terms["windowStart"]), parse_time(terms["windowEnd"])
    if end - start != timedelta(minutes=15):
        raise ValueError("Unexpected contract measurement window")
    sides = row["marketSides"]
    if len(sides) != 2 or {side["long"] for side in sides} != {True, False}:
        raise ValueError("Expected exactly one Yes and one No instrument")
    increment = number(row["minimumTradeQty"])
    tick = number(row["orderPriceMinTickSize"])
    coefficient = number(row["feeCoefficient"])
    if increment <= ZERO or not ZERO < tick < ONE or not ZERO <= coefficient <= ONE:
        raise ValueError("Invalid market quantity, tick, or fee terms")
    return Market(
        row["slug"],
        start,
        end,
        increment,
        tick,
        coefficient,
        row["active"] is True
        and row["closed"] is False
        and all(side["tradable"] is True for side in sides),
    )


def position_quantity(position: dict) -> Decimal:
    return number(position["netPositionDecimal"])


def order_fill(order: dict, plan: Plan) -> tuple[Decimal, Decimal, Decimal | None]:
    """Return terminal fills and actual collected fee, retaining unknown fees."""
    if order["marketSlug"] != plan.slug or order["intent"] != plan.request["intent"]:
        raise ValueError("Filled order contract or intent mismatch")
    if order["state"] not in TERMINAL_STATES:
        raise ValueError("Order is not terminal; fill accounting is not final")
    quantity = number(order["cumQuantity"])
    if not ZERO <= quantity <= plan.quantity:
        raise ValueError("Invalid cumulative fill quantity")
    if quantity == ZERO:
        return ZERO, ZERO, ZERO
    yes_price = amount(order["avgPx"])
    price = yes_price if plan.outcome == "up" else ONE - yes_price
    if not ZERO < price < ONE:
        raise ValueError("Invalid fill price")
    limit = plan.yes_price if plan.outcome == "up" else ONE - plan.yes_price
    if (plan.action == "BUY" and price > limit) or (plan.action == "SELL" and price < limit):
        raise ValueError("Fill violated the limit price")
    collected = order.get("commissionNotionalTotalCollected")
    fee = amount(collected) if collected is not None else None
    if fee is not None and fee < ZERO:
        raise ValueError("Unexpected negative taker fee")
    return quantity, quantity * price, fee


def number(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Expected a finite decimal") from None
    if not result.is_finite():
        raise ValueError("Expected a finite decimal")
    return result


def estimate_fee(coefficient: Decimal, quantity: Decimal, yes_price: Decimal) -> Decimal:
    """US taker fee; p=0.5 gives the conservative per-order bound."""
    return (coefficient * quantity * yes_price * (ONE - yes_price)).quantize(
        CENT, rounding=ROUND_HALF_EVEN
    )


def amount(value: dict) -> Decimal:
    if value["currency"] != "USD":
        raise ValueError("Expected a USD amount")
    return number(value["value"])


def parse_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Missing market timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Expected a timezone-aware timestamp")
    return result.astimezone(timezone.utc)


def _credential(config: dict, *names: str) -> str:
    values = {config[name].strip() for name in names if config.get(name)}
    if len(values) != 1:
        raise ValueError(f"Set one consistent credential using {' or '.join(names)}")
    return values.pop()


def _order_read_delay(error: Exception, attempt: int) -> float:
    delay = float(2**attempt)
    if isinstance(error, APIStatusError):
        header = error.response.headers.get("Retry-After", "")
        try:
            retry_after = float(header)
        except ValueError:
            try:
                retry_after = (
                    parsedate_to_datetime(header) - datetime.now(timezone.utc)
                ).total_seconds()
            except (ValueError, TypeError, OverflowError):
                return delay
        if retry_after >= 0:
            delay = max(delay, retry_after)
    return min(delay, 30.0)


def _levels(rows: list[dict], *, reverse: bool) -> tuple:
    levels = [(amount(row["px"]), number(row["qty"])) for row in rows]
    if any(not ZERO < price < ONE or quantity < ZERO for price, quantity in levels):
        raise ValueError("Invalid order book level")
    return tuple(sorted((level for level in levels if level[1] > ZERO), reverse=reverse))


def _validate_book(market: Market, book: Book) -> None:
    now = datetime.now(timezone.utc)
    if not market.tradable or book.slug != market.slug or not market.start <= now < market.end:
        raise ValueError("Contract is not currently tradable")
    if not -5 <= (now - book.timestamp).total_seconds() <= 35:
        raise ValueError("Order book is stale")


def _floor(value: Decimal, increment: Decimal) -> Decimal:
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def _plan(
    market: Market,
    book: Book,
    outcome: str,
    action: str,
    yes_price: Decimal,
    quantity: Decimal,
    fee: Decimal,
) -> Plan:
    if yes_price % market.tick:
        raise ValueError("Book price is not aligned to the market tick")
    intent = f"ORDER_INTENT_{action}_{'LONG' if outcome == 'up' else 'SHORT'}"
    request = {
        "marketSlug": market.slug,
        "intent": intent,
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": str(yes_price), "currency": "USD"},
        "quantity": float(quantity),
        "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        "synchronousExecution": True,
    }
    price = yes_price if outcome == "up" else ONE - yes_price
    expires = min(market.end, book.timestamp + timedelta(seconds=35))
    return Plan(
        market.slug, outcome, action, yes_price, quantity, quantity * price, fee, request, expires
    )


@dataclass(frozen=True)
class Market:
    slug: str
    start: datetime
    end: datetime
    increment: Decimal
    tick: Decimal
    fee_coefficient: Decimal
    tradable: bool


@dataclass(frozen=True)
class Book:
    slug: str
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    timestamp: datetime

    @property
    def mid(self) -> Decimal:
        return (self.bids[0][0] + self.asks[0][0]) / 2

    def signal_book(self) -> dict:
        return {
            "bids": [{"price": str(p), "size": str(q)} for p, q in self.bids],
            "asks": [{"price": str(p), "size": str(q)} for p, q in self.asks],
        }


@dataclass(frozen=True)
class Plan:
    slug: str
    outcome: str
    action: str
    yes_price: Decimal
    quantity: Decimal
    notional: Decimal
    fee_bound: Decimal
    request: dict
    expires_at: datetime

    @property
    def budget(self) -> Decimal:
        return self.notional + self.fee_bound


@dataclass(frozen=True)
class Account:
    buying_power: Decimal
    positions: dict
    orders: list[dict]
    bankroll: Decimal | None


ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")
TERMINAL_STATES = {
    "ORDER_STATE_FILLED",
    "ORDER_STATE_CANCELED",
    "ORDER_STATE_REJECTED",
    "ORDER_STATE_EXPIRED",
}


class OrderNotSubmitted(ValueError):
    """A plan expired before any order-create request was sent."""
