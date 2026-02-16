"""
Binance Spot Paper Trader
=========================
Multi-coin spot trading simulation with separate $10K bankroll.
Trades all high-volume coins: BTC, ETH, SOL, XRP, DOGE, AVAX, ADA, LINK, DOT, MATIC.
Uses the same v2 indicator logic as BinanceMomentumTrader but with independent balance tracking.
"""

import logging
import time
from datetime import datetime, timezone
from strategies.base import BaseStrategy
from signals.technical import (
    rsi, macd, adx, ema_trend,
    squeeze_momentum, heikin_ashi_streak, ema_cross, modified_kelly,
)

log = logging.getLogger(__name__)


class BinanceSpotTrader(BaseStrategy):

    name = "binance_spot"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_trade_time = {}
        self._last_close_time = {}

    @property
    def interval(self) -> float:
        return 30

    def notify_trade_closed(self, symbol: str):
        """Trade resolver callback - cooldown baslatir."""
        self._last_close_time[symbol] = time.time()

    async def scan(self):
        if not self._is_trading_session():
            return

        for symbol in self.cfg.SPOT_SYMBOLS:
            try:
                await self._check_symbol(symbol)
            except Exception as e:
                log.error(f"{self.name} {symbol} error: {e}")

    def _is_trading_session(self) -> bool:
        # Kripto 7/24 trade edilir - Spot icin ayri seans filtresi
        if not getattr(self.cfg, 'SPOT_USE_SESSION_FILTER', False):
            return True
        now_utc = datetime.now(timezone.utc)
        current_hour = now_utc.hour
        for session in self.cfg.TRADING_SESSIONS:
            if session["start_utc"] <= current_hour < session["end_utc"]:
                return True
        return False

    async def _check_symbol(self, symbol: str):
        now = time.time()

        # Cooldown check
        last_close = self._last_close_time.get(symbol, 0)
        cooldown = self.cfg.TRADE_COOLDOWN_SECONDS
        if last_close > 0 and now - last_close < cooldown:
            return

        # Trade interval: don't re-trade if we recently opened one
        hold_time = self.cfg.BTC_HOLD_MINUTES * 60 if symbol == "BTC" else self.cfg.TEMPORAL_HOLD_MINUTES * 60
        last_trade = self._last_trade_time.get(symbol, 0)
        if now - last_trade < hold_time:
            return

        # Check if already have open position for this symbol
        positions = self.db.active_positions()
        for pos in positions:
            if pos.get("market_id") == symbol and pos.get("strategy") == self.name:
                return

        # Get Binance price
        bp = self.binance.get(symbol)
        if not bp or bp.age > 15:
            return

        # Volume filter: dusuk hacimli coinlerde trade acma
        vol_24h = self.binance.volume(symbol)
        min_vol = self.cfg.MIN_VOLUME_USD.get(symbol, self.cfg.MIN_VOLUME_USD.get("DEFAULT", 10_000_000))
        if 0 < vol_24h < min_vol:
            self.db.log_opportunity(
                strategy=self.name, market_id=symbol,
                question=f"{symbol}/USDT spot",
                edge=0, binance_price=bp.price,
                action="skipped_low_volume",
                notes=f"vol_24h=${vol_24h:,.0f} < min=${min_vol:,.0f}",
            )
            return

        # Get price history for indicators
        hist = self.binance.history(symbol)
        if not hist:
            return

        prices = hist.prices(200)
        if len(prices) < 60:
            return

        # Momentum hard gate
        mom_10m = self.binance.momentum(symbol, seconds=600)
        if mom_10m is None or abs(mom_10m) < self.cfg.MIN_MOMENTUM_GATE:
            if mom_10m is not None:
                self.db.log_opportunity(
                    strategy=self.name, market_id=symbol,
                    question=f"{symbol}/USDT spot",
                    edge=0, binance_price=bp.price,
                    action="skipped_low_momentum",
                    notes=f"mom={mom_10m:+.4%} < gate={self.cfg.MIN_MOMENTUM_GATE:.4%}",
                )
            return

        # v3: Multi-TF momentum (5m, 10m alignment - 1m cok noisy, skip)
        if getattr(self.cfg, 'USE_MULTI_TF_MOMENTUM', True):
            mom_5m = self.binance.momentum(symbol, seconds=300)
            if mom_5m is not None and mom_10m is not None:
                sign_5m = 1 if mom_5m > 0 else -1
                sign_10m = 1 if mom_10m > 0 else -1
                if sign_5m != sign_10m:
                    self.db.log_opportunity(
                        strategy=self.name, market_id=symbol,
                        question=f"{symbol}/USDT spot",
                        edge=0, binance_price=bp.price,
                        action="skipped_multi_tf",
                        notes=f"TF conflict: 5m={mom_5m:+.4%} 10m={mom_10m:+.4%}",
                    )
                    return

        # ADX filter
        adx_val = adx(prices, self.cfg.ADX_PERIOD)
        if adx_val is not None and adx_val < self.cfg.ADX_TREND_THRESHOLD:
            self.db.log_opportunity(
                strategy=self.name, market_id=symbol,
                question=f"{symbol}/USDT spot",
                edge=0, binance_price=bp.price,
                action="skipped_sideways",
                notes=f"ADX={adx_val:.1f} < {self.cfg.ADX_TREND_THRESHOLD}",
            )
            return

        # RSI dead zone
        rsi_val = rsi(prices, min(self.cfg.RSI_PERIOD, len(prices) - 2))
        if rsi_val is not None and self.cfg.RSI_NO_TRADE_LOW <= rsi_val <= self.cfg.RSI_NO_TRADE_HIGH:
            self.db.log_opportunity(
                strategy=self.name, market_id=symbol,
                question=f"{symbol}/USDT spot",
                edge=0, binance_price=bp.price,
                action="skipped_rsi_neutral",
                notes=f"RSI={rsi_val:.1f} in [{self.cfg.RSI_NO_TRADE_LOW}-{self.cfg.RSI_NO_TRADE_HIGH}]",
            )
            return

        # Analyze signals
        direction, confidence, signal_notes = self._analyze(symbol, prices, mom_10m, rsi_val, adx_val)

        if direction is None:
            self.db.log_opportunity(
                strategy=self.name, market_id=symbol,
                question=f"{symbol}/USDT spot",
                edge=0, binance_price=bp.price,
                action="scanned",
                notes=f"No signal | {signal_notes}",
            )
            return

        # Position sizing (v3: Modified Kelly option)
        price = bp.price
        base_size = self.cfg.SPOT_BANKROLL * self.cfg.SPOT_MAX_POSITION_PCT

        if getattr(self.cfg, 'USE_MODIFIED_KELLY', True):
            win_prob = getattr(self.cfg, 'KELLY_WIN_PROB_BASE', 0.55)
            adjusted_wp = min(win_prob + (confidence - 0.5) * 0.2, 0.80)
            kelly_frac = modified_kelly(
                edge=max(abs(mom_10m or 0), 0.001),
                win_prob=adjusted_wp,
            )
            size_usd = self.cfg.SPOT_BANKROLL * kelly_frac
            size_usd = max(10.0, min(size_usd, base_size))
        else:
            if confidence >= self.cfg.STRONG_SIGNAL_CONFIDENCE:
                size_usd = base_size
            else:
                size_usd = base_size * (0.5 + confidence * 0.5)
            size_usd = max(10.0, min(size_usd, base_size))

        # Realistic edge calculation
        realistic_edge = self._calculate_edge(symbol, confidence, mom_10m, adx_val, rsi_val)
        if realistic_edge < 0.005:
            self.db.log_opportunity(
                strategy=self.name, market_id=symbol,
                question=f"{symbol}/USDT spot",
                edge=realistic_edge, binance_price=price,
                action="skipped_low_edge",
                notes=f"edge={realistic_edge:.3%} < 0.5% | {signal_notes}",
            )
            return

        side = "LONG" if direction > 0 else "SHORT"
        shares = size_usd / price

        # Minimum dollar profit check - fee'lerden fazla kazanmali
        expected_profit_usd = size_usd * realistic_edge
        fee_usd = size_usd * self.cfg.BINANCE_FEE_RATE
        min_profit = getattr(self.cfg, 'MIN_DOLLAR_PROFIT', 1.50)
        if expected_profit_usd < min_profit or expected_profit_usd < fee_usd * 2:
            self.db.log_opportunity(
                strategy=self.name, market_id=symbol,
                question=f"{symbol}/USDT SPOT {side}",
                edge=realistic_edge, binance_price=price,
                action="skipped_low_dollar_profit",
                notes=f"exp_profit=${expected_profit_usd:.2f} < min=${min_profit:.2f} | fee=${fee_usd:.2f}",
            )
            return

        self.db.log_opportunity(
            strategy=self.name, market_id=symbol,
            question=f"{symbol}/USDT SPOT {side}",
            edge=realistic_edge, binance_price=price,
            action="trade", notes=signal_notes,
        )

        self.db.log_trade(
            strategy=self.name, market_id=symbol,
            question=f"{symbol}/USDT SPOT {side} @ ${price:,.2f}",
            side=side, price=price, size=shares,
            is_paper=True,
            notes=f"SPOT {symbol} {signal_notes}",
        )

        self._last_trade_time[symbol] = now
        emoji = "\U0001f7e2" if side == "LONG" else "\U0001f534"
        log.info(
            f"{emoji} SPOT: {symbol} {side} ${size_usd:.2f} @ ${price:,.2f} | "
            f"edge={realistic_edge:.2%} | exp_profit=${expected_profit_usd:.2f} fee=${fee_usd:.2f} | "
            f"vol24h=${vol_24h:,.0f} | {signal_notes}"
        )

    def _analyze(self, symbol, prices, mom_10m, rsi_val, adx_val):
        """Signal analysis with strict trend filter + consensus."""
        notes_parts = []

        # Strict trend filter
        trend = ema_trend(prices, short_period=20, long_period=50)
        if trend is not None:
            notes_parts.append(f"trend={'UP' if trend > 0 else 'DOWN' if trend < 0 else 'FLAT'}")
        else:
            trend = 0
            notes_parts.append("trend=N/A")

        if self.cfg.STRICT_TREND_FILTER and trend == 0:
            return None, 0, " | ".join(notes_parts) + " | FLAT"

        allowed = trend

        if adx_val is not None:
            notes_parts.append(f"adx={adx_val:.1f}")

        # Momentum alignment
        if mom_10m is not None:
            notes_parts.append(f"mom={mom_10m:+.3%}")
            if self.cfg.STRICT_TREND_FILTER and (1 if mom_10m > 0 else -1) != allowed:
                return None, 0, " | ".join(notes_parts) + " | MOM_VS_TREND"
        else:
            return None, 0, " | ".join(notes_parts) + " | mom=N/A"

        # RSI alignment
        rsi_direction = None
        if rsi_val is not None:
            notes_parts.append(f"rsi={rsi_val:.1f}")
            if rsi_val < self.cfg.RSI_OVERSOLD:
                rsi_direction = 1
            elif rsi_val > self.cfg.RSI_OVERBOUGHT:
                rsi_direction = -1
            elif rsi_val < 40:
                rsi_direction = -1
            elif rsi_val > 60:
                rsi_direction = 1
            if self.cfg.STRICT_TREND_FILTER and rsi_direction is not None and rsi_direction != allowed:
                return None, 0, " | ".join(notes_parts) + " | RSI_VS_TREND"
        else:
            notes_parts.append("rsi=N/A")

        # MACD alignment
        _, _, histogram_val = macd(prices)
        macd_direction = None
        if histogram_val is not None:
            notes_parts.append(f"macd={'bull' if histogram_val > 0 else 'bear'}")
            macd_direction = 1 if histogram_val > 0 else -1
            if self.cfg.STRICT_TREND_FILTER and macd_direction != allowed:
                return None, 0, " | ".join(notes_parts) + " | MACD_VS_TREND"
        else:
            notes_parts.append("macd=N/A")

        notes = " | ".join(notes_parts)

        # v3: Squeeze Momentum
        squeeze_dir = None
        if getattr(self.cfg, 'USE_SQUEEZE_MOMENTUM', True):
            sq_on, sq_val = squeeze_momentum(
                prices,
                getattr(self.cfg, 'SQUEEZE_BB_PERIOD', 20),
                getattr(self.cfg, 'SQUEEZE_BB_MULT', 2.0),
                getattr(self.cfg, 'SQUEEZE_KC_MULT', 1.5),
            )
            if sq_on is not None:
                notes += " | SQZ=ON" if sq_on else " | SQZ=off"
                if sq_val is not None and sq_val != 0:
                    squeeze_dir = 1 if sq_val > 0 else -1

        # v3: Heikin-Ashi Streak
        ha_dir = None
        if getattr(self.cfg, 'USE_HEIKIN_ASHI', True):
            ha_s = heikin_ashi_streak(prices)
            if ha_s is not None:
                notes += f" | HA={ha_s:+d}"
                min_streak = getattr(self.cfg, 'MIN_HA_STREAK', 2)
                if ha_s >= min_streak:
                    ha_dir = 1
                elif ha_s <= -min_streak:
                    ha_dir = -1

        # v3: EMA 5/20 Cross
        ema_cross_dir = None
        if getattr(self.cfg, 'USE_EMA_CROSS', True):
            ec = ema_cross(prices, fast=5, slow=20)
            if ec is not None and ec != 0:
                ema_cross_dir = ec
                notes += f" | EMA5/20={'bull' if ec > 0 else 'bear'}"

        # Consensus: v2 base + v3 bonus
        consensus = 0
        if mom_10m is not None and (1 if mom_10m > 0 else -1) == allowed:
            consensus += 1
        if rsi_direction is not None and rsi_direction == allowed:
            consensus += 1
        if macd_direction is not None and macd_direction == allowed:
            consensus += 1

        v3_bonus = 0
        if squeeze_dir is not None and squeeze_dir == allowed:
            v3_bonus += 1
        if ha_dir is not None and ha_dir == allowed:
            v3_bonus += 1
        if ema_cross_dir is not None and ema_cross_dir == allowed:
            v3_bonus += 1

        if consensus < 2:
            return None, 0, notes + f" | LOW_CONSENSUS({consensus}/3)"

        # Confidence
        factors = []
        if mom_10m is not None:
            factors.append(min(abs(mom_10m) / 0.005, 1.0))
        if rsi_val is not None:
            if allowed == 1:
                rs = min((rsi_val - 60) / 20, 1.0) if rsi_val > 60 else 0.3
                if rsi_val < self.cfg.RSI_OVERSOLD:
                    rs = 1.0
            else:
                rs = min((40 - rsi_val) / 20, 1.0) if rsi_val < 40 else 0.3
                if rsi_val > self.cfg.RSI_OVERBOUGHT:
                    rs = 1.0
            factors.append(rs)
        if histogram_val is not None and macd_direction == allowed:
            price_ref = prices[-1] if prices else 1
            factors.append(min(abs(histogram_val) / (price_ref * 0.001), 1.0))
        if adx_val is not None:
            factors.append(min((adx_val - 20) / 30, 1.0))
        # v3 bonus
        if v3_bonus > 0:
            factors.append(min(v3_bonus * 0.15, 0.3) + 0.5)

        if not factors:
            return None, 0, notes

        confidence = min(sum(factors) / len(factors), 1.0)
        if confidence < self.cfg.MIN_SIGNAL_CONFIDENCE:
            return None, 0, notes + f" | LOW_CONF({confidence:.2f})"

        notes += f" | conf={confidence:.2f} | consensus={consensus}+{v3_bonus}v3"
        return allowed, confidence, notes

    def _calculate_edge(self, symbol, confidence, momentum, adx_val, rsi_val):
        """Realistic edge calculation."""
        if momentum is None:
            return 0.0

        expected_move = abs(momentum)

        trend_bonus = 1.0
        if adx_val is not None:
            if adx_val > 40:
                trend_bonus = 1.5
            elif adx_val > 25:
                trend_bonus = 1.2
            else:
                trend_bonus = 0.8

        rsi_bonus = 1.0
        if rsi_val is not None:
            if rsi_val < 25 or rsi_val > 75:
                rsi_bonus = 1.3
            elif rsi_val < 35 or rsi_val > 65:
                rsi_bonus = 1.1

        # Maliyet: giris fee + cikis fee + slippage
        fee_cost = self.cfg.BINANCE_FEE_RATE  # 0.2% round-trip
        slippage = 0.001  # 0.1% slippage
        total_cost = fee_cost + slippage  # 0.3% toplam

        # Mean-reversion riski: momentum'un %60'i devam eder
        continuation_factor = 0.6

        gross = expected_move * continuation_factor * trend_bonus * rsi_bonus
        net = gross - total_cost

        return max(net * confidence, 0.0)
