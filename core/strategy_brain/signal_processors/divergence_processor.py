"""Heuristic spot/contract momentum and extreme-price fade signals.

Spot returns use distinct timestamped observations over 30 seconds (within 5s).
When spot is unavailable, the existing five-sample contract return is used.
Neither these heuristics nor the market midpoint is a calibrated probability.
"""

from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

from loguru import logger

from core.strategy_brain.signal_processors.base_processor import (
    BaseSignalProcessor,
    SignalDirection,
    SignalStrength,
    SignalType,
    TradingSignal,
)


class PriceDivergenceProcessor(BaseSignalProcessor):
    """Apply the existing momentum and extreme-price thresholds."""

    def __init__(
        self,
        divergence_threshold: float = 0.05,  # kept for API compatibility (unused)
        min_confidence: float = 0.55,
        momentum_threshold: float = 0.003,  # 0.3% spot move = meaningful momentum
        extreme_prob_threshold: float = 0.68,  # above this → fade to Down
        low_prob_threshold: float = 0.32,  # below this → fade to Up
    ):
        super().__init__("PriceDivergence")

        self.min_confidence = min_confidence
        self.momentum_threshold = momentum_threshold
        self.extreme_prob_threshold = extreme_prob_threshold
        self.low_prob_threshold = low_prob_threshold

        # At most 20 distinct observations, pruned to the latest 60 seconds.
        self._spot_history = deque(maxlen=20)

        logger.info(
            f"Initialized Price Divergence Processor (FIXED): "
            f"momentum_thresh={momentum_threshold:.1%}, "
            f"extreme_fade={extreme_prob_threshold:.0%}/{low_prob_threshold:.0%}"
        )

    def process(
        self,
        current_price: Decimal,  # Polymarket UP probability (0.0–1.0)
        historical_prices: list,
        metadata: Dict[str, Any] = None,
    ) -> Optional[TradingSignal]:
        """
        Generate signal from spot momentum vs Polymarket probability.

        metadata keys used:
          - spot_price (float): current Coinbase BTC-USD price
          - spot_timestamp (str): source time, or receipt time when absent
          - momentum (float): return across the latest five contract samples
        """
        if not self.is_enabled:
            return None

        if not metadata:
            return None

        poly_prob = float(current_price)  # Quoted YES midpoint, not a model forecast.

        spot_price = metadata.get("spot_price")
        poly_momentum = float(metadata.get("momentum", 0.0))

        if spot_price is not None:
            spot_momentum = self._spot_momentum(spot_price, metadata.get("spot_timestamp"))
            if spot_momentum is None:
                return None  # Timestamped spot history is still warming up.
        else:
            spot_momentum = poly_momentum

        logger.info(
            f"PriceDivergence: poly_prob={poly_prob:.3f}, "
            f"spot_momentum={spot_momentum:+.4f} ({spot_momentum * 100:+.2f}%), "
            f"spot_price={'${:,.2f}'.format(spot_price) if spot_price else 'N/A'}"
        )

        # =====================================================================
        # SIGNAL 1: EXTREME PROBABILITY FADE
        # If the market has already priced in a strong move, fade it.
        # =====================================================================
        if poly_prob >= self.extreme_prob_threshold:
            # Existing contrarian heuristic; late-window reversion is unproven.
            if spot_momentum <= 0.001:  # momentum not strongly confirming Up
                extremeness = (poly_prob - self.extreme_prob_threshold) / (
                    1.0 - self.extreme_prob_threshold
                )
                confidence = min(0.80, self.min_confidence + extremeness * 0.25)
                strength = SignalStrength.STRONG if extremeness > 0.5 else SignalStrength.MODERATE

                signal = TradingSignal(
                    timestamp=datetime.now(),
                    source=self.name,
                    signal_type=SignalType.PRICE_DIVERGENCE,
                    direction=SignalDirection.BEARISH,
                    strength=strength,
                    confidence=confidence,
                    current_price=current_price,
                    metadata={
                        "signal_type": "extreme_prob_fade_down",
                        "poly_prob": poly_prob,
                        "spot_momentum": spot_momentum,
                        "extremeness": extremeness,
                    },
                )
                self._record_signal(signal)
                logger.info(
                    f"Generated BEARISH fade signal: poly Up prob too high "
                    f"({poly_prob:.0%}) with weak momentum → fade DOWN, "
                    f"confidence={confidence:.2%}"
                )
                return signal

        elif poly_prob <= self.low_prob_threshold:
            # Market >68% confident BTC goes Down — fade to UP unless momentum confirms
            if spot_momentum >= -0.001:  # not strongly falling
                extremeness = (self.low_prob_threshold - poly_prob) / self.low_prob_threshold
                confidence = min(0.80, self.min_confidence + extremeness * 0.25)
                strength = SignalStrength.STRONG if extremeness > 0.5 else SignalStrength.MODERATE

                signal = TradingSignal(
                    timestamp=datetime.now(),
                    source=self.name,
                    signal_type=SignalType.PRICE_DIVERGENCE,
                    direction=SignalDirection.BULLISH,
                    strength=strength,
                    confidence=confidence,
                    current_price=current_price,
                    metadata={
                        "signal_type": "extreme_prob_fade_up",
                        "poly_prob": poly_prob,
                        "spot_momentum": spot_momentum,
                        "extremeness": extremeness,
                    },
                )
                self._record_signal(signal)
                logger.info(
                    f"Generated BULLISH fade signal: poly Down prob too high "
                    f"({1 - poly_prob:.0%}) with weak negative momentum → fade UP, "
                    f"confidence={confidence:.2%}"
                )
                return signal

        # =====================================================================
        # SIGNAL 2: MOMENTUM MISPRICING
        # Polymarket probability near 50% but spot has strong directional move.
        # This is a directional heuristic, not evidence of mispricing.
        # Only fires when poly_prob is between 35-65% (no strong lean already).
        # =====================================================================
        if 0.35 <= poly_prob <= 0.65 and abs(spot_momentum) >= self.momentum_threshold:
            # Apply the existing momentum threshold in the central price band.
            momentum_strength = abs(spot_momentum) / self.momentum_threshold  # multiplier
            confidence = min(0.78, 0.55 + min(momentum_strength - 1, 2) * 0.08)

            if momentum_strength >= 3:
                strength = SignalStrength.STRONG
            elif momentum_strength >= 2:
                strength = SignalStrength.MODERATE
            else:
                strength = SignalStrength.WEAK

            if confidence < self.min_confidence:
                return None

            direction = SignalDirection.BULLISH if spot_momentum > 0 else SignalDirection.BEARISH

            signal = TradingSignal(
                timestamp=datetime.now(),
                source=self.name,
                signal_type=SignalType.PRICE_DIVERGENCE,
                direction=direction,
                strength=strength,
                confidence=confidence,
                current_price=current_price,
                metadata={
                    "signal_type": "momentum_mispricing",
                    "poly_prob": poly_prob,
                    "spot_momentum": spot_momentum,
                    "momentum_strength": momentum_strength,
                },
            )
            self._record_signal(signal)
            logger.info(
                f"Generated {direction.value.upper()} momentum signal: "
                f"spot moved {spot_momentum:+.3%} but poly still at {poly_prob:.0%}, "
                f"confidence={confidence:.2%}"
            )
            return signal

        logger.debug(
            f"PriceDivergence: no signal — prob={poly_prob:.2f}, "
            f"momentum={spot_momentum:+.4f} (below threshold or already priced in)"
        )
        return None

    def _spot_momentum(self, price: float, timestamp: str | None) -> float | None:
        """Return the spot change over 30s (nearest observation within 5s)."""
        if timestamp is None:
            return None
        observed = datetime.fromisoformat(timestamp)
        if self._spot_history and observed < self._spot_history[-1][0]:
            return None
        while self._spot_history and (observed - self._spot_history[0][0]).total_seconds() > 60:
            self._spot_history.popleft()
        if not self._spot_history or observed > self._spot_history[-1][0]:
            self._spot_history.append((observed, float(price)))
        target = observed - timedelta(seconds=30)
        previous_time, previous_price = min(
            self._spot_history, key=lambda item: abs(item[0] - target)
        )
        if abs((previous_time - target).total_seconds()) > 5:
            return None
        return (float(price) - previous_price) / previous_price
