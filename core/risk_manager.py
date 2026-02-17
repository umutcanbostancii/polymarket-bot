"""Risk management for BTC 5-min fast loop."""

import logging
import time
from typing import Tuple

log = logging.getLogger(__name__)


class RiskManager:

    def __init__(self, config, db):
        self.cfg = config
        self.db = db
        self._halted = False
        self._halt_reason = ""
        self._halt_time = 0.0

    @property
    def is_halted(self) -> bool:
        return self._halted

    def can_trade(self) -> Tuple[bool, str]:
        # Auto-resume check
        if self._halted and time.time() - self._halt_time > self.cfg.HALT_RESUME_SECONDS:
            self.resume()

        if self._halted:
            return False, f"HALTED: {self._halt_reason}"

        # Daily loss limit
        daily_pnl = self.db.daily_pnl()
        if daily_pnl < -self.cfg.MAX_DAILY_LOSS_USD:
            reason = f"Daily loss ${daily_pnl:.2f} > limit -${self.cfg.MAX_DAILY_LOSS_USD:.2f}"
            self.halt(reason)
            return False, reason

        # Consecutive losses (only count active strategy, not dead ones)
        losses = self.db.consecutive_losses_by_strategy("btc_5min_fast")
        if losses >= self.cfg.MAX_CONSECUTIVE_LOSSES:
            reason = f"{losses} consecutive losses (limit={self.cfg.MAX_CONSECUTIVE_LOSSES})"
            self.halt(reason)
            return False, reason

        # PDF s.90: max simultaneous positions
        max_pos = getattr(self.cfg, "MAX_SIMULTANEOUS_POSITIONS", 3)
        active = self.db.active_positions()
        if len(active) >= max_pos:
            return False, f"Max {max_pos} simultaneous positions ({len(active)} active)"

        return True, "OK"

    def position_size(self, edge: float, price: float, orderbook_depth_usd: float = 0.0) -> float:
        """PDF s.36,68: Modified Kelly position sizing for binary market.
        f = (b*p - q) / b * sqrt(p) — quarter-Kelly.
        Capped at 50% of orderbook depth if available."""
        if edge <= 0 or price <= 0 or price >= 1:
            return 0.0

        # Binary payout odds
        b = (1.0 - price) / price
        if b <= 0:
            return 0.0

        # Estimate win prob from edge + market price
        win_prob = price + edge
        win_prob = max(0.01, min(0.99, win_prob))
        q = 1 - win_prob

        # PDF modified Kelly: f = (b*p - q) / b * sqrt(p)
        import math
        full_kelly = (b * win_prob - q) / b * math.sqrt(win_prob)
        if full_kelly <= 0:
            return 0.0

        size = self.cfg.BANKROLL * full_kelly * self.cfg.KELLY_FRACTION
        # Cap at configured trade size
        size = min(size, self.cfg.TRADE_SIZE_USD)

        # PDF: Cap at 50% of orderbook depth
        if orderbook_depth_usd > 0:
            size = min(size, orderbook_depth_usd * 0.5)

        return round(size, 2) if size >= 1.0 else 0.0

    def halt(self, reason: str):
        self._halted = True
        self._halt_reason = reason
        self._halt_time = time.time()
        log.warning(f"RISK HALT: {reason}")

    def resume(self):
        self._halted = False
        self._halt_reason = ""
        self._halt_time = 0.0
        log.info("Trading resumed")

    def status(self) -> dict:
        return {
            "halted": self._halted,
            "reason": self._halt_reason,
            "halt_time": self._halt_time,
            "daily_pnl": self.db.daily_pnl(),
            "consecutive_losses": self.db.consecutive_losses(),
            "max_daily_loss": self.cfg.MAX_DAILY_LOSS_USD,
            "max_consecutive_losses": self.cfg.MAX_CONSECUTIVE_LOSSES,
            "active_positions": len(self.db.active_positions()),
            "max_simultaneous": getattr(self.cfg, "MAX_SIMULTANEOUS_POSITIONS", 3),
        }
