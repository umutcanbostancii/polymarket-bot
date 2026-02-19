"""Signal aggregation: weighted combination of multiple signal sources."""

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class SignalResult:
    """Single signal source result."""
    name: str
    direction: str  # "up", "down", "neutral"
    score: float    # -1.0 to +1.0
    confidence: float = 1.0  # 0.0 to 1.0


@dataclass
class AggregatedSignal:
    """Combined signal from all sources."""
    direction: str          # "up", "down", "neutral"
    composite_score: float  # -1.0 to +1.0
    agreement_ratio: float  # 0.0 to 1.0
    should_trade: bool
    action: str             # "ENTER", "WEAK_ENTER", "SKIP"
    signals: list = field(default_factory=list)
    details: str = ""


class SignalAggregator:

    DEFAULT_WEIGHTS = {
        "candle": 0.30,
        "sentiment": 0.25,
        "delta": 0.20,
        "momentum": 0.10,
        "technical": 0.10,
        "ai": 0.05,
    }

    def __init__(self, config):
        self.cfg = config
        self.weights = {
            "candle": getattr(config, "SIGNAL_WEIGHT_CANDLE", 0.30),
            "sentiment": getattr(config, "SIGNAL_WEIGHT_SENTIMENT", 0.25),
            "delta": getattr(config, "SIGNAL_WEIGHT_DELTA", 0.20),
            "momentum": getattr(config, "SIGNAL_WEIGHT_MOMENTUM", 0.10),
            "technical": getattr(config, "SIGNAL_WEIGHT_TECHNICAL", 0.10),
            "ai": getattr(config, "SIGNAL_WEIGHT_AI", 0.05),
        }
        self.score_threshold = getattr(config, "SIGNAL_SCORE_THRESHOLD", 0.15)
        self.min_agreement = getattr(config, "SIGNAL_MIN_AGREEMENT", 0.60)

    def update_weights(self, new_weights: dict):
        """Update signal weights (e.g. from feedback system)."""
        for k, v in new_weights.items():
            if k in self.weights:
                self.weights[k] = v
        # Normalize to sum to 1.0
        total = sum(self.weights.values())
        if total > 0:
            for k in self.weights:
                self.weights[k] /= total

    def aggregate(self, signals: list[SignalResult]) -> AggregatedSignal:
        """Agirlikli skor + agreement kontrolu."""
        if not signals:
            return AggregatedSignal(
                direction="neutral",
                composite_score=0.0,
                agreement_ratio=0.0,
                should_trade=False,
                action="SKIP",
                details="no signals",
            )

        # Calculate weighted composite score
        weighted_sum = 0.0
        weight_total = 0.0

        for sig in signals:
            w = self.weights.get(sig.name, 0.05)
            weighted_sum += sig.score * sig.confidence * w
            weight_total += w

        if weight_total > 0:
            composite = weighted_sum / weight_total
        else:
            composite = 0.0

        composite = max(-1.0, min(1.0, composite))

        # Determine dominant direction
        if composite > 0.02:
            dominant = "up"
        elif composite < -0.02:
            dominant = "down"
        else:
            dominant = "neutral"

        # Agreement: how many signals agree with dominant direction
        non_neutral = [s for s in signals if s.direction != "neutral"]
        if non_neutral:
            agreeing = sum(1 for s in non_neutral if s.direction == dominant)
            agreement = agreeing / len(non_neutral)
        else:
            agreement = 0.0

        # Decision
        abs_score = abs(composite)
        if abs_score > 0.4 and agreement > self.min_agreement:
            action = "ENTER"
            should_trade = True
        elif abs_score > self.score_threshold and agreement > self.min_agreement:
            action = "WEAK_ENTER"
            should_trade = True
        else:
            action = "SKIP"
            should_trade = False

        details = (
            f"composite={composite:.4f} agreement={agreement:.2f} "
            f"action={action} signals={len(signals)}"
        )

        return AggregatedSignal(
            direction=dominant,
            composite_score=round(composite, 4),
            agreement_ratio=round(agreement, 2),
            should_trade=should_trade,
            action=action,
            signals=[
                {"name": s.name, "direction": s.direction, "score": s.score}
                for s in signals
            ],
            details=details,
        )
