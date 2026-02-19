"""Binance BTC/USDT real-time price feed for 5-min fast loop."""

import asyncio
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, List, Optional

import aiohttp

log = logging.getLogger(__name__)

SYMBOL = "BTCUSDT"
SYMBOL_LOWER = "btcusdt"


@dataclass
class PricePoint:
    price: float
    ts: float
    volume_24h: float = 0.0

    @property
    def age(self) -> float:
        return time.time() - self.ts


class BinanceFeed:

    def __init__(self, config):
        self.cfg = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._prices: deque = deque(maxlen=600)  # ~10 minutes at 1/sec
        self._volume_24h: float = 0.0
        self._callbacks: List[Callable] = []
        self._connected: bool = False
        self._last_ws_message: float = 0.0
        self._warmup_complete: bool = False
        self._reference_price: Optional[float] = None
        self._reference_ts: Optional[float] = None
        self._kline_1s_cache: Optional[list] = None
        self._kline_1s_cache_ts: float = 0.0

    def on_price(self, cb: Callable):
        self._callbacks.append(cb)

    @property
    def latest(self) -> Optional[PricePoint]:
        if not self._prices:
            return None
        p, t = self._prices[-1]
        return PricePoint(p, t, self._volume_24h)

    @property
    def price(self) -> Optional[float]:
        if not self._prices:
            return None
        return self._prices[-1][0]

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def warmup_complete(self) -> bool:
        return self._warmup_complete

    def set_reference_price(self, price: float):
        """Set the reference price for a 5-min market window."""
        self._reference_price = price
        self._reference_ts = time.time()
        log.info(f"Reference price set: ${price:,.2f}")

    def clear_reference_price(self):
        self._reference_price = None
        self._reference_ts = None

    @property
    def reference_price(self) -> Optional[float]:
        return self._reference_price

    def get_delta(self) -> Optional[float]:
        """Return (current - reference) / reference as a fraction."""
        if self._reference_price is None or not self._prices:
            return None
        current = self._prices[-1][0]
        return (current - self._reference_price) / self._reference_price

    def get_momentum(self, seconds: int = 30) -> Optional[float]:
        """Price change over last N seconds."""
        if len(self._prices) < 2:
            return None
        now = time.time()
        cutoff = now - seconds
        old_candidates = [p for p, t in self._prices if t <= cutoff]
        if not old_candidates:
            if len(self._prices) < 5:
                return None
            old_price = self._prices[0][0]
        else:
            old_price = old_candidates[-1]
        current = self._prices[-1][0]
        if old_price == 0:
            return None
        return (current - old_price) / old_price

    def get_volatility(self, seconds: int = 300) -> Optional[float]:
        """Standard deviation of tick-to-tick returns over last N seconds."""
        now = time.time()
        points = [(p, t) for p, t in self._prices if t >= now - seconds]
        if len(points) < 10:
            return None
        rets = []
        for i in range(1, len(points)):
            prev = points[i - 1][0]
            if prev == 0:
                continue
            rets.append((points[i][0] - prev) / prev)
        if len(rets) < 5:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        return math.sqrt(var)

    def prices(self, n: int = 200) -> List[float]:
        return [p for p, _ in list(self._prices)[-n:]]

    async def fetch_klines(self, interval: str = "1m", limit: int = 200) -> List[dict]:
        """Son N adet mum cek. AI prediction icin.
        Returns: [{"open":, "high":, "low":, "close":, "volume":, "ts":}, ...]"""
        if not self._session or self._session.closed:
            return []
        try:
            async with self._session.get(
                f"{self.cfg.BINANCE_REST}/klines",
                params={"symbol": SYMBOL, "interval": interval, "limit": limit},
            ) as r:
                if r.status != 200:
                    log.warning(f"fetch_klines HTTP {r.status}")
                    return []
                raw = await r.json()
                result = []
                for k in raw:
                    result.append({
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                        "ts": float(k[6]) / 1000.0,
                    })
                return result
        except Exception as e:
            log.error(f"fetch_klines error: {e}")
            return []

    async def fetch_klines_1s(self, limit: int = 60) -> list[dict]:
        """1-saniye mumlarini cek (cache: 5s TTL)."""
        now = time.time()
        if self._kline_1s_cache and now - self._kline_1s_cache_ts < 5.0:
            return self._kline_1s_cache[-limit:]

        klines = await self.fetch_klines(interval="1s", limit=limit)
        if klines:
            self._kline_1s_cache = klines
            self._kline_1s_cache_ts = now
        return (self._kline_1s_cache or [])[-limit:]

    async def fetch_depth(self, limit: int = 20) -> Optional[dict]:
        """Binance orderbook depth snapshot.
        Returns: {"bids": [[price, qty], ...], "asks": [[price, qty], ...]}"""
        if not self._session or self._session.closed:
            return None
        try:
            async with self._session.get(
                f"{self.cfg.BINANCE_REST}/depth",
                params={"symbol": SYMBOL, "limit": limit},
            ) as r:
                if r.status != 200:
                    log.warning(f"fetch_depth HTTP {r.status}")
                    return None
                data = await r.json()
                return {
                    "bids": [[float(p), float(q)] for p, q in data.get("bids", [])],
                    "asks": [[float(p), float(q)] for p, q in data.get("asks", [])],
                }
        except Exception as e:
            log.error(f"fetch_depth error: {e}")
            return None

    async def start(self):
        self._session = aiohttp.ClientSession()
        self._running = True
        await self._fetch_initial()
        asyncio.create_task(self._ws_loop())
        log.info("BinanceFeed ready (BTCUSDT only)")

    async def stop(self):
        self._running = False
        self._connected = False
        if self._session:
            await self._session.close()

    async def _fetch_initial(self):
        """Warm up with 100 recent 1m klines + current ticker."""
        try:
            async with self._session.get(
                f"{self.cfg.BINANCE_REST}/klines",
                params={"symbol": SYMBOL, "interval": "1m", "limit": 100},
            ) as r:
                if r.status == 200:
                    klines = await r.json()
                    now_s = time.time()
                    loaded = 0
                    for k in klines:
                        close_ts = float(k[6]) / 1000.0
                        if close_ts > now_s:
                            continue
                        close_price = float(k[4])
                        self._prices.append((close_price, close_ts))
                        loaded += 1
                    log.info(f"BTC warmup: {loaded} klines loaded")

            async with self._session.get(
                f"{self.cfg.BINANCE_REST}/ticker/24hr",
                params={"symbol": SYMBOL},
            ) as r:
                if r.status == 200:
                    t = await r.json()
                    price = float(t.get("lastPrice", 0))
                    self._volume_24h = float(t.get("quoteVolume", 0))
                    if price > 0:
                        self._prices.append((price, time.time()))
                    log.info(f"BTC: ${price:,.2f} vol=${self._volume_24h:,.0f}")
        except Exception as e:
            log.error(f"BTC warmup error: {e}")

        self._warmup_complete = True

    async def _ws_loop(self):
        delay = 1
        while self._running:
            try:
                url = f"{self.cfg.BINANCE_WS}/{SYMBOL_LOWER}@miniTicker"
                async with self._session.ws_connect(url, heartbeat=20) as ws:
                    delay = 1
                    self._connected = True
                    log.info("Binance WS connected (BTCUSDT)")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._last_ws_message = time.time()
                            await self._handle(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
            except Exception as e:
                log.error(f"Binance WS error: {e}")

            self._connected = False
            if self._running:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _handle(self, raw: str):
        try:
            ticker = json.loads(raw)
            price = float(ticker.get("c", 0))
            if price <= 0:
                return

            vol_quote = float(ticker.get("q", 0))
            if vol_quote > 0:
                self._volume_24h = vol_quote

            ts = time.time()
            self._prices.append((price, ts))

            pp = PricePoint(price, ts, self._volume_24h)
            for cb in self._callbacks:
                try:
                    if asyncio.iscoroutinefunction(cb):
                        await cb(pp)
                    else:
                        cb(pp)
                except Exception as e:
                    log.error(f"callback error: {e}")
        except Exception as e:
            log.error(f"handle error: {e}")
