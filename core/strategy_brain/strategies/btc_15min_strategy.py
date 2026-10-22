"""The existing late-window BTC policy, driven entirely by US market books."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from time import monotonic

from loguru import logger

from core.strategy_brain.fusion_engine.signal_fusion import SignalFusionEngine
from core.strategy_brain.signal_processors.base_processor import SignalDirection
from core.strategy_brain.signal_processors.deribit_pcr_processor import DeribitPCRProcessor
from core.strategy_brain.signal_processors.divergence_processor import PriceDivergenceProcessor
from core.strategy_brain.signal_processors.orderbook_processor import OrderBookImbalanceProcessor
from core.strategy_brain.signal_processors.sentiment_processor import SentimentProcessor
from core.strategy_brain.signal_processors.spike_detector import SpikeDetectionProcessor
from core.strategy_brain.signal_processors.tick_velocity_processor import TickVelocityProcessor
from execution.polymarket_client import Book, Market


class BTCStrategy15Min:
    def __init__(self):
        self.entry_window = (780, 840)
        self.slug = None
        self.ticks = deque(maxlen=200)
        self.last_sample = None
        self.processors = [
            SpikeDetectionProcessor(),
            SentimentProcessor(),
            PriceDivergenceProcessor(),
            OrderBookImbalanceProcessor(),
            TickVelocityProcessor(),
            DeribitPCRProcessor(),
        ]
        self.fusion = SignalFusionEngine()
        for name, weight in {
            "OrderBookImbalance": 0.30,
            "TickVelocity": 0.25,
            "PriceDivergence": 0.18,
            "SpikeDetection": 0.12,
            "DeribitPCR": 0.10,
            "SentimentAnalysis": 0.05,
        }.items():
            self.fusion.set_weight(name, weight)

    def observe(self, book: Book) -> None:
        if self.slug != book.slug:
            self.slug = book.slug
            self.ticks.clear()
            self.last_sample = None
        # Sample the latest distinct snapshot at most every three seconds, with
        # no catch-up after slow ticks. Transport frequency must not
        # shorten the 20-observation warmup or count-based feature horizons.
        now = monotonic()
        if (self.last_sample is None or now - self.last_sample >= 3) and (
            not self.ticks or book.timestamp > self.ticks[-1]["ts"]
        ):
            self.ticks.append({"ts": book.timestamp, "price": book.mid})
            self.last_sample = now

    def decide(self, market: Market, book: Book, context: dict | None = None) -> str | None:
        self.observe(book)
        elapsed = (datetime.now(timezone.utc) - market.start).total_seconds()
        if not self.entry_window[0] <= elapsed < self.entry_window[1] or len(self.ticks) < 20:
            return None
        if book.mid > 0.60:
            outcome = "up"
        elif book.mid < 0.40:
            outcome = "down"
        else:
            return None
        prices = [tick["price"] for tick in self.ticks]
        metadata = {
            "observation_time": book.timestamp,
            "tick_buffer": list(self.ticks),
            "order_book": book.signal_book(),
            "momentum": float((prices[-1] - prices[-5]) / prices[-5]),
        }
        # One caller-selected snapshot is the only source of optional context.
        # Missing or expired inputs are omitted; processors never fetch them.
        context = context or {}
        if context.get("spot", {}).get("available"):
            metadata["spot_price"] = context["spot"]["value"]
            metadata["spot_timestamp"] = context["spot"].get("source_timestamp") or context[
                "spot"
            ].get("received_at")
        if context.get("sentiment", {}).get("available"):
            metadata.update(context["sentiment"]["value"])
        if context.get("options", {}).get("available"):
            metadata["options"] = context["options"]["value"]
        signals = []
        for processor in self.processors:
            signal = processor.process(book.mid, prices, metadata)
            if signal:
                signals.append(signal)
        fused = self.fusion.fuse_signals(signals, min_signals=1, min_score=50)
        expected = SignalDirection.BULLISH if outcome == "up" else SignalDirection.BEARISH
        if fused is None or fused.score <= 50 or fused.direction != expected:
            return None
        logger.info(
            "Decision: {} with {} signals (consensus {:.0f}, confidence {:.0%})",
            outcome,
            len(signals),
            fused.score,
            fused.confidence,
        )
        return outcome


BASELINE_POLICY_VERSION = "btc-15m-fusion-v2"
