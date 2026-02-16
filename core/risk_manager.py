"""Risk management -- Kelly sizing, daily limit, cooldown"""

import logging
import time
from typing import Dict, Tuple

log = logging.getLogger(__name__)


class RiskManager:

    def __init__(self, config, db):
        self.cfg = config
        self.db = db
        self._strategy_halts: Dict[str, dict] = {}

    @property
    def is_halted(self) -> bool:
        return any(v.get("halted", False) for v in self._strategy_halts.values())

    def can_trade(self) -> tuple:
        # Backward-compatible global check.
        return self.can_trade_strategy("__global__")

    def can_trade_strategy(self, strategy: str) -> Tuple[bool, str]:
        state = self._state(strategy)

        # Auto-resume check
        if state["halted"] and time.time() - state["halt_time"] > self.cfg.HALT_RESUME_SECONDS:
            self.resume_strategy(strategy)

        if state["halted"]:
            return False, f"HALTED[{strategy}]: {state['reason']}"

        bankroll, max_daily_loss_pct = self._profile(strategy)

        if strategy == "__global__":
            daily = self.db.daily_pnl()
            losses = self.db.consecutive_losses()
        else:
            daily = self.db.daily_pnl_by_strategy(strategy)
            losses = self.db.consecutive_losses_by_strategy(strategy)

        limit = bankroll * max_daily_loss_pct
        if daily < -limit:
            reason = f"Daily loss ${daily:.2f} > limit -${limit:.2f}"
            self.halt_strategy(strategy, reason)
            return False, reason

        if losses >= self.cfg.COOLDOWN_AFTER_LOSSES:
            return False, f"Cooldown[{strategy}]: {losses} consecutive losses"

        return True, "OK"

    def _profile(self, strategy: str) -> Tuple[float, float]:
        if strategy in ("binance_spot_margin_core", "binance_spot"):
            return self.cfg.SPOT_BANKROLL, self.cfg.SPOT_MAX_DAILY_LOSS_PCT

        # Polymarket + legacy momentum fall back to main bankroll profile.
        return self.cfg.BANKROLL, self.cfg.MAX_DAILY_LOSS_PCT

    def _state(self, strategy: str) -> dict:
        if strategy not in self._strategy_halts:
            self._strategy_halts[strategy] = {
                "halted": False,
                "reason": "",
                "halt_time": 0.0,
            }
        return self._strategy_halts[strategy]

    def kelly_size(self, price: float, win_prob: float) -> float:
        """Quarter Kelly position sizing using Polymarket binary odds"""
        if price <= 0 or price >= 1 or win_prob <= 0 or win_prob >= 1:
            return 0.0

        b = (1.0 - price) / price  # binary market payout odds
        if b <= 0:
            return 0.0

        q = 1 - win_prob
        full = (b * win_prob - q) / b
        if full <= 0:
            return 0.0

        frac = full * self.cfg.KELLY_FRACTION
        size = min(self.cfg.BANKROLL * frac, self.cfg.BANKROLL * self.cfg.MAX_POSITION_PCT)
        return round(size, 2) if size >= 1.0 else 0.0

    def arb_size(self, edge: float) -> float:
        """Sum-to-one arbitraj sizing (daha agresif)"""
        if edge <= 0:
            return 0.0
        pct = min(edge * 2, 0.20)
        size = self.cfg.BANKROLL * pct
        return round(size, 2) if size >= 1.0 else 0.0

    def halt(self, reason: str):
        self.halt_strategy("__global__", reason)

    def halt_strategy(self, strategy: str, reason: str):
        state = self._state(strategy)
        state["halted"] = True
        state["reason"] = reason
        state["halt_time"] = time.time()
        log.warning(f"🛑 [{strategy}] {reason}")

    def resume(self):
        self.resume_strategy("__global__")

    def resume_strategy(self, strategy: str):
        state = self._state(strategy)
        state["halted"] = False
        state["reason"] = ""
        state["halt_time"] = 0.0
        log.info(f"✅ Trading resumed [{strategy}]")

    def status(self) -> dict:
        """Return current risk manager status."""
        global_state = self._state("__global__")
        return {
            "halted": self.is_halted,
            "reason": global_state["reason"],
            "halt_time": global_state["halt_time"],
            "strategies": self._strategy_halts,
        }
