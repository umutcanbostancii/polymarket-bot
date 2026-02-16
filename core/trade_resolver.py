"""Triple-barrier trade resolver with mandatory exit reason and cost attribution."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from signals.technical import macd

log = logging.getLogger(__name__)


class TradeResolver:

    def __init__(self, config, db, poly, binance, strategies=None):
        self.cfg = config
        self.db = db
        self.poly = poly
        self.binance = binance
        self._strategies = strategies or []

    async def run(self):
        interval = max(5, int(getattr(self.cfg, "RESOLVER_INTERVAL_SECONDS", 20)))
        while True:
            await asyncio.sleep(interval)
            try:
                await self._resolve_cycle()
            except Exception as exc:
                log.error(f"TradeResolver error: {exc}", exc_info=True)

    async def _resolve_cycle(self):
        positions = self.db.active_positions()
        if not positions:
            return

        needs_poly = any(p.get("strategy") in self._polymarket_strategies() for p in positions)
        poly_markets = {}
        if needs_poly:
            crypto = await self.poly.get_crypto_markets()
            weather = await self.poly.get_weather_markets()
            poly_markets = {m.get("id"): m for m in (crypto + weather) if m.get("id")}

        now = datetime.now(timezone.utc)
        resolved = 0

        for pos in positions:
            decision = await self._resolve_one(pos, poly_markets, now)
            if not decision:
                continue

            self.db.resolve_trade(
                trade_id=pos["id"],
                pnl=round(decision["net_pnl"], 6),
                exit_reason=decision["exit_reason"],
                realized_fee_usd=round(decision["realized_fee_usd"], 6),
                realized_net_pct=round(decision["realized_net_pct"], 6),
                borrow_fee_usd=round(decision["borrow_fee_usd"], 6),
                realized_slippage_usd=round(decision["realized_slippage_usd"], 6),
                realized_gas_usd=round(decision.get("realized_gas_usd", 0.0), 6),
                fill_ratio=round(decision.get("fill_ratio", 1.0), 6),
                reject_reason=decision.get("reject_reason", ""),
                gross_pnl=round(decision["gross_pnl"], 6),
                notes_append=decision["notes_append"],
            )
            self._notify_cooldown(pos.get("strategy", ""), pos.get("market_id", ""))
            resolved += 1

            log.info(
                f"Resolved #{pos['id']} {pos.get('strategy')} {pos.get('market_id')} "
                f"reason={decision['exit_reason']} net=${decision['net_pnl']:+.2f} "
                f"fee=${decision['realized_fee_usd']:.2f} slip=${decision['realized_slippage_usd']:.2f} "
                f"borrow=${decision['borrow_fee_usd']:.2f}"
            )

        if resolved:
            log.info(f"Resolver cycle closed {resolved} position(s)")

    async def _resolve_one(self, pos: dict, poly_markets: Dict[str, dict], now: datetime) -> Optional[dict]:
        strategy = pos.get("strategy", "")

        if strategy in self._binance_strategies():
            return await self._resolve_binance(pos, now)

        if strategy in self._polymarket_strategies():
            return await self._resolve_polymarket(pos, poly_markets, now)

        # Unknown strategy: safety exit once max hold exceeded.
        age = self._age_minutes(pos.get("time", ""), now)
        if age < self.cfg.MAX_HOLD_MINUTES_SPOT:
            return None

        cost = float(pos.get("cost", 0.0))
        return {
            "exit_reason": "risk_exit",
            "gross_pnl": 0.0,
            "realized_fee_usd": 0.0,
            "realized_slippage_usd": 0.0,
            "realized_gas_usd": 0.0,
            "fill_ratio": 1.0,
            "reject_reason": "",
            "borrow_fee_usd": 0.0,
            "net_pnl": 0.0,
            "realized_net_pct": 0.0 if cost <= 0 else 0.0,
            "notes_append": "risk_exit:unknown_strategy",
        }

    async def _resolve_binance(self, pos: dict, now: datetime) -> Optional[dict]:
        symbol = pos.get("market_id", "")
        side = pos.get("side", "")
        entry = float(pos.get("price", 0.0))
        size = float(pos.get("size", 0.0))
        cost = float(pos.get("cost", 0.0))
        strategy = str(pos.get("strategy", "") or "")

        if entry <= 0 or size <= 0:
            return None

        bp = self.binance.get(symbol)
        if not bp:
            return None

        current = float(bp.price)
        age_min = self._age_minutes(pos.get("time", ""), now)

        if side == "LONG":
            gross_pnl = (current - entry) * size
            move_pct = (current - entry) / entry
        else:
            gross_pnl = (entry - current) * size
            move_pct = (entry - current) / entry

        notional_entry = entry * size
        notional_exit = current * size

        fee_rate = float(getattr(self.cfg, "BINANCE_FEE_RATE", 0.002))
        realized_fee_usd = (notional_entry * (fee_rate / 2.0)) + (notional_exit * (fee_rate / 2.0))

        expected_slip = float(pos.get("expected_slippage_usd", 0.0) or 0.0)
        slippage_pct = expected_slip / max(cost, 1.0) if expected_slip > 0 else self.cfg.BINANCE_BASE_SLIPPAGE_PCT
        realized_slippage_usd = (notional_entry + notional_exit) * (slippage_pct / 2.0)

        borrow_fee_usd = 0.0
        if side == "SHORT":
            days_held = age_min / 1440.0
            borrow_fee_usd = notional_entry * (self.cfg.MARGIN_BORROW_APR / 365.0) * days_held

        net_pnl = gross_pnl - realized_fee_usd - realized_slippage_usd - borrow_fee_usd
        net_pct = net_pnl / cost if cost > 0 else 0.0

        vol = self.binance.volatility(symbol, seconds=300) or 0.001
        cost_pct = (realized_fee_usd + realized_slippage_usd + borrow_fee_usd) / max(cost, 1.0)

        tp_pct = max(self.cfg.MIN_DYNAMIC_TP_PCT, vol * self.cfg.DYNAMIC_TP_VOL_MULT + cost_pct * 1.2)
        sl_pct = min(
            self.cfg.MAX_DYNAMIC_SL_PCT,
            max(self.cfg.MIN_DYNAMIC_SL_PCT, vol * self.cfg.DYNAMIC_SL_VOL_MULT + cost_pct),
        )

        max_hold = int(getattr(self.cfg, "MAX_HOLD_MINUTES_SPOT", 120))
        min_flip_age = float(getattr(self.cfg, "SIGNAL_FLIP_MIN_AGE_MINUTES", 2.0))
        cross_hold_mode = bool(getattr(self.cfg, "CROSS_HOLD_UNTIL_FLIP", True)) and strategy == "binance_spot_margin_core"

        exit_reason = ""
        if cross_hold_mode:
            entry_tf = self._extract_entry_tf(pos.get("notes", ""))
            emergency_stop = abs(float(getattr(self.cfg, "CROSS_HOLD_MAX_ADVERSE_PCT", 0.015)))
            quality = self.binance.quality(symbol)
            cross_min_flip = float(getattr(self.cfg, "CROSS_HOLD_MIN_FLIP_AGE_MINUTES", 8.0))
            min_flip_move = float(getattr(self.cfg, "SIGNAL_FLIP_MIN_MOVE_PCT", 0.002))

            # 1. TP kontrolu (YENI - cross_hold'da da aktif)
            if move_pct >= tp_pct:
                exit_reason = "tp"
            # 2. SL kontrolu (YENI)
            elif move_pct <= -sl_pct:
                exit_reason = "sl"
            # 3. Emergency stop (sikilastirildi)
            elif move_pct <= -emergency_stop:
                exit_reason = "risk_exit"
            # 4. Karda + signal flip → cik (ama min hareket sarti var)
            elif move_pct >= min_flip_move and age_min >= min_flip_age:
                if await self._signal_flip(symbol, side, timeframe=entry_tf, prefer_ema=True):
                    exit_reason = "signal_flip"
            # 5. Zararda → uzun bekle + signal flip
            elif age_min >= cross_min_flip:
                if await self._signal_flip(symbol, side, timeframe=entry_tf, prefer_ema=True):
                    exit_reason = "signal_flip"
            # 6. Stale data
            elif quality.get("stale") and age_min >= max_hold * 0.5:
                exit_reason = "risk_exit"
            # 7. Zaman asimi
            elif age_min >= max_hold * 1.5:
                exit_reason = "time_expiry"
        else:
            if move_pct >= tp_pct:
                exit_reason = "tp"
            elif move_pct <= -sl_pct:
                exit_reason = "sl"
            else:
                quality = self.binance.quality(symbol)
                if age_min >= min_flip_age and await self._signal_flip(symbol, side):
                    exit_reason = "signal_flip"
                elif quality.get("stale") and age_min >= max_hold * 0.5:
                    exit_reason = "risk_exit"
                elif age_min >= max_hold * 1.5:
                    exit_reason = "time_expiry"

        if not exit_reason:
            return None

        notes = (
            f"exit={exit_reason} move={move_pct:+.4%} tp={tp_pct:.4%} sl={sl_pct:.4%} "
            f"fee={realized_fee_usd:.2f} slip={realized_slippage_usd:.2f} borrow={borrow_fee_usd:.2f}"
        )
        if cross_hold_mode:
            notes += f" mode=cross_hold tf={self._extract_entry_tf(pos.get('notes', ''))}"

        return {
            "exit_reason": exit_reason,
            "gross_pnl": gross_pnl,
            "realized_fee_usd": realized_fee_usd,
            "realized_slippage_usd": realized_slippage_usd,
            "realized_gas_usd": 0.0,
            "fill_ratio": float(pos.get("fill_ratio", 1.0) or 1.0),
            "reject_reason": str(pos.get("reject_reason", "") or ""),
            "borrow_fee_usd": borrow_fee_usd,
            "net_pnl": net_pnl,
            "realized_net_pct": net_pct,
            "notes_append": notes,
        }

    async def _resolve_polymarket(self, pos: dict, poly_markets: Dict[str, dict], now: datetime) -> Optional[dict]:
        market_id = pos.get("market_id", "")
        side = pos.get("side", "")
        entry = float(pos.get("price", 0.0))
        size = float(pos.get("size", 0.0))
        cost = float(pos.get("cost", 0.0))

        if entry <= 0 or size <= 0:
            return None

        market = poly_markets.get(market_id)
        if not market:
            # If market snapshot is unavailable, do a conservative risk exit after max hold.
            age_min = self._age_minutes(pos.get("time", ""), now)
            if age_min < self.cfg.MAX_HOLD_MINUTES_POLY * 1.2:
                return None
            return {
                "exit_reason": "risk_exit",
                "gross_pnl": 0.0,
                "realized_fee_usd": cost * self.cfg.POLY_TAKER_FEE,
                "realized_slippage_usd": cost * self.cfg.POLY_SLIPPAGE_PCT,
                "realized_gas_usd": float(pos.get("expected_gas_usd", 0.0) or 0.0),
                "fill_ratio": float(pos.get("fill_ratio", 1.0) or 1.0),
                "reject_reason": str(pos.get("reject_reason", "") or ""),
                "borrow_fee_usd": 0.0,
                "net_pnl": -(
                    cost * (self.cfg.POLY_TAKER_FEE + self.cfg.POLY_SLIPPAGE_PCT)
                    + float(pos.get("expected_gas_usd", 0.0) or 0.0)
                ),
                "realized_net_pct": -(
                    self.cfg.POLY_TAKER_FEE
                    + self.cfg.POLY_SLIPPAGE_PCT
                    + (float(pos.get("expected_gas_usd", 0.0) or 0.0) / max(cost, 1.0))
                ),
                "notes_append": "exit=risk_exit market_snapshot_unavailable",
            }

        current = float(market.get("yes_price", 0.0) if side == "YES" else market.get("no_price", 0.0))
        if current <= 0:
            return None

        age_min = self._age_minutes(pos.get("time", ""), now)
        gross_pnl = (current - entry) * size
        move_pct = (current - entry) / entry

        notional_entry = entry * size
        notional_exit = current * size

        fee_rate = float(getattr(self.cfg, "POLY_TAKER_FEE", 0.025))
        realized_fee_usd = (notional_entry * (fee_rate / 2.0)) + (notional_exit * (fee_rate / 2.0))

        expected_slip = float(pos.get("expected_slippage_usd", 0.0) or 0.0)
        slippage_pct = expected_slip / max(cost, 1.0) if expected_slip > 0 else self.cfg.POLY_SLIPPAGE_PCT
        realized_slippage_usd = (notional_entry + notional_exit) * (slippage_pct / 2.0)

        borrow_fee_usd = 0.0
        realized_gas_usd = float(pos.get("expected_gas_usd", 0.0) or 0.0)
        net_pnl = gross_pnl - realized_fee_usd - realized_slippage_usd - realized_gas_usd
        net_pct = net_pnl / cost if cost > 0 else 0.0

        symbol = self._symbol_from_text(pos.get("question", ""))
        vol = self.binance.volatility(symbol, seconds=300) if symbol else None
        if vol is None:
            vol = 0.002

        cost_pct = (realized_fee_usd + realized_slippage_usd) / max(cost, 1.0)
        tp_pct = max(self.cfg.MIN_DYNAMIC_TP_PCT, vol * self.cfg.DYNAMIC_TP_VOL_MULT + cost_pct * 1.2)
        sl_pct = min(
            self.cfg.MAX_DYNAMIC_SL_PCT,
            max(self.cfg.MIN_DYNAMIC_SL_PCT, vol * self.cfg.DYNAMIC_SL_VOL_MULT + cost_pct),
        )

        max_hold = int(getattr(self.cfg, "MAX_HOLD_MINUTES_POLY", 180))
        min_flip_age = float(getattr(self.cfg, "SIGNAL_FLIP_MIN_AGE_MINUTES", 2.0))

        exit_reason = ""
        if move_pct >= tp_pct:
            exit_reason = "tp"
        elif move_pct <= -sl_pct:
            exit_reason = "sl"
        elif age_min >= min_flip_age and await self._signal_flip(symbol, side):
            exit_reason = "signal_flip"
        elif age_min >= max_hold * 1.5:
            exit_reason = "time_expiry"

        if not exit_reason:
            return None

        notes = (
            f"exit={exit_reason} move={move_pct:+.4%} tp={tp_pct:.4%} sl={sl_pct:.4%} "
            f"fee={realized_fee_usd:.2f} slip={realized_slippage_usd:.2f} gas={realized_gas_usd:.2f}"
        )

        return {
            "exit_reason": exit_reason,
            "gross_pnl": gross_pnl,
            "realized_fee_usd": realized_fee_usd,
            "realized_slippage_usd": realized_slippage_usd,
            "realized_gas_usd": realized_gas_usd,
            "fill_ratio": float(pos.get("fill_ratio", 1.0) or 1.0),
            "reject_reason": str(pos.get("reject_reason", "") or ""),
            "borrow_fee_usd": borrow_fee_usd,
            "net_pnl": net_pnl,
            "realized_net_pct": net_pct,
            "notes_append": notes,
        }

    async def _signal_flip(
        self,
        symbol: str,
        side: str,
        timeframe: str = "",
        prefer_ema: bool = False,
    ) -> bool:
        if not symbol:
            return False
        side_u = (side or "").upper()

        if prefer_ema:
            tf = timeframe or str(getattr(self.cfg, "EMA_CROSS_PRIMARY_TF", "15m"))
            cross_dir, _ = await self._ema_cross_direction(symbol, tf)
            if side_u in ("LONG", "YES") and cross_dir == -1:
                return True
            if side_u in ("SHORT", "NO") and cross_dir == 1:
                return True

        hist = self.binance.history(symbol)
        prices = hist.prices(260) if hist else []
        if len(prices) >= 40:
            prev_line, prev_sig, _ = macd(prices[:-1], fast=5, slow=13, signal=4)
            cur_line, cur_sig, _ = macd(prices, fast=5, slow=13, signal=4)
            if (
                prev_line is not None
                and prev_sig is not None
                and cur_line is not None
                and cur_sig is not None
            ):
                bull_cross = prev_line <= prev_sig and cur_line > cur_sig
                bear_cross = prev_line >= prev_sig and cur_line < cur_sig
                if side_u in ("LONG", "YES") and bear_cross:
                    return True
                if side_u in ("SHORT", "NO") and bull_cross:
                    return True

        mom = self.binance.momentum(symbol, seconds=300)
        if mom is None:
            return False

        thr = abs(float(getattr(self.cfg, "SIGNAL_FLIP_MOMENTUM_PCT", 0.0012)))
        if side_u in ("LONG", "YES"):
            return mom < -thr
        if side_u in ("SHORT", "NO"):
            return mom > thr
        return False

    async def _ema_cross_direction(self, symbol: str, timeframe: str) -> Tuple[int, Optional[int]]:
        try:
            bars = max(220, int(getattr(self.cfg, "EMA_CROSS_SLOW_PERIOD", 50)) * 4)
            chart = await self.binance.export_chart(symbol, timeframe=timeframe, limit=bars)
            candles = chart.get("candles", []) if isinstance(chart, dict) else []
            closes = [float(c.get("close", 0.0)) for c in candles if float(c.get("close", 0.0)) > 0]
        except Exception:
            closes = []

        fast = int(getattr(self.cfg, "EMA_CROSS_FAST_PERIOD", 20))
        slow = int(getattr(self.cfg, "EMA_CROSS_SLOW_PERIOD", 50))
        lookback = int(getattr(self.cfg, "EMA_CROSS_EXIT_LOOKBACK_BARS", 2))
        return self._recent_ema_cross(closes, fast=fast, slow=slow, lookback=lookback)

    @staticmethod
    def _ema_series(values, period: int):
        if not values:
            return []
        k = 2.0 / (period + 1.0)
        out = []
        ema = float(values[0])
        for v in values:
            ema = float(v) * k + ema * (1.0 - k)
            out.append(ema)
        return out

    def _recent_ema_cross(self, closes, fast: int, slow: int, lookback: int = 2) -> Tuple[int, Optional[int]]:
        if not closes or len(closes) < max(slow + 5, 35):
            return 0, None
        fast_series = self._ema_series(closes, fast)
        slow_series = self._ema_series(closes, slow)

        max_age = max(1, min(int(lookback), len(closes) - 2))
        for age in range(0, max_age + 1):
            idx = len(closes) - 1 - age
            if idx <= 0:
                break
            prev_fast, prev_slow = fast_series[idx - 1], slow_series[idx - 1]
            cur_fast, cur_slow = fast_series[idx], slow_series[idx]
            if prev_fast <= prev_slow and cur_fast > cur_slow:
                return 1, age
            if prev_fast >= prev_slow and cur_fast < cur_slow:
                return -1, age
        return 0, None

    def _extract_entry_tf(self, notes: str) -> str:
        text = str(notes or "")
        marker = "entry_tf="
        idx = text.find(marker)
        default_tf = str(getattr(self.cfg, "EMA_CROSS_PRIMARY_TF", "15m"))
        if idx == -1:
            return default_tf
        start = idx + len(marker)
        end = start
        while end < len(text) and text[end] not in (" ", "|", ",", ";"):
            end += 1
        tf = text[start:end].strip()
        return tf or default_tf

    @staticmethod
    def _symbol_from_text(text: str) -> str:
        q = (text or "").upper()
        for sym in ("BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "MATIC"):
            if sym in q:
                return sym
        # fallback from long names
        ql = (text or "").lower()
        if "bitcoin" in ql:
            return "BTC"
        if "ethereum" in ql:
            return "ETH"
        if "solana" in ql:
            return "SOL"
        return ""

    def _notify_cooldown(self, strategy: str, symbol: str):
        for s in self._strategies:
            if s.name == strategy and hasattr(s, "notify_trade_closed"):
                s.notify_trade_closed(symbol)
                break

    @staticmethod
    def _age_minutes(ts_str: str, now: datetime) -> float:
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return max(0.0, (now - ts).total_seconds() / 60.0)
        except Exception:
            return 0.0

    @staticmethod
    def _binance_strategies():
        return {
            "binance_spot_margin_core",
            "binance_momentum",
            "binance_spot",
        }

    @staticmethod
    def _polymarket_strategies():
        return {
            "polymarket_core",
            "temporal_arb",
            "sum_to_one",
            "weather_arb",
        }
