"""
Deribit Put/Call Ratio Signal Processor
Consumes refreshed BTC options data from Deribit (free public API)
and uses the put/call ratio as a proxy for institutional sentiment.

WHY THIS WORKS:
  Professional traders hedge and speculate using BTC options on Deribit —
  the world's largest crypto options exchange ($15B+ daily volume).

  Put/Call Ratio (PCR) = open_interest_puts / open_interest_calls

  PCR interpretation (CONTRARIAN — options markets lean the opposite):
    PCR > 1.2  → More puts than calls = FEAR → contrarian BULLISH
    PCR < 0.7  → More calls than puts = GREED → contrarian BEARISH
    0.7–1.2    → Balanced → no strong signal

  We look specifically at SHORT-DATED options (0-1 days to expiry)
  which are most sensitive to near-term price movements — ideal for
  15-minute trading.

  We also track the 25-delta skew (difference between put and call IV)
  as a secondary signal:
    Positive skew (puts more expensive) → fear → contrarian BULLISH
    Negative skew (calls more expensive) → greed → contrarian BEARISH

API USED (completely free, no auth required):
  GET https://www.deribit.com/api/v2/public/get_book_summary_by_currency
    ?currency=BTC&kind=option

  Returns all active BTC option contracts with:
    - open_interest
    - instrument_name (e.g. BTC-20FEB26-95000-P = Put, -C = Call)
    - days to expiry (parsed from instrument name)
"""

import math
from datetime import datetime, timezone
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


class DeribitPCRProcessor(BaseSignalProcessor):
    """Generate the existing contrarian signal from a selected options snapshot."""

    def __init__(
        self,
        bullish_pcr_threshold: float = 1.20,
        bearish_pcr_threshold: float = 0.70,
        min_confidence: float = 0.55,
    ):
        super().__init__("DeribitPCR")
        self.bullish_pcr_threshold = bullish_pcr_threshold
        self.bearish_pcr_threshold = bearish_pcr_threshold
        self.min_confidence = min_confidence

    def process(
        self,
        current_price: Decimal,
        historical_prices: list,
        metadata: Dict[str, Any] = None,
    ) -> Optional[TradingSignal]:
        """Read the already selected optional input; never perform HTTP or wait."""
        if not self.is_enabled or not metadata:
            return None
        pcr_data = metadata.get("options")
        if not isinstance(pcr_data, dict):
            return None
        try:
            for key in ("overall_pcr", "short_pcr", "short_put_oi", "short_call_oi"):
                if not isinstance(pcr_data[key], (int, float)):
                    return None
                _nonnegative_number(pcr_data[key])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        return self._generate_signal(current_price, pcr_data)

    def _generate_signal(
        self,
        current_price: Decimal,
        pcr_data: Dict,
    ) -> Optional[TradingSignal]:
        """Generate signal from PCR data."""
        # Prefer short-dated PCR; fall back to overall
        pcr = pcr_data.get("short_pcr") or pcr_data.get("overall_pcr", 1.0)

        if pcr >= self.bullish_pcr_threshold:
            # High PCR = excessive put buying = FEAR = contrarian BULLISH
            direction = SignalDirection.BULLISH
            extremeness = (pcr - self.bullish_pcr_threshold) / self.bullish_pcr_threshold
            confidence = min(0.80, 0.57 + extremeness * 0.15)

            if pcr >= 1.60:
                strength = SignalStrength.VERY_STRONG
            elif pcr >= 1.40:
                strength = SignalStrength.STRONG
            else:
                strength = SignalStrength.MODERATE

            logger.info(
                f"DeribitPCR: HIGH PCR={pcr:.3f} (excessive puts = fear) → contrarian BULLISH"
            )

        elif pcr <= self.bearish_pcr_threshold:
            # Low PCR = excessive call buying = GREED = contrarian BEARISH
            direction = SignalDirection.BEARISH
            extremeness = (self.bearish_pcr_threshold - pcr) / self.bearish_pcr_threshold
            confidence = min(0.80, 0.57 + extremeness * 0.15)

            if pcr <= 0.45:
                strength = SignalStrength.VERY_STRONG
            elif pcr <= 0.55:
                strength = SignalStrength.STRONG
            else:
                strength = SignalStrength.MODERATE

            logger.info(
                f"DeribitPCR: LOW PCR={pcr:.3f} (excessive calls = greed) → contrarian BEARISH"
            )

        else:
            logger.debug(
                f"DeribitPCR: balanced PCR={pcr:.3f} "
                f"(range {self.bearish_pcr_threshold}–{self.bullish_pcr_threshold}) — no signal"
            )
            return None

        if confidence < self.min_confidence:
            return None

        signal = TradingSignal(
            timestamp=datetime.now(),
            source=self.name,
            signal_type=SignalType.SENTIMENT_SHIFT,
            direction=direction,
            strength=strength,
            confidence=confidence,
            current_price=current_price,
            metadata={
                "pcr": round(pcr, 4),
                "overall_pcr": pcr_data.get("overall_pcr"),
                "short_put_oi": pcr_data.get("short_put_oi"),
                "short_call_oi": pcr_data.get("short_call_oi"),
                "interpretation": (
                    "excessive_puts_fear"
                    if direction == SignalDirection.BULLISH
                    else "excessive_calls_greed"
                ),
            },
        )

        self._record_signal(signal)

        logger.info(
            f"Generated {direction.value.upper()} signal (DeribitPCR): "
            f"PCR={pcr:.3f}, confidence={confidence:.2%}, score={signal.score:.1f}"
        )

        return signal


def parse_options(
    data: dict,
    *,
    max_days_to_expiry: int = 2,
    min_open_interest: float = 100.0,
    received_at: Optional[datetime] = None,
) -> dict:
    """Validate and aggregate provider summaries with the existing PCR calculations."""
    summaries = data["result"]
    if not isinstance(summaries, list) or not summaries:
        raise ValueError("empty options")
    now = received_at or datetime.now(timezone.utc)
    put_oi = call_oi = short_put_oi = short_call_oi = 0.0
    for item in summaries:
        name = item["instrument_name"]
        if not isinstance(name, str):
            raise ValueError("invalid option instrument")
        parts = name.split("-")
        if len(parts) != 4 or parts[0] != "BTC" or parts[3] not in ("P", "C"):
            raise ValueError("invalid option instrument")
        if _nonnegative_number(parts[2]) <= 0:
            raise ValueError("invalid option strike")
        expiry = datetime.strptime(parts[1], "%d%b%y").replace(tzinfo=timezone.utc)
        dte = max(0, (expiry - now).days)
        oi = _nonnegative_number(item["open_interest"])
        if oi < min_open_interest:
            continue
        if parts[3] == "P":
            put_oi += oi
            if dte <= max_days_to_expiry:
                short_put_oi += oi
        else:
            call_oi += oi
            if dte <= max_days_to_expiry:
                short_call_oi += oi
    for value in (put_oi, call_oi, short_put_oi, short_call_oi):
        _nonnegative_number(value)
    overall_pcr = put_oi / call_oi if call_oi > 0 else 1.0
    short_pcr = short_put_oi / short_call_oi if short_call_oi > 0 else overall_pcr
    _nonnegative_number(overall_pcr)
    _nonnegative_number(short_pcr)
    return {
        "overall_pcr": round(overall_pcr, 4),
        "short_pcr": round(short_pcr, 4),
        "put_oi": round(put_oi, 2),
        "call_oi": round(call_oi, 2),
        "short_put_oi": round(short_put_oi, 2),
        "short_call_oi": round(short_call_oi, 2),
        "total_contracts": len(summaries),
        "fetched_at": now.isoformat(),
    }


def _nonnegative_number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("invalid number")
    return result
