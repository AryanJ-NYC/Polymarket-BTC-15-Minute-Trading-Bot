"""Order-flow signals from the same validated US book used for execution."""

from datetime import datetime

from loguru import logger

from core.strategy_brain.signal_processors.base_processor import (
    BaseSignalProcessor,
    SignalDirection,
    SignalStrength,
    SignalType,
    TradingSignal,
)


class OrderBookImbalanceProcessor(BaseSignalProcessor):
    def __init__(self):
        super().__init__("OrderBookImbalance")

    def process(self, current_price, historical_prices, metadata=None):
        if not self.is_enabled or not metadata or not metadata.get("order_book"):
            return None
        book = metadata["order_book"]
        bids, asks = book["bids"][:10], book["asks"][:10]
        bid_volume = sum(float(level["price"]) * float(level["size"]) for level in bids)
        ask_volume = sum(float(level["price"]) * float(level["size"]) for level in asks)
        total = bid_volume + ask_volume
        if total < 50:
            return None
        imbalance = (bid_volume - ask_volume) / total
        magnitude = abs(imbalance)
        if magnitude < 0.30:
            return None
        direction = SignalDirection.BULLISH if imbalance > 0 else SignalDirection.BEARISH
        strength = (
            SignalStrength.VERY_STRONG
            if magnitude >= 0.70
            else SignalStrength.STRONG
            if magnitude >= 0.50
            else SignalStrength.MODERATE
            if magnitude >= 0.35
            else SignalStrength.WEAK
        )
        wall_levels = bids if imbalance > 0 else asks
        wall = any(
            float(level["price"]) * float(level["size"]) / total >= 0.20 for level in wall_levels
        )
        confidence = min(0.90, min(0.85, 0.55 + magnitude * 0.40) + (0.05 if wall else 0))
        signal = TradingSignal(
            timestamp=datetime.now(),
            source=self.name,
            signal_type=SignalType.VOLUME_SURGE,
            direction=direction,
            strength=strength,
            confidence=confidence,
            current_price=current_price,
            metadata={"imbalance": imbalance},
        )
        self._record_signal(signal)
        logger.debug("Order book imbalance: {:.3f}", imbalance)
        return signal
