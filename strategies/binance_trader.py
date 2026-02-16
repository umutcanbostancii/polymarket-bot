"""
Binance Momentum Paper Trader v2
=================================
Uses real-time Binance price data to open simulated crypto trades.

KRITIK IYILESTIRMELER (v2 - strateji duzeltmeleri):

Fix 1: STRICT TREND FILTER
  - EMA50 ustunde = SADECE LONG, altinda = SADECE SHORT
  - Istisna yok. Counter-trend trade = yasak.
  - Tek basina win rate'i dramatik arttirir.

Fix 2: MINIMUM MOMENTUM HARD GATE
  - |momentum| < %0.15 = NO TRADE
  - Micro hareketler gurultu. Trade'e degmez.
  - Overtrading'i keser.

Fix 3: RSI DEAD ZONE
  - RSI 40-60 arasi = NO TRADE
  - Bu notr bolge. Sinyal uretmez, sadece noise.
  - False signal sayisini ciddi azaltir.

Fix 4: EDGE RECALIBRATION
  - Edge = volatility-adjusted momentum x trend alignment x indicator consensus
  - Artik teorik degil, gercekci hesaplama.
  - Predicted edge vs realized PnL arasindaki korelasyonu arttirir.

Fix 5: TRADE COOLDOWN
  - Trade kapandiktan sonra 3 dakika bekleme
  - Ayni symbol'de arka arkaya trade acmayi onler
  - Choppy markette kendini ogutmeyi engeller

Onceki sorunlar:
- %23 win rate (kabul edilemez)
- macd=bull + SHORT aciliyordu (celiski)
- RSI 50 civarinda trade aciliyordu (notr bolge)
- mom=+0.02% ile trade aciliyordu (gurultu)
- Edge %30-70 ama PnL ~$0 (kalibre degil)
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


class BinanceMomentumTrader(BaseStrategy):

    name = "binance_momentum"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_trade_time = {}      # {symbol: timestamp} - trade acilis zamani
        self._last_close_time = {}      # {symbol: timestamp} - trade kapanis zamani (cooldown icin)

    @property
    def interval(self) -> float:
        return 30  # Check every 30 seconds

    async def scan(self):
        # Seans filtresi: dusuk hacimli saatlerde trade acma
        if not self._is_trading_session():
            return

        for symbol in ("BTC", "ETH", "SOL"):
            try:
                await self._check_symbol(symbol)
            except Exception as e:
                log.error(f"{self.name} {symbol} error: {e}")

    def _is_trading_session(self) -> bool:
        """
        Trading session kontrolu.
        Sadece London ve NY seanslarinda trade acar.
        @ecocantravel: "dusuk hacimli saatlerde spread yuksek"
        @evrenkececi: "NY ve London seanslarinda calistirin"
        """
        if not self.cfg.USE_SESSION_FILTER:
            return True

        now_utc = datetime.now(timezone.utc)
        current_hour = now_utc.hour

        for session in self.cfg.TRADING_SESSIONS:
            start = session["start_utc"]
            end = session["end_utc"]
            if start <= current_hour < end:
                return True

        log.debug(f"Seans disi: UTC {current_hour}:00 - trade atlanir")
        return False

    def notify_trade_closed(self, symbol: str):
        """Trade resolver tarafindan cagirilir - cooldown baslatir."""
        self._last_close_time[symbol] = time.time()

    async def _check_symbol(self, symbol: str):
        now = time.time()

        # ===== FIX 5: TRADE COOLDOWN =====
        # Son kapatilan trade'den sonra bekleme suresi
        last_close = self._last_close_time.get(symbol, 0)
        cooldown = getattr(self.cfg, 'TRADE_COOLDOWN_SECONDS', 180)
        if now - last_close < cooldown and last_close > 0:
            return  # Cooldown suresi dolmadi

        # Trade interval: BTC her 30 dk, digerleri her 60 dk
        if symbol == "BTC":
            trade_interval = self.cfg.BTC_HOLD_MINUTES * 60
        else:
            trade_interval = self.cfg.TEMPORAL_HOLD_MINUTES * 60

        last_trade = self._last_trade_time.get(symbol, 0)
        if now - last_trade < trade_interval:
            return

        # Check if we already have an open position for this symbol
        positions = self.db.active_positions()
        for pos in positions:
            if pos.get("market_id") == symbol and pos.get("strategy") == self.name:
                return  # Already have open position

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
                question=f"{symbol}/USDT analysis",
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
        if len(prices) < 60:  # Daha fazla data gerekli (eskiden 50)
            log.debug(f"{symbol}: Only {len(prices)} data points, need 60+")
            return

        # ===== FIX 2: MOMENTUM HARD GATE =====
        # |momentum| < %0.15 ise trade ACMA. Bu gurultu.
        mom_10m = self.binance.momentum(symbol, seconds=600)
        min_mom_gate = getattr(self.cfg, 'MIN_MOMENTUM_GATE', 0.0015)
        if mom_10m is None or abs(mom_10m) < min_mom_gate:
            if mom_10m is not None:
                self.db.log_opportunity(
                    strategy=self.name,
                    market_id=symbol,
                    question=f"{symbol}/USDT analysis",
                    edge=0,
                    binance_price=bp.price,
                    action="skipped_low_momentum",
                    notes=f"mom={mom_10m:+.4%} < gate={min_mom_gate:.4%} (gurultu)",
                )
            return

        # ===== v3: MULTI-TIMEFRAME MOMENTUM =====
        # 5m ve 10m uyumlu olmali (1m cok noisy, kaldirildi)
        if getattr(self.cfg, 'USE_MULTI_TF_MOMENTUM', True):
            mom_5m = self.binance.momentum(symbol, seconds=300)
            if mom_5m is not None and mom_10m is not None:
                sign_5m = 1 if mom_5m > 0 else -1
                sign_10m = 1 if mom_10m > 0 else -1
                if sign_5m != sign_10m:
                    self.db.log_opportunity(
                        strategy=self.name,
                        market_id=symbol,
                        question=f"{symbol}/USDT analysis",
                        edge=0,
                        binance_price=bp.price,
                        action="skipped_multi_tf",
                        notes=f"TF conflict: 5m={mom_5m:+.4%} 10m={mom_10m:+.4%}",
                    )
                    return

        # ADX filtresi: yatay piyasada trade acma
        adx_val = adx(prices, self.cfg.ADX_PERIOD)
        if adx_val is not None and adx_val < self.cfg.ADX_TREND_THRESHOLD:
            self.db.log_opportunity(
                strategy=self.name,
                market_id=symbol,
                question=f"{symbol}/USDT analysis",
                edge=0,
                binance_price=bp.price,
                action="skipped_sideways",
                notes=f"ADX={adx_val:.1f} < {self.cfg.ADX_TREND_THRESHOLD} (yatay piyasa)",
            )
            return

        # ===== FIX 3: RSI DEAD ZONE =====
        rsi_val = rsi(prices, min(self.cfg.RSI_PERIOD, len(prices) - 2))
        rsi_no_low = getattr(self.cfg, 'RSI_NO_TRADE_LOW', 40.0)
        rsi_no_high = getattr(self.cfg, 'RSI_NO_TRADE_HIGH', 60.0)
        if rsi_val is not None and rsi_no_low <= rsi_val <= rsi_no_high:
            self.db.log_opportunity(
                strategy=self.name,
                market_id=symbol,
                question=f"{symbol}/USDT analysis",
                edge=0,
                binance_price=bp.price,
                action="skipped_rsi_neutral",
                notes=f"RSI={rsi_val:.1f} in dead zone [{rsi_no_low}-{rsi_no_high}]",
            )
            return

        # Calculate signals (with strict trend filter)
        direction, confidence, signal_notes = self._analyze(symbol, prices, mom_10m, rsi_val, adx_val)

        if direction is None:
            self.db.log_opportunity(
                strategy=self.name,
                market_id=symbol,
                question=f"{symbol}/USDT analysis",
                edge=0,
                binance_price=bp.price,
                action="scanned",
                notes=f"No clear signal | {signal_notes}",
            )
            return

        # Position sizing (v3: Modified Kelly option)
        price = bp.price
        base_size = self.cfg.BANKROLL * self.cfg.MAX_POSITION_PCT

        if getattr(self.cfg, 'USE_MODIFIED_KELLY', True):
            # Modified Kelly: f = (b*p - q) / b * sqrt(p)
            win_prob = getattr(self.cfg, 'KELLY_WIN_PROB_BASE', 0.55)
            # Confidence ile ayarla: yuksek confidence = yuksek win_prob
            adjusted_wp = min(win_prob + (confidence - 0.5) * 0.2, 0.80)
            kelly_frac = modified_kelly(
                edge=max(abs(mom_10m or 0), 0.001),
                win_prob=adjusted_wp,
            )
            size_usd = self.cfg.BANKROLL * kelly_frac
            size_usd = max(10.0, min(size_usd, base_size))
        else:
            if confidence >= self.cfg.STRONG_SIGNAL_CONFIDENCE:
                size_usd = base_size
            else:
                size_usd = base_size * (0.5 + confidence * 0.5)
            size_usd = max(10.0, min(size_usd, base_size))

        # ===== FIX 4: EDGE RECALIBRATION - realistic edge =====
        realistic_edge = self._calculate_realistic_edge(
            symbol, confidence, mom_10m, adx_val, rsi_val
        )

        # Edge cok dusukse trade acma
        if realistic_edge < 0.005:  # %0.5'den dusuk gercekci edge = gecme
            self.db.log_opportunity(
                strategy=self.name,
                market_id=symbol,
                question=f"{symbol}/USDT analysis",
                edge=realistic_edge,
                binance_price=price,
                action="skipped_low_edge",
                notes=f"realistic_edge={realistic_edge:.3%} < 0.5% | {signal_notes}",
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
                strategy=self.name,
                market_id=symbol,
                question=f"{symbol}/USDT {side}",
                edge=realistic_edge,
                binance_price=price,
                action="skipped_low_dollar_profit",
                notes=(
                    f"exp_profit=${expected_profit_usd:.2f} < min=${min_profit:.2f} | "
                    f"fee=${fee_usd:.2f} | size=${size_usd:.0f} | {signal_notes}"
                ),
            )
            return

        # Log opportunity with REALISTIC edge
        self.db.log_opportunity(
            strategy=self.name,
            market_id=symbol,
            question=f"{symbol}/USDT {side}",
            edge=realistic_edge,
            binance_price=price,
            action="trade",
            notes=signal_notes,
        )

        # Execute paper trade
        self.db.log_trade(
            strategy=self.name,
            market_id=symbol,
            question=f"{symbol}/USDT {side} @ ${price:,.2f}",
            side=side,
            price=price,
            size=shares,
            is_paper=True,
            notes=f"{symbol} {signal_notes}",
        )

        self._last_trade_time[symbol] = now
        emoji = "\U0001f7e2" if side == "LONG" else "\U0001f534"
        log.info(
            f"{emoji} {self.name}: {symbol} {side} "
            f"${size_usd:.2f} @ ${price:,.2f} | edge={realistic_edge:.2%} | "
            f"exp_profit=${expected_profit_usd:.2f} fee=${fee_usd:.2f} | "
            f"vol24h=${vol_24h:,.0f} | {signal_notes}"
        )

    def _analyze(self, symbol, prices, mom_10m, rsi_val, adx_val):
        """
        Analyze signals and return (direction, confidence, notes).

        v2 KURALLARI:
        1. Trend yonu = trade yonu (EMA50). ZORUNLU.
        2. RSI rejim: < 40 SHORT, > 60 LONG (dead zone zaten filtrelendi)
        3. MACD trendle uyumlu olmali
        4. Momentum trendle uyumlu olmali
        5. Her sinyal trendle uyumsuzsa → trade YOK
        """
        notes_parts = []

        # ===== FIX 1: STRICT TREND FILTER =====
        trend = ema_trend(prices, short_period=20, long_period=50)
        strict_filter = getattr(self.cfg, 'STRICT_TREND_FILTER', True)

        if trend is not None:
            trend_dir = "UP" if trend > 0 else ("DOWN" if trend < 0 else "FLAT")
            notes_parts.append(f"trend={trend_dir}")
        else:
            trend = 0
            notes_parts.append("trend=N/A")

        # Trend FLAT ise: trade ACMA
        if strict_filter and trend == 0:
            notes = " | ".join(notes_parts) + " | FLAT_TREND_REJECTED"
            return None, 0, notes

        # Trend yonu = izin verilen trade yonu
        # trend=+1 → sadece LONG, trend=-1 → sadece SHORT
        allowed_direction = trend  # +1 veya -1

        if adx_val is not None:
            notes_parts.append(f"adx={adx_val:.1f}")

        # Momentum check (zaten mom_10m gecmis, burada sinyale cevir)
        if mom_10m is not None:
            notes_parts.append(f"mom={mom_10m:+.3%}")
            # Momentum trende uyumlu mu?
            mom_direction = 1 if mom_10m > 0 else -1
            if strict_filter and mom_direction != allowed_direction:
                notes = " | ".join(notes_parts) + " | MOM_VS_TREND"
                return None, 0, notes
        else:
            notes_parts.append("mom=N/A")
            return None, 0, " | ".join(notes_parts)

        # RSI check (dead zone zaten _check_symbol'de filtrelendi)
        rsi_direction = None
        if rsi_val is not None:
            notes_parts.append(f"rsi={rsi_val:.1f}")
            if rsi_val < self.cfg.RSI_OVERSOLD:
                rsi_direction = 1   # Oversold → LONG sinyal
            elif rsi_val > self.cfg.RSI_OVERBOUGHT:
                rsi_direction = -1  # Overbought → SHORT sinyal
            elif rsi_val < 40:
                rsi_direction = -1  # 30-40: SHORT bolgesi
            elif rsi_val > 60:
                rsi_direction = 1   # 60-70: LONG bolgesi

            # RSI trende uyumlu mu?
            if strict_filter and rsi_direction is not None and rsi_direction != allowed_direction:
                notes = " | ".join(notes_parts) + " | RSI_VS_TREND"
                return None, 0, notes
        else:
            notes_parts.append("rsi=N/A")

        # MACD check
        macd_line_val, signal_line_val, histogram_val = macd(prices)
        macd_direction = None
        if histogram_val is not None:
            macd_trend = "bull" if histogram_val > 0 else "bear"
            notes_parts.append(f"macd={macd_trend}")
            macd_direction = 1 if histogram_val > 0 else -1

            # MACD trende uyumlu mu?
            if strict_filter and macd_direction != allowed_direction:
                notes = " | ".join(notes_parts) + " | MACD_VS_TREND"
                return None, 0, notes
        else:
            notes_parts.append("macd=N/A")

        notes = " | ".join(notes_parts)

        # ===== v3: SQUEEZE MOMENTUM =====
        # Squeeze aktifse ve momentum trend yonundeyse bonus sinyal
        squeeze_dir = None
        if getattr(self.cfg, 'USE_SQUEEZE_MOMENTUM', True):
            sq_on, sq_val = squeeze_momentum(
                prices,
                getattr(self.cfg, 'SQUEEZE_BB_PERIOD', 20),
                getattr(self.cfg, 'SQUEEZE_BB_MULT', 2.0),
                getattr(self.cfg, 'SQUEEZE_KC_MULT', 1.5),
            )
            if sq_on is not None:
                if sq_on:
                    notes += " | SQZ=ON"  # Volatilite sikismis, breakout yaklasıyor
                else:
                    notes += " | SQZ=off"
                if sq_val is not None and sq_val != 0:
                    squeeze_dir = 1 if sq_val > 0 else -1

        # ===== v3: HEIKIN-ASHI STREAK =====
        ha_dir = None
        if getattr(self.cfg, 'USE_HEIKIN_ASHI', True):
            ha_streak = heikin_ashi_streak(prices)
            if ha_streak is not None:
                notes += f" | HA={ha_streak:+d}"
                min_streak = getattr(self.cfg, 'MIN_HA_STREAK', 2)
                if ha_streak >= min_streak:
                    ha_dir = 1
                elif ha_streak <= -min_streak:
                    ha_dir = -1

        # ===== v3: EMA 5/20 CROSS =====
        ema_cross_dir = None
        if getattr(self.cfg, 'USE_EMA_CROSS', True):
            ec = ema_cross(prices, fast=5, slow=20)
            if ec is not None and ec != 0:
                ema_cross_dir = ec
                notes += f" | EMA5/20={'bull' if ec > 0 else 'bear'}"

        # === KONSENSUS: v3 genisletilmis ===
        # Temel sinyaller (v2): momentum, RSI, MACD
        # Ek sinyaller (v3): squeeze, HA streak, EMA cross
        consensus_count = 0
        total_signals = 3  # v2 base

        if mom_10m is not None and (1 if mom_10m > 0 else -1) == allowed_direction:
            consensus_count += 1
        if rsi_direction is not None and rsi_direction == allowed_direction:
            consensus_count += 1
        if macd_direction is not None and macd_direction == allowed_direction:
            consensus_count += 1

        # v3 bonus signals (konsensüse ekle ama zorunlu degil)
        v3_bonus = 0
        if squeeze_dir is not None and squeeze_dir == allowed_direction:
            v3_bonus += 1
        if ha_dir is not None and ha_dir == allowed_direction:
            v3_bonus += 1
        if ema_cross_dir is not None and ema_cross_dir == allowed_direction:
            v3_bonus += 1

        # En az 2 temel indicator trendle ayni yonde olmali
        if consensus_count < 2:
            return None, 0, notes + f" | LOW_CONSENSUS({consensus_count}/3)"

        # Confidence hesapla (0-1 arasi)
        strength_factors = []

        # Momentum gucu
        if mom_10m is not None:
            mom_strength = min(abs(mom_10m) / 0.005, 1.0)
            strength_factors.append(mom_strength)

        # RSI gucu
        if rsi_val is not None:
            if allowed_direction == 1:
                rsi_strength = min((rsi_val - 60) / 20, 1.0) if rsi_val > 60 else 0.3
                if rsi_val < self.cfg.RSI_OVERSOLD:
                    rsi_strength = 1.0
            else:
                rsi_strength = min((40 - rsi_val) / 20, 1.0) if rsi_val < 40 else 0.3
                if rsi_val > self.cfg.RSI_OVERBOUGHT:
                    rsi_strength = 1.0
            strength_factors.append(rsi_strength)

        # MACD gucu
        if histogram_val is not None and macd_direction == allowed_direction:
            price_ref = prices[-1] if prices else 1
            macd_strength = min(abs(histogram_val) / (price_ref * 0.001), 1.0)
            strength_factors.append(macd_strength)

        # ADX gucu
        if adx_val is not None:
            adx_strength = min((adx_val - 20) / 30, 1.0)
            strength_factors.append(adx_strength)

        # v3 bonus factors
        if v3_bonus > 0:
            # Her v3 sinyal uyumu confidence'a %10 bonus ekler
            v3_strength = min(v3_bonus * 0.15, 0.3)
            strength_factors.append(v3_strength + 0.5)  # base 0.5 + bonus

        if not strength_factors:
            return None, 0, notes

        confidence = sum(strength_factors) / len(strength_factors)
        confidence = min(confidence, 1.0)

        if confidence < self.cfg.MIN_SIGNAL_CONFIDENCE:
            return None, 0, notes + f" | LOW_CONF({confidence:.2f})"

        direction = allowed_direction
        total_with_bonus = consensus_count + v3_bonus
        notes += f" | conf={confidence:.2f} | consensus={consensus_count}+{v3_bonus}v3"

        return direction, confidence, notes

    def _calculate_realistic_edge(self, symbol, confidence, momentum, adx_val, rsi_val):
        """
        FIX 4: Gercekci edge hesaplama.

        Edge = (beklenen hareket * trend uyumu * indicator consensus) - maliyet

        Eski model: confidence'i direkt edge olarak kullaniyordu → gercekle uyumsuz.
        Yeni model: Volatilite, momentum buyuklugu ve maliyeti hesaba katar.
        """
        if momentum is None:
            return 0.0

        # 1. Beklenen hareket = mevcut momentum (yonde devam varsayimi)
        expected_move = abs(momentum)

        # 2. Trend alignment bonus (ADX gucuyse trend devam edecek)
        trend_bonus = 1.0
        if adx_val is not None:
            if adx_val > 40:
                trend_bonus = 1.5  # Guclu trend = momentum devam eder
            elif adx_val > 25:
                trend_bonus = 1.2
            else:
                trend_bonus = 0.8  # Zayif trend = riskli

        # 3. RSI extremity bonus
        rsi_bonus = 1.0
        if rsi_val is not None:
            if rsi_val < 25 or rsi_val > 75:
                rsi_bonus = 1.3  # Extreme RSI = buyuk hareket beklentisi
            elif rsi_val < 35 or rsi_val > 65:
                rsi_bonus = 1.1

        # 4. Maliyet cikart (giris fee + cikis fee + slippage)
        fee_cost = self.cfg.BINANCE_FEE_RATE  # 0.2% round-trip
        slippage = 0.001  # %0.1 ortalama slippage
        total_cost = fee_cost + slippage  # %0.3 toplam maliyet

        # 5. Mean-reversion riski: momentum her zaman devam etmez
        # Momentum'un sadece bir kisminin devam edecegini varsay
        continuation_factor = 0.6  # Momentum'un %60'i devam eder (konservatif)

        # Gercekci edge = beklenen hareket * devam orani * bonuslar - maliyetler
        gross_edge = expected_move * continuation_factor * trend_bonus * rsi_bonus
        net_edge = gross_edge - total_cost

        # Confidence ile scale (dusuk confidence = daha az guvenilir)
        realistic_edge = net_edge * confidence

        return max(realistic_edge, 0.0)
