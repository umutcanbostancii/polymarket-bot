"""Cost-aware Binance spot/margin directional engine (simulation)."""

import logging
import math
import time
from typing import Dict, Optional, Tuple

from signals.technical import macd, rsi, squeeze_momentum
from strategies.base import BaseStrategy

log = logging.getLogger(__name__)


class BinanceSpotMarginCoreStrategy(BaseStrategy):
    """
    Secondary strategy from the plan:
    EMA20/EMA50 cross trigger + multi-timeframe confirmation +
    fast MACD + RSI regime + VWAP/CVD + squeeze.
    Trades only when fee-aware expected net remains positive after hard gates.
    """

    name = "binance_spot_margin_core"
    model_version = "binance_spot_margin_core_v2"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_trade_time: Dict[str, float] = {}
        self._last_close_time: Dict[str, float] = {}

    @property
    def interval(self) -> float:
        return 20.0

    def notify_trade_closed(self, symbol: str):
        self._last_close_time[symbol] = time.time()

    async def scan(self):
        # Block all trading until kline warmup is complete.
        if not self.binance.warmup_complete:
            return

        positions = self.db.active_positions_by_strategy(self.name)
        open_symbols = {p.get("market_id") for p in positions}

        # Portfolio korelasyon limiti (Degisiklik 4)
        max_total = int(getattr(self.cfg, "MAX_SIMULTANEOUS_POSITIONS", 3))
        max_per_dir = int(getattr(self.cfg, "MAX_POSITIONS_PER_DIRECTION", 2))
        if len(positions) >= max_total:
            return

        long_count = sum(1 for p in positions if p.get("side") == "LONG")
        short_count = sum(1 for p in positions if p.get("side") == "SHORT")

        for symbol in self.cfg.SPOT_SYMBOLS:
            if symbol in open_symbols:
                continue
            try:
                opened = await self._evaluate_symbol(
                    symbol,
                    long_count=long_count,
                    short_count=short_count,
                    max_per_dir=max_per_dir,
                )
                if opened:
                    # One new position per cycle keeps execution disciplined.
                    break
            except Exception as exc:
                log.error(f"{self.name}:{symbol} error: {exc}")

    async def _evaluate_symbol(self, symbol: str, **kwargs) -> bool:
        now = time.time()
        cooldown = getattr(self.cfg, "TRADE_COOLDOWN_SECONDS", 180)
        last_close = self._last_close_time.get(symbol, 0.0)
        if last_close and now - last_close < cooldown:
            return False

        # Skip degraded symbols (gap/missing kline data produces bad indicators).
        degraded = self.binance.is_degraded(symbol)
        if degraded:
            self._audit(symbol=symbol, action="skipped", reason=f"degraded:{degraded}")
            return False

        bp = self.binance.get(symbol)
        if not bp or bp.age > 10:
            self._audit(symbol=symbol, action="skipped", reason="stale_price")
            return False

        quality = self.binance.quality(symbol)
        quality_score = float(quality.get("score", 0.0))
        if quality_score < 0.55:
            self._audit(
                symbol=symbol,
                action="skipped",
                reason="poor_data_quality",
                data_quality=quality_score,
                meta=quality,
            )
            return False

        volume_24h = self.binance.volume(symbol)
        min_vol = self.cfg.MIN_VOLUME_USD.get(symbol, self.cfg.MIN_VOLUME_USD.get("DEFAULT", 10_000_000))
        if 0 < volume_24h < min_vol:
            reason = f"low_volume:{volume_24h:.0f}<{min_vol:.0f}"
            self._audit(symbol=symbol, action="skipped", reason=reason, data_quality=quality_score)
            return False

        hist = self.binance.history(symbol)
        if not hist:
            return False

        prices = hist.prices(450)
        if len(prices) < 120:
            self._audit(symbol=symbol, action="skipped", reason="insufficient_history")
            return False

        # --- Cross-driven direction engine (EMA20/EMA50) ---
        cross_state = await self._ema_cross_state(symbol)
        trend = int(cross_state.get("trend", 0))
        if trend not in (-1, 1):
            self._audit(
                symbol=symbol,
                action="skipped",
                reason="ema_trend_flat",
                data_quality=quality_score,
                meta=cross_state,
            )
            return False

        primary_cross_dir = int(cross_state.get("primary_cross_dir", 0))
        primary_cross_age = cross_state.get("primary_cross_age")
        max_age = int(getattr(self.cfg, "EMA_CROSS_MAX_AGE_BARS", 1))
        if (
            primary_cross_dir != trend
            or primary_cross_age is None
            or int(primary_cross_age) > max_age
        ):
            self._audit(
                symbol=symbol,
                action="skipped",
                reason=f"ema_cross_not_fresh:{primary_cross_dir}@{primary_cross_age}",
                data_quality=quality_score,
                meta=cross_state,
            )
            return False

        confirmations = int(cross_state.get("confirmations", 0))
        min_confirms = int(getattr(self.cfg, "EMA_CROSS_MIN_CONFIRMATIONS", 2))
        if confirmations < min_confirms:
            self._audit(
                symbol=symbol,
                action="skipped",
                reason=f"ema_tf_not_aligned:{confirmations}/{min_confirms}",
                data_quality=quality_score,
                meta=cross_state,
            )
            return False

        macd_line, macd_signal, macd_hist = macd(prices, fast=5, slow=13, signal=4)
        macd_lb = int(getattr(self.cfg, "MACD_CROSS_LOOKBACK_BARS", 12))
        macd_cross_dir, macd_cross_age = self._recent_macd_cross(prices, lookback=macd_lb)
        rsi_val = rsi(prices, period=self.cfg.RSI_PERIOD)
        sq_on, sq_val = squeeze_momentum(
            prices,
            bb_period=getattr(self.cfg, "SQUEEZE_BB_PERIOD", 20),
            bb_mult=getattr(self.cfg, "SQUEEZE_BB_MULT", 2.0),
            kc_period=getattr(self.cfg, "SQUEEZE_BB_PERIOD", 20),
            kc_mult=getattr(self.cfg, "SQUEEZE_KC_MULT", 1.5),
        )

        mom_5m = self.binance.momentum(symbol, seconds=300)
        mom_15m = self.binance.momentum(symbol, seconds=900)
        micro = self.binance.microstructure(symbol, window_seconds=180)

        def _sign(v: Optional[float]) -> int:
            if v is None:
                return 0
            if v > 0:
                return 1
            if v < 0:
                return -1
            return 0

        rsi_sign = 0
        if rsi_val is not None:
            if rsi_val > 55:
                rsi_sign = 1
            elif rsi_val < 45:
                rsi_sign = -1

        votes = [
            _sign(mom_5m),
            _sign(macd_hist),
            rsi_sign,
            _sign(micro.get("price_vs_vwap_pct")),
            _sign(micro.get("cvd_ratio")),
            _sign(sq_val),
        ]

        aligned = sum(1 for v in votes if v == trend)
        if aligned < 4:
            self._audit(
                symbol=symbol,
                action="skipped",
                reason=f"low_alignment:{aligned}/6",
                data_quality=quality_score,
                meta={"votes": votes, "trend": trend, "ema_cross": cross_state},
            )
            return False

        side = "LONG" if trend > 0 else "SHORT"

        # Per-direction portfolio limit (Degisiklik 4)
        if side == "LONG" and kwargs.get("long_count", 0) >= kwargs.get("max_per_dir", 2):
            self._audit(symbol=symbol, action="skipped", reason="max_long_reached")
            return False
        if side == "SHORT" and kwargs.get("short_count", 0) >= kwargs.get("max_per_dir", 2):
            self._audit(symbol=symbol, action="skipped", reason="max_short_reached")
            return False

        # LONG icin ek filtre: daha guclu sinyal gerekli (Degisiklik 6)
        if side == "LONG":
            long_min_align = int(getattr(self.cfg, "LONG_MIN_ALIGNMENT", 5))
            if aligned < long_min_align:
                self._audit(symbol=symbol, action="skipped", reason=f"long_weak_alignment:{aligned}/6")
                return False

        confidence = aligned / 6.0

        # --- Cost-aware edge estimation (moved before sizing) ---
        vol = micro.get("volatility") or self.binance.volatility(symbol, seconds=300) or 0.001

        # Hold-time-aware edge: gercekci time scale (Degisiklik 1)
        expected_hold = float(getattr(self.cfg, "EXPECTED_HOLD_MINUTES", 10))
        time_scale = math.sqrt(max(expected_hold, 5) / 5.0)  # 1.41x (gercekci, eskisi 3.46x)

        # Momentum edge: directional continuation with time horizon
        mom_edge = (abs(mom_5m or 0.0) * 1.0 + abs(mom_15m or 0.0) * 0.6) * time_scale * 0.5

        # Volatility floor: vol capture dusuruldu (gercekci)
        vol_edge = vol * time_scale * (0.45 if sq_on else 0.25)

        # Confidence boost: more aligned signals = higher expected capture
        conf_mult = 1.0 + (confidence - 0.67) * 0.8  # 4/6=1.0, 5/6=1.13, 6/6=1.26

        raw_edge = max(mom_edge, vol_edge) * conf_mult

        # Shrink factor: model sistematik iyimser, duzeltme uygula (Degisiklik 1)
        shrink = float(getattr(self.cfg, "EDGE_SHRINK_FACTOR", 0.50))
        predicted_edge = raw_edge * shrink

        # --- Modified Kelly sizing with execution-risk-adjusted p ---
        liq_score = min(volume_24h / max(min_vol * 8, 1.0), 1.0)
        p_exec = max(0.40, min(0.99, 0.45 + 0.35 * quality_score + 0.20 * liq_score))

        base_win_prob = 0.52 + 0.30 * confidence
        win_prob = max(0.01, min(0.95, base_win_prob * p_exec))

        b = 1.2
        q = 1.0 - win_prob
        kelly = ((b * win_prob - q) / b) * math.sqrt(win_prob)
        if kelly <= 0:
            self._audit(symbol=symbol, action="skipped", reason="kelly_non_positive", data_quality=quality_score)
            return False

        bankroll = self.cfg.SPOT_BANKROLL
        raw_notional = bankroll * self.cfg.KELLY_FRACTION * kelly

        # Kademeli pozisyon boyutlandirma (Degisiklik 2)
        preliminary_net = raw_notional * predicted_edge - raw_notional * self.cfg.BINANCE_FEE_RATE
        tier_caps = getattr(self.cfg, "NOTIONAL_TIERS", None) or {3.0: 200, 6.0: 400, 999: 600}
        tier_cap = 200.0  # default en dusuk
        for threshold, cap in sorted(tier_caps.items()):
            if preliminary_net >= threshold:
                tier_cap = float(cap)

        pos_cap = min(bankroll * self.cfg.SPOT_MAX_POSITION_PCT, tier_cap)
        liq_cap = max(self.cfg.MIN_TRADE_NOTIONAL_USD, volume_24h * self.cfg.MAX_LIQUIDITY_SHARE)
        available_cash = self._wallet_available(side)
        notional = max(self.cfg.MIN_TRADE_NOTIONAL_USD, min(raw_notional, pos_cap, liq_cap, available_cash))

        if notional < self.cfg.MIN_TRADE_NOTIONAL_USD:
            self._audit(
                symbol=symbol,
                action="skipped",
                reason="notional_below_min",
                confidence=confidence,
                p_exec=p_exec,
                win_prob=win_prob,
                notional=notional,
                data_quality=quality_score,
            )
            return False

        # Volume-proportional slippage via sqrt market-impact model
        # $800 on BTC ($200M vol) -> ~0.09%, $800 on MATIC ($2M vol) -> ~0.18%
        impact_ratio = notional / max(volume_24h, 1.0)
        slippage_pct = min(
            self.cfg.BINANCE_MAX_SLIPPAGE_PCT,
            self.cfg.BINANCE_BASE_SLIPPAGE_PCT + math.sqrt(impact_ratio) * 0.05,
        )

        expected_fee_usd = notional * self.cfg.BINANCE_FEE_RATE
        expected_slippage_usd = notional * slippage_pct
        expected_borrow_usd = 0.0
        if side == "SHORT":
            hold_days = self.cfg.MAX_HOLD_MINUTES_SPOT / 1440.0
            expected_borrow_usd = notional * (self.cfg.MARGIN_BORROW_APR / 365.0) * hold_days

        total_cost_usd = expected_fee_usd + expected_slippage_usd + expected_borrow_usd
        expected_net_usd = notional * predicted_edge - total_cost_usd
        expected_net_pct = expected_net_usd / notional if notional > 0 else 0.0

        min_gate_usd = max(
            self.cfg.DIRECTIONAL_MIN_EXPECTED_NET_USD,
            self.cfg.DIRECTIONAL_MIN_NET_FEE_MULT * expected_fee_usd,
            self.cfg.DIRECTIONAL_MIN_NOTIONAL_EDGE_PCT * notional,
        )

        decision_reason = (
            f"trend={trend} aligned={aligned}/6 macd={_sign(macd_hist)} rsi={rsi_val:.1f} "
            f"mom5={mom_5m:+.4%} vwap={micro.get('price_vs_vwap_pct', 0):+.4%} "
            f"cvd={micro.get('cvd_ratio', 0):+.3f} ema_cross={primary_cross_dir}@{primary_cross_age} "
            f"tf={cross_state.get('entry_tf')} tf_ok={confirmations} macd_cross={macd_cross_dir}@{macd_cross_age} "
            f"p_exec={p_exec:.2f}"
        ) if rsi_val is not None else (
            f"trend={trend} aligned={aligned}/6 p_exec={p_exec:.2f}"
        )

        if (
            expected_net_usd < min_gate_usd
            or expected_net_pct < self.cfg.DIRECTIONAL_MIN_EXPECTED_NET_PCT
        ):
            self._audit(
                symbol=symbol,
                side=side,
                action="skipped",
                reason="hard_entry_gate",
                predicted_edge=predicted_edge,
                expected_fee_usd=expected_fee_usd,
                expected_slippage_usd=expected_slippage_usd,
                expected_net_usd=expected_net_usd,
                expected_net_pct=expected_net_pct,
                confidence=confidence,
                p_exec=p_exec,
                win_prob=win_prob,
                notional=notional,
                data_quality=quality_score,
                meta={
                    "min_gate_usd": min_gate_usd,
                    "expected_borrow_usd": expected_borrow_usd,
                    "ema_cross": cross_state,
                    "macd_cross_dir": macd_cross_dir,
                    "macd_cross_age": macd_cross_age,
                    "decision": decision_reason,
                },
            )
            return False

        shares = notional / bp.price
        notes = (
            f"{symbol} {side} | edge={predicted_edge:.3%} | exp_net=${expected_net_usd:.2f} "
            f"({expected_net_pct:.3%}) | fee=${expected_fee_usd:.2f} "
            f"slip=${expected_slippage_usd:.2f} borrow=${expected_borrow_usd:.2f} "
            f"| entry_tf={cross_state.get('entry_tf')} entry_cross={primary_cross_dir}@{primary_cross_age} "
            f"tf_conf={confirmations} | {decision_reason}"
        )

        self.db.log_opportunity(
            strategy=self.name,
            market_id=symbol,
            question=f"{symbol}/USDT {side}",
            edge=predicted_edge,
            binance_price=bp.price,
            action="trade",
            notes=notes,
        )

        self._audit(
            symbol=symbol,
            side=side,
            action="trade",
            reason="entry_ok",
            predicted_edge=predicted_edge,
            expected_fee_usd=expected_fee_usd,
            expected_slippage_usd=expected_slippage_usd,
            expected_net_usd=expected_net_usd,
            expected_net_pct=expected_net_pct,
            confidence=confidence,
            p_exec=p_exec,
            win_prob=win_prob,
            notional=notional,
            data_quality=quality_score,
            meta={
                "expected_borrow_usd": expected_borrow_usd,
                "ema_cross": cross_state,
                "macd_cross_dir": macd_cross_dir,
                "macd_cross_age": macd_cross_age,
                "decision": decision_reason,
                "wallet_available": available_cash,
            },
        )

        self.db.log_trade(
            strategy=self.name,
            market_id=symbol,
            question=f"{symbol}/USDT {side} @ ${bp.price:,.4f}",
            side=side,
            price=bp.price,
            size=shares,
            is_paper=True,
            notes=notes,
            expected_fee_usd=expected_fee_usd + expected_borrow_usd,
            expected_slippage_usd=expected_slippage_usd,
            expected_net_usd=expected_net_usd,
            expected_net_pct=expected_net_pct,
            predicted_edge=predicted_edge,
            model_version=self.model_version,
            decision_reason=decision_reason,
        )

        self._last_trade_time[symbol] = now
        log.info(
            f"{self.name}: {symbol} {side} notional=${notional:.2f} edge={predicted_edge:.3%} "
            f"exp_net=${expected_net_usd:.2f} fee=${expected_fee_usd:.2f} tf={cross_state.get('entry_tf')}"
        )
        return True

    async def _ema_cross_state(self, symbol: str) -> dict:
        primary_tf = str(getattr(self.cfg, "EMA_CROSS_PRIMARY_TF", "15m") or "15m")
        raw_tfs = list(getattr(self.cfg, "EMA_CROSS_TIMEFRAMES", ["1m", "5m", "15m"]) or [])

        # Keep insertion order and guarantee primary exists.
        ordered = []
        seen = set()
        for tf in [primary_tf] + raw_tfs:
            t = str(tf).strip()
            if not t or t in seen:
                continue
            seen.add(t)
            ordered.append(t)

        fast_p = int(getattr(self.cfg, "EMA_CROSS_FAST_PERIOD", 20))
        slow_p = int(getattr(self.cfg, "EMA_CROSS_SLOW_PERIOD", 50))
        lookback = max(1, int(getattr(self.cfg, "EMA_CROSS_MAX_AGE_BARS", 1)) + 2)

        snaps = {}
        for tf in ordered:
            chart = await self.binance.export_chart(symbol, timeframe=tf, limit=max(220, slow_p * 4))
            candles = chart.get("candles", []) if isinstance(chart, dict) else []
            closes = [float(c.get("close", 0.0)) for c in candles if float(c.get("close", 0.0)) > 0]

            trend_dir = self._ema_trend_direction(closes, fast_p, slow_p)
            cross_dir, cross_age = self._recent_ema_cross(closes, fast_p, slow_p, lookback=lookback)
            snaps[tf] = {
                "trend": trend_dir,
                "cross_dir": cross_dir,
                "cross_age": cross_age,
                "bars": len(closes),
            }

        primary = snaps.get(primary_tf, {})
        trend = int(primary.get("trend", 0))
        confirmations = sum(1 for s in snaps.values() if int(s.get("trend", 0)) == trend and trend in (-1, 1))

        return {
            "entry_tf": primary_tf,
            "trend": trend,
            "primary_cross_dir": int(primary.get("cross_dir", 0)),
            "primary_cross_age": primary.get("cross_age"),
            "confirmations": confirmations,
            "timeframes": snaps,
        }

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

    def _ema_trend_direction(self, closes, fast: int, slow: int) -> int:
        if not closes or len(closes) < max(slow + 2, 30):
            return 0
        fast_series = self._ema_series(closes, fast)
        slow_series = self._ema_series(closes, slow)
        if not fast_series or not slow_series:
            return 0
        f = fast_series[-1]
        s = slow_series[-1]
        if s == 0:
            return 0
        diff = (f - s) / s
        if diff > 0.0002:
            return 1
        if diff < -0.0002:
            return -1
        return 0

    def _recent_ema_cross(self, closes, fast: int, slow: int, lookback: int = 3) -> Tuple[int, Optional[int]]:
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

    def _recent_macd_cross(self, prices, lookback: int = 8) -> Tuple[int, Optional[int]]:
        """
        Returns (direction, age_bars):
          +1 bullish crossover (macd_line crossed above signal_line)
          -1 bearish crossover
           0 none in lookback
        age_bars=0 means latest bar.
        """
        if not prices or len(prices) < 40:
            return 0, None

        max_lb = max(1, min(int(lookback), len(prices) - 30))
        for age in range(0, max_lb):
            # after: includes the bar where cross may appear
            after = prices if age == 0 else prices[:-age]
            before = after[:-1]
            if len(before) < 35:
                continue

            prev_line, prev_sig, _ = macd(before, fast=5, slow=13, signal=4)
            cur_line, cur_sig, _ = macd(after, fast=5, slow=13, signal=4)
            if (
                prev_line is None
                or prev_sig is None
                or cur_line is None
                or cur_sig is None
            ):
                continue

            if prev_line <= prev_sig and cur_line > cur_sig:
                return 1, age
            if prev_line >= prev_sig and cur_line < cur_sig:
                return -1, age
        return 0, None

    def _wallet_available(self, side: str) -> float:
        positions = self.db.active_positions_by_strategy(self.name)
        long_total = self.cfg.SPOT_BANKROLL * self.cfg.SPOT_LONG_WALLET_SHARE
        short_total = self.cfg.SPOT_BANKROLL * self.cfg.SPOT_SHORT_WALLET_SHARE

        locked_long = sum(p.get("cost", 0.0) for p in positions if p.get("side") == "LONG")
        locked_short = sum(p.get("cost", 0.0) for p in positions if p.get("side") == "SHORT")

        if side == "LONG":
            return max(0.0, long_total - locked_long)
        return max(0.0, short_total - locked_short)

    def _audit(
        self,
        symbol: str,
        action: str,
        reason: str,
        side: str = "",
        predicted_edge: float = 0.0,
        expected_fee_usd: float = 0.0,
        expected_slippage_usd: float = 0.0,
        expected_net_usd: float = 0.0,
        expected_net_pct: float = 0.0,
        confidence: float = 0.0,
        p_exec: float = 0.0,
        win_prob: float = 0.0,
        notional: float = 0.0,
        data_quality: float = 0.0,
        meta: dict = None,
    ):
        self.db.log_decision_audit(
            strategy=self.name,
            market_id=symbol,
            question=f"{symbol}/USDT",
            symbol=symbol,
            side=side,
            action=action,
            decision_reason=reason,
            predicted_edge=predicted_edge,
            expected_fee_usd=expected_fee_usd,
            expected_slippage_usd=expected_slippage_usd,
            expected_net_usd=expected_net_usd,
            expected_net_pct=expected_net_pct,
            model_version=self.model_version,
            confidence=confidence,
            p_exec=p_exec,
            win_prob=win_prob,
            notional=notional,
            data_quality=data_quality,
            meta=meta or {},
        )
