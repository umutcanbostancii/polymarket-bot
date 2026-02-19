"""Risk management for BTC 5-min fast loop."""

import logging
import time
from typing import Tuple

log = logging.getLogger(__name__)


class RiskManager:

    # Test stratejileri — risk hesaplamasina dahil edilmez
    _TEST_STRATEGIES = {"btc_5min_test", "btc_15min_test"}

    def __init__(self, config, db):
        self.cfg = config
        self.db = db
        self._halted = False
        self._halt_reason = ""
        self._halt_time = 0.0
        self._consec_warned = 0  # son uyari verilen ardisik kayip sayisi

    @property
    def is_halted(self) -> bool:
        return self._halted

    def can_trade(self) -> Tuple[bool, str]:
        # Auto-resume check
        if self._halted and time.time() - self._halt_time > self.cfg.HALT_RESUME_SECONDS:
            self.resume()

        if self._halted:
            return False, f"HALTED: {self._halt_reason}"

        # Daily loss limit — test stratejileri haric
        daily_pnl = self._daily_pnl_excl_test()
        if daily_pnl < -self.cfg.MAX_DAILY_LOSS_USD:
            reason = f"Daily loss ${daily_pnl:.2f} > limit -${self.cfg.MAX_DAILY_LOSS_USD:.2f}"
            self.halt(reason)
            return False, reason

        # Consecutive losses (5m + 15m birlikte)
        losses = self.db.consecutive_losses_by_strategy(("btc_5min_fast", "btc_15min"))
        if losses >= self.cfg.MAX_CONSECUTIVE_LOSSES:
            if getattr(self.cfg, "HALT_ON_CONSECUTIVE_LOSSES", True):
                reason = (
                    f"Consecutive losses {losses} >= limit {self.cfg.MAX_CONSECUTIVE_LOSSES}"
                )
                self.halt(reason)
                return False, reason

            if not self._consec_warned or losses > self._consec_warned:
                log.warning(
                    f"⚠ CONSECUTIVE LOSS WARNING: {losses} ardisik kayip! "
                    f"(limit={self.cfg.MAX_CONSECUTIVE_LOSSES}) — devam ediliyor"
                )
                self._consec_warned = losses
        elif losses == 0:
            self._consec_warned = 0

        # PDF s.90: max simultaneous positions — test stratejileri haric
        max_pos = getattr(self.cfg, "MAX_SIMULTANEOUS_POSITIONS", 3)
        active = [
            p for p in self.db.active_positions()
            if p.get("strategy") not in self._TEST_STRATEGIES
        ]
        if len(active) >= max_pos:
            return False, f"Max {max_pos} simultaneous positions ({len(active)} active)"

        return True, "OK"

    def _daily_pnl_excl_test(self) -> float:
        """Test stratejileri haric gunluk PnL."""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            r = self.db.conn.execute(
                """SELECT COALESCE(SUM(pnl),0) FROM trades
                   WHERE DATE(ts)=? AND status='resolved'
                     AND (strategy IS NULL OR strategy NOT IN ('btc_5min_test','btc_15min_test'))""",
                (today,),
            ).fetchone()
            return r[0] if r else 0.0
        except Exception:
            return self.db.daily_pnl()

    def position_size(self, edge: float, price: float, orderbook_depth_usd: float = 0.0) -> float:
        """Kelly-gated dynamic sizing. Kelly = go/no-go + scale factor."""
        if edge <= 0 or price <= 0 or price >= 1:
            return 0.0

        b = (1.0 - price) / price
        if b <= 0:
            return 0.0

        win_prob = max(0.01, min(0.99, price + edge))
        q = 1 - win_prob

        import math
        full_kelly = (b * win_prob - q) / b * math.sqrt(win_prob)
        if full_kelly <= 0:
            return 0.0

        # Kelly'yi scale factor olarak kullan (0.05 = tam boy)
        kelly_scale = min(1.0, full_kelly / 0.05)
        size = self.cfg.TRADE_SIZE_USD * kelly_scale

        # Min $3 (Polymarket 5 share * ~$0.50 = $2.50 alt sinir)
        if size < 3.0:
            return 0.0

        size = min(size, self.cfg.TRADE_SIZE_USD)
        size = min(size, self.cfg.BANKROLL * 0.25)  # Bakiyenin max %25'i

        if orderbook_depth_usd > 0:
            size = min(size, orderbook_depth_usd * 0.5)

        return round(size, 2)

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
        active = [
            p for p in self.db.active_positions()
            if p.get("strategy") not in self._TEST_STRATEGIES
        ]
        return {
            "halted": self._halted,
            "reason": self._halt_reason,
            "halt_time": self._halt_time,
            "daily_pnl": self._daily_pnl_excl_test(),
            "consecutive_losses": self.db.consecutive_losses_by_strategy(
                ("btc_5min_fast", "btc_15min")
            ),
            "max_daily_loss": self.cfg.MAX_DAILY_LOSS_USD,
            "max_consecutive_losses": self.cfg.MAX_CONSECUTIVE_LOSSES,
            "active_positions": len(active),
            "max_simultaneous": getattr(self.cfg, "MAX_SIMULTANEOUS_POSITIONS", 3),
        }
