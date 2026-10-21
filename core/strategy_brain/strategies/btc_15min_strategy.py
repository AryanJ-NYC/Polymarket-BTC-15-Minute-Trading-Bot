"""The existing late-window BTC policy, driven entirely by US market books."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from time import monotonic

import httpx
from loguru import logger

from core.strategy_brain.fusion_engine.signal_fusion import SignalFusionEngine
from core.strategy_brain.signal_processors.deribit_pcr_processor import DeribitPCRProcessor
from core.strategy_brain.signal_processors.divergence_processor import PriceDivergenceProcessor
from core.strategy_brain.signal_processors.orderbook_processor import OrderBookImbalanceProcessor
from core.strategy_brain.signal_processors.sentiment_processor import SentimentProcessor
from core.strategy_brain.signal_processors.spike_detector import SpikeDetectionProcessor
from core.strategy_brain.signal_processors.tick_velocity_processor import TickVelocityProcessor
from execution.polymarket_client import Book, Market


class BTCStrategy15Min:
    def __init__(self):
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
        # no catch-up after slow context reads. Transport frequency must not
        # shorten the 20-observation warmup or count-based feature horizons.
        now = monotonic()
        if (self.last_sample is None or now - self.last_sample >= 3) and (
            not self.ticks or book.timestamp > self.ticks[-1]["ts"]
        ):
            self.ticks.append({"ts": book.timestamp, "price": book.mid})
            self.last_sample = now

    def decide(self, market: Market, book: Book) -> str | None:
        self.observe(book)
        elapsed = (datetime.now(timezone.utc) - market.start).total_seconds()
        if not 780 <= elapsed < 840 or len(self.ticks) < 20:
            return None
        if book.mid > 0.60:
            outcome = "up"
        elif book.mid < 0.40:
            outcome = "down"
        else:
            return None
        prices = [tick["price"] for tick in self.ticks]
        metadata = {
            "tick_buffer": list(self.ticks),
            "order_book": book.signal_book(),
            "momentum": float((prices[-1] - prices[-5]) / prices[-5]),
        }
        # External context is optional; a failed source never invents an input.
        with httpx.Client(timeout=5) as http:
            try:
                response = http.get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
                response.raise_for_status()
                metadata["spot_price"] = float(response.json()["data"]["amount"])
            except (httpx.HTTPError, KeyError, ValueError):
                logger.warning("Coinbase context unavailable")
            try:
                response = http.get("https://api.alternative.me/fng/", params={"limit": 1})
                response.raise_for_status()
                metadata["sentiment_score"] = float(response.json()["data"][0]["value"])
            except (httpx.HTTPError, KeyError, ValueError, IndexError):
                logger.warning("Sentiment context unavailable")
        signals = []
        for processor in self.processors:
            signal = processor.process(book.mid, prices, metadata)
            if signal:
                signals.append(signal)
        fused = self.fusion.fuse_signals(signals, min_signals=1, min_score=40)
        if fused is None:
            return None
        logger.info(
            "Decision: {} with {} signals (consensus {:.0f}, confidence {:.0%})",
            outcome,
            len(signals),
            fused.score,
            fused.confidence,
        )
        return outcome
