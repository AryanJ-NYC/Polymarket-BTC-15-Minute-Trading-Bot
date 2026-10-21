"""Heuristic signals from sampled contract prices.

The existing policy fades deviations from a 20-sample moving average and follows
three-sample price changes. These are count-based horizons, not fixed durations;
continuation and reversion are hypotheses, not established trading edges.
"""

from datetime import datetime
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


class SpikeDetectionProcessor(BaseSignalProcessor):
    """
    Detects price spikes in Polymarket UP probability.

    Two detection modes:
    1. MA DEVIATION: Price deviates >5% from 20-period MA → mean reversion
    2. VELOCITY SPIKE: Price moves >3% in last 3 ticks → momentum continuation

    Direction logic:
    - Deviation spike UP → expect reversion → BEARISH
    - Deviation spike DOWN → expect reversion → BULLISH
    - Velocity spike UP → momentum continuation → BULLISH
    - Velocity spike DOWN → momentum continuation → BEARISH
    """

    def __init__(
        self,
        spike_threshold: float = 0.05,  # FIXED: was 0.15, now 0.05 for probability prices
        lookback_periods: int = 20,
        min_confidence: float = 0.55,  # FIXED: was 0.60, slightly lower for more signals
        velocity_threshold: float = 0.03,  # 3% move in 3 ticks = velocity spike
    ):
        super().__init__("SpikeDetection")

        self.spike_threshold = spike_threshold
        self.lookback_periods = lookback_periods
        self.min_confidence = min_confidence
        self.velocity_threshold = velocity_threshold

        logger.info(
            f"Initialized Spike Detector (FIXED): "
            f"deviation_threshold={spike_threshold:.1%}, "
            f"velocity_threshold={velocity_threshold:.1%}, "
            f"lookback={lookback_periods}"
        )

    def process(
        self,
        current_price: Decimal,
        historical_prices: list,
        metadata: Dict[str, Any] = None,
    ) -> Optional[TradingSignal]:
        """
        Detect probability spikes and generate mean-reversion or momentum signals.
        """
        if not self.is_enabled:
            return None

        if len(historical_prices) < self.lookback_periods:
            return None

        # --- Compute 20-period MA ---
        recent = historical_prices[-self.lookback_periods :]
        ma = sum(float(p) for p in recent) / len(recent)
        curr = float(current_price)

        deviation = (curr - ma) / ma if ma > 0 else 0.0
        deviation_abs = abs(deviation)

        # --- Check velocity (last 3 ticks) ---
        velocity = 0.0
        if len(historical_prices) >= 3:
            prev3 = float(historical_prices[-3])
            velocity = (curr - prev3) / prev3 if prev3 > 0 else 0.0

        logger.debug(
            f"SpikeDetector: price={curr:.4f}, MA={ma:.4f}, "
            f"deviation={deviation:+.3%}, velocity={velocity:+.3%}"
        )

        # =====================================================================
        # SIGNAL 1: MA DEVIATION SPIKE → mean reversion
        # =====================================================================
        if deviation_abs >= self.spike_threshold:
            logger.info(
                f"MA deviation spike: {deviation:+.3%} from MA (${curr:.4f} vs MA={ma:.4f})"
            )

            # Fade the spike (mean reversion)
            direction = SignalDirection.BEARISH if deviation > 0 else SignalDirection.BULLISH
            target = Decimal(str(ma))

            # Strength by magnitude (calibrated for 0-1 probability prices)
            if deviation_abs >= 0.12:
                strength = SignalStrength.VERY_STRONG
            elif deviation_abs >= 0.08:
                strength = SignalStrength.STRONG
            elif deviation_abs >= 0.05:
                strength = SignalStrength.MODERATE
            else:
                strength = SignalStrength.WEAK

            confidence = min(0.90, 0.50 + (deviation_abs - self.spike_threshold) * 3.0)

            if confidence < self.min_confidence:
                return None

            stop_distance = abs(Decimal(str(curr)) - Decimal(str(ma))) * Decimal("1.5")
            stop_loss = (
                Decimal(str(curr)) + stop_distance
                if direction == SignalDirection.BEARISH
                else Decimal(str(curr)) - stop_distance
            )

            signal = TradingSignal(
                timestamp=datetime.now(),
                source=self.name,
                signal_type=SignalType.SPIKE_DETECTED,
                direction=direction,
                strength=strength,
                confidence=confidence,
                current_price=current_price,
                target_price=target,
                stop_loss=stop_loss,
                metadata={
                    "detection_mode": "ma_deviation",
                    "deviation_pct": deviation,
                    "moving_average": ma,
                    "velocity": velocity,
                    "spike_direction": "up" if deviation > 0 else "down",
                },
            )
            self._record_signal(signal)
            logger.info(
                f"Generated {direction.value.upper()} signal (MA deviation): "
                f"deviation={deviation:+.3%}, confidence={confidence:.2%}, "
                f"score={signal.score:.1f}"
            )
            return signal

        # =====================================================================
        # SIGNAL 2: VELOCITY SPIKE → short-term momentum continuation
        # Only fires when price is NOT already at an MA extreme (no double-signal)
        # =====================================================================
        if abs(velocity) >= self.velocity_threshold and deviation_abs < self.spike_threshold * 0.6:
            logger.info(f"Velocity spike: {velocity:+.3%} in last 3 ticks")

            # Momentum continuation (short-term, lower confidence)
            direction = SignalDirection.BULLISH if velocity > 0 else SignalDirection.BEARISH

            vel_strength = abs(velocity) / self.velocity_threshold
            if vel_strength >= 3:
                strength = SignalStrength.MODERATE
                confidence = 0.65
            elif vel_strength >= 2:
                strength = SignalStrength.WEAK
                confidence = 0.60
            else:
                strength = SignalStrength.WEAK
                confidence = 0.57

            if confidence < self.min_confidence:
                return None

            signal = TradingSignal(
                timestamp=datetime.now(),
                source=self.name,
                signal_type=SignalType.MOMENTUM,
                direction=direction,
                strength=strength,
                confidence=confidence,
                current_price=current_price,
                metadata={
                    "detection_mode": "velocity",
                    "velocity_pct": velocity,
                    "moving_average": ma,
                    "deviation_pct": deviation,
                },
            )
            self._record_signal(signal)
            logger.info(
                f"Generated {direction.value.upper()} signal (velocity): "
                f"velocity={velocity:+.3%}, confidence={confidence:.2%}"
            )
            return signal

        return None
