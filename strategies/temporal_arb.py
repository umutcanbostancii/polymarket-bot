"""
Temporal Arbitrage
===================
Binance fiyat hareketini Polymarket'ten önce gör.
Momentum yönüne göre UP/DOWN pozisyon aç.
Signal fusion gate: RSI + MACD + OBI >= FUSION_THRESHOLD
"""

import logging
from datetime import datetime, timezone
from strategies.base import BaseStrategy
from signals.technical import rsi, macd
from signals.orderbook import order_book_imbalance

log = logging.getLogger(__name__)

COIN_MAP = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL",
}


class TemporalArbStrategy(BaseStrategy):

    name = "temporal_arb"
    FEE_RATE = 0.025

    @property
    def interval(self) -> float:
        return self.cfg.SCAN_INTERVAL

    async def scan(self):
        # Seans filtresi
        if not self._is_trading_session():
            return

        markets = await self.poly.get_crypto_markets()

        for market in markets:
            await self._check(market)

    def _is_trading_session(self) -> bool:
        """Sadece yuksek hacimli seanslarda trade ac."""
        if not self.cfg.USE_SESSION_FILTER:
            return True
        now_utc = datetime.now(timezone.utc)
        current_hour = now_utc.hour
        for session in self.cfg.TRADING_SESSIONS:
            if session["start_utc"] <= current_hour < session["end_utc"]:
                return True
        return False

    async def _check(self, market: dict):
        # Liquidity guard
        if market.get("liquidity", 0) < self.cfg.MIN_LIQUIDITY:
            return

        # Time remaining guard
        end_date = market.get("end_date", "")
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                remaining = (end_dt - datetime.now(timezone.utc)).total_seconds()
                if remaining < self.cfg.MIN_TIME_REMAINING:
                    return
            except (ValueError, TypeError):
                pass

        question = market.get("question", "").lower()

        # Hangi coin?
        symbol = None
        for kw, sym in COIN_MAP.items():
            if kw in question:
                symbol = sym
                break
        if not symbol:
            return

        # Binance fiyat
        bp = self.binance.get(symbol)
        if not bp or bp.age > 10:
            return

        # Momentum
        mom = self.binance.momentum(symbol, seconds=300)
        if mom is None or abs(mom) < self.cfg.MOMENTUM_THRESHOLD:
            return

        y = market.get("yes_price", 0)
        n = market.get("no_price", 0)
        if y <= 0 or n <= 0:
            return

        # Yön belirleme
        if mom > 0:
            implied = min(0.5 + mom * 10, 0.95)
            edge = implied - y - self.FEE_RATE
            side, price = "YES", y
        else:
            implied = min(0.5 + abs(mom) * 10, 0.95)
            edge = implied - n - self.FEE_RATE
            side, price = "NO", n

        if edge < self.cfg.MIN_EDGE:
            return

        # Signal fusion gate
        fusion = await self._signal_check(symbol, side, market)
        if fusion < self.cfg.FUSION_THRESHOLD:
            log.debug(
                f"⚡ TEMPORAL: {symbol} {side} fusion={fusion:.2f} < "
                f"{self.cfg.FUSION_THRESHOLD} — skipped"
            )
            return

        win_prob = min(implied, 0.90)
        size = self.risk.kelly_size(price, win_prob)
        if size <= 0:
            return

        self.db.log_opportunity(
            strategy=self.name, market_id=market["id"],
            question=market["question"], edge=edge,
            yes_price=y, no_price=n,
            binance_price=bp.price,
            action="signal",
            notes=f"{symbol} mom={mom:+.3%} side={side} size=${size:.2f} fusion={fusion:.2f}"
        )

        log.info(
            f"⚡ TEMPORAL: {symbol} {side} | mom={mom:+.3%} | "
            f"Binance=${bp.price:,.2f} | Y={y:.2f} N={n:.2f} | "
            f"edge={edge:.2%} size=${size:.2f} fusion={fusion:.2f}"
        )

        await self.executor.execute(
            self.name, market, side, price, size,
            notes=f"{symbol} momentum={mom:+.3%} fusion={fusion:.2f}"
        )

    async def _signal_check(self, symbol: str, side: str, market: dict) -> float:
        """Compute fusion score from RSI + MACD + OBI. Returns 0.0–1.0."""
        scores = []

        # RSI
        hist = self.binance.history(symbol)
        if hist:
            prices = hist.prices(100)
            rsi_val = rsi(prices, self.cfg.RSI_PERIOD)
            if rsi_val is not None:
                if side == "YES":
                    # Bullish: RSI < oversold is strong, > overbought is weak
                    score = max(0, (self.cfg.RSI_OVERBOUGHT - rsi_val) /
                                (self.cfg.RSI_OVERBOUGHT - self.cfg.RSI_OVERSOLD))
                else:
                    # Bearish: RSI > overbought is strong
                    score = max(0, (rsi_val - self.cfg.RSI_OVERSOLD) /
                                (self.cfg.RSI_OVERBOUGHT - self.cfg.RSI_OVERSOLD))
                scores.append(min(score, 1.0))

        # MACD
        if hist:
            prices = hist.prices(100)
            macd_line, signal_line, histogram = macd(prices)
            if histogram is not None:
                if side == "YES":
                    score = 1.0 if histogram > 0 else 0.3
                else:
                    score = 1.0 if histogram < 0 else 0.3
                scores.append(score)

        # OBI (Order Book Imbalance)
        token_ids = market.get("token_ids", [])
        token_id = token_ids[0] if side == "YES" and len(token_ids) > 0 else (
            token_ids[1] if side == "NO" and len(token_ids) > 1 else None
        )
        if token_id:
            book = await self.poly.get_orderbook(token_id)
            if book:
                obi = order_book_imbalance(book)
                if obi is not None:
                    # OBI ranges -1 to +1; normalize to 0-1 where positive = buy pressure
                    if side == "YES":
                        score = (obi + 1) / 2  # +1 → 1.0, -1 → 0.0
                    else:
                        score = (1 - obi) / 2  # -1 → 1.0, +1 → 0.0
                    scores.append(score)

        if not scores:
            return 0.0

        return sum(scores) / len(scores)
