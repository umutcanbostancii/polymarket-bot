"""Binance market data feed with microstructure and data-quality metrics."""

import asyncio
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import aiohttp

log = logging.getLogger(__name__)

SYMBOLS = {
    "btcusdt": "BTC",
    "ethusdt": "ETH",
    "solusdt": "SOL",
    "xrpusdt": "XRP",
    "dogeusdt": "DOGE",
    "avaxusdt": "AVAX",
    "adausdt": "ADA",
    "linkusdt": "LINK",
    "dotusdt": "DOT",
    "maticusdt": "MATIC",
}

BINANCE_SYMBOL_BY_NAME = {v: k.upper() for k, v in SYMBOLS.items()}

# timeframe -> (base_binance_interval, aggregation_factor, seconds_per_bar)
CHART_TIMEFRAME_SPECS: Dict[str, Tuple[str, int, int]] = {
    "1m": ("1m", 1, 60),
    "5m": ("5m", 1, 300),
    "10m": ("5m", 2, 600),
    "15m": ("15m", 1, 900),
    "30m": ("30m", 1, 1800),
    "45m": ("15m", 3, 2700),
    "60m": ("1h", 1, 3600),
    "2h": ("2h", 1, 7200),
    "4h": ("4h", 1, 14400),
    "6h": ("6h", 1, 21600),
    "8h": ("8h", 1, 28800),
    "12h": ("12h", 1, 43200),
    "24h": ("1d", 1, 86400),
    "1d": ("1d", 1, 86400),
    "3d": ("3d", 1, 259200),
    "5d": ("1d", 5, 432000),
    "7d": ("1d", 7, 604800),
    "10d": ("1d", 10, 864000),
    "20d": ("1d", 20, 1728000),
    "30d": ("1d", 30, 2592000),
    "1w": ("1w", 1, 604800),
    "2w": ("1w", 2, 1209600),
    "3w": ("1w", 3, 1814400),
    "4w": ("1w", 4, 2419200),
    "1M": ("1M", 1, 2592000),
}

TIMEFRAME_ALIASES = {
    "1h": "60m",
    "1H": "60m",
    "24H": "24h",
    "1D": "1d",
    "3D": "3d",
    "1W": "1w",
    "1MO": "1M",
    "1mo": "1M",
    "1MONTH": "1M",
    "1month": "1M",
    "MONTHLY": "1M",
    "monthly": "1M",
}


@dataclass
class PricePoint:
    symbol: str
    price: float
    ts: float
    volume_24h: float = 0.0

    @property
    def age(self) -> float:
        return time.time() - self.ts


class PriceHistory:

    def __init__(self, symbol: str, maxlen: int = 2000):
        self.symbol = symbol
        self._data: deque = deque(maxlen=maxlen)
        self._flow: deque = deque(maxlen=maxlen)
        self.volume_24h: float = 0.0
        self.volume_base_24h: float = 0.0

    def add(
        self,
        price: float,
        ts: float,
        quote_volume_24h: float = 0.0,
        base_volume_24h: float = 0.0,
    ):
        prev_price = self._data[-1][0] if self._data else price
        prev_quote_vol = self.volume_24h

        quote_delta = 0.0
        if quote_volume_24h > 0:
            if prev_quote_vol > 0:
                quote_delta = quote_volume_24h - prev_quote_vol
                if quote_delta < 0:
                    # 24h rolling window reset/rollover.
                    quote_delta = 0.0
            self.volume_24h = quote_volume_24h

        if base_volume_24h > 0:
            self.volume_base_24h = base_volume_24h

        signed_flow = 0.0
        if quote_delta > 0:
            if price > prev_price:
                signed_flow = quote_delta
            elif price < prev_price:
                signed_flow = -quote_delta

        self._data.append((price, ts))
        self._flow.append((ts, signed_flow, quote_delta))

    @property
    def latest(self) -> Optional[PricePoint]:
        if not self._data:
            return None
        p, t = self._data[-1]
        return PricePoint(self.symbol, p, t, self.volume_24h)

    def prices(self, n: int = 200) -> List[float]:
        return [p for p, _ in list(self._data)[-n:]]

    def change_pct(self, seconds: int = 300) -> Optional[float]:
        if len(self._data) < 2:
            return None

        now = time.time()
        newest_ts = self._data[-1][1]
        if now - newest_ts > seconds * 2:
            return None

        cutoff = now - seconds
        old_candidates = [p for p, t in self._data if t <= cutoff]
        if not old_candidates:
            oldest_ts = self._data[0][1]
            if now - oldest_ts < seconds * 0.5:
                return None
            old_price = self._data[0][0]
        else:
            old_price = old_candidates[-1]

        new_price = self._data[-1][0]
        if old_price == 0:
            return None
        return (new_price - old_price) / old_price

    def returns(self, n: int = 200) -> List[float]:
        prices = self.prices(n + 1)
        if len(prices) < 2:
            return []
        out = []
        for i in range(1, len(prices)):
            prev = prices[i - 1]
            if prev == 0:
                continue
            out.append((prices[i] - prev) / prev)
        return out

    def volatility(self, seconds: int = 300) -> Optional[float]:
        now = time.time()
        points = [p for p in self._data if p[1] >= now - seconds]
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

    def microstructure(self, window_seconds: int = 180) -> dict:
        latest = self.latest
        if not latest:
            return {
                "data_points": 0,
                "vwap": None,
                "price_vs_vwap_pct": None,
                "cvd": 0.0,
                "cvd_ratio": 0.0,
                "volatility": None,
            }

        now = time.time()
        recent_prices = [(p, t) for p, t in self._data if t >= now - window_seconds]
        recent_flow = [(ts, sf, qd) for ts, sf, qd in self._flow if ts >= now - window_seconds]

        if recent_flow:
            vol_by_ts = {ts: qd for ts, _, qd in recent_flow if qd > 0}
            weighted = []
            for p, t in recent_prices:
                w = vol_by_ts.get(t, 0.0)
                if w > 0:
                    weighted.append((p, w))

            if weighted:
                total_w = sum(w for _, w in weighted)
                vwap = sum(p * w for p, w in weighted) / total_w if total_w else latest.price
            else:
                vwap = sum(p for p, _ in recent_prices) / len(recent_prices)

            cvd = sum(sf for _, sf, _ in recent_flow)
            abs_flow = sum(abs(sf) for _, sf, _ in recent_flow)
            cvd_ratio = cvd / abs_flow if abs_flow > 0 else 0.0
        else:
            vwap = sum(p for p, _ in recent_prices) / len(recent_prices) if recent_prices else latest.price
            cvd = 0.0
            cvd_ratio = 0.0

        price_vs_vwap_pct = (latest.price - vwap) / vwap if vwap else 0.0

        return {
            "data_points": len(recent_prices),
            "vwap": vwap,
            "price_vs_vwap_pct": price_vs_vwap_pct,
            "cvd": cvd,
            "cvd_ratio": cvd_ratio,
            "volatility": self.volatility(window_seconds),
        }

    def data_quality(self, max_age: float = 10.0, min_points: int = 40, window_seconds: int = 300) -> dict:
        latest = self.latest
        if not latest:
            return {
                "score": 0.0,
                "stale": True,
                "insufficient_points": True,
                "jump_count": 0,
                "has_volume": False,
            }

        now = time.time()
        age = now - latest.ts
        recent = [(p, t) for p, t in self._data if t >= now - window_seconds]

        rets = []
        for i in range(1, len(recent)):
            prev = recent[i - 1][0]
            if prev == 0:
                continue
            rets.append((recent[i][0] - prev) / prev)

        jump_count = sum(1 for r in rets if abs(r) > 0.02)

        score = 1.0
        if age > max_age:
            score -= 0.45
        if len(recent) < min_points:
            score -= 0.35
        if jump_count > 2:
            score -= 0.2
        if self.volume_24h <= 0:
            score -= 0.1

        return {
            "score": round(max(score, 0.0), 3),
            "stale": age > max_age,
            "insufficient_points": len(recent) < min_points,
            "jump_count": jump_count,
            "has_volume": self.volume_24h > 0,
        }


class BinanceFeed:

    def __init__(self, config):
        self.cfg = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._histories: Dict[str, PriceHistory] = {
            name: PriceHistory(name) for name in SYMBOLS.values()
        }
        self._callbacks: List[Callable] = []
        self._connected: bool = False
        self._last_ws_message: float = 0.0
        self._msg_count: int = 0
        self._msg_count_start: float = time.time()
        self._warmup_complete: bool = False
        self._warmup_ts: float = 0.0
        self._degraded_symbols: Dict[str, str] = {}  # symbol -> reason
        self._chart_cache: Dict[Tuple[str, str, int], dict] = {}

    def on_price(self, cb: Callable):
        self._callbacks.append(cb)

    def get(self, symbol: str) -> Optional[PricePoint]:
        h = self._histories.get(symbol.upper())
        return h.latest if h else None

    def history(self, symbol: str) -> Optional[PriceHistory]:
        return self._histories.get(symbol.upper())

    def momentum(self, symbol: str, seconds: int = 300) -> Optional[float]:
        h = self._histories.get(symbol.upper())
        return h.change_pct(seconds) if h else None

    def volume(self, symbol: str) -> float:
        h = self._histories.get(symbol.upper())
        return h.volume_24h if h else 0.0

    def volume_base(self, symbol: str) -> float:
        h = self._histories.get(symbol.upper())
        return h.volume_base_24h if h else 0.0

    def volatility(self, symbol: str, seconds: int = 300) -> Optional[float]:
        h = self._histories.get(symbol.upper())
        return h.volatility(seconds) if h else None

    def microstructure(self, symbol: str, window_seconds: int = 180) -> dict:
        h = self._histories.get(symbol.upper())
        if not h:
            return {
                "data_points": 0,
                "vwap": None,
                "price_vs_vwap_pct": None,
                "cvd": 0.0,
                "cvd_ratio": 0.0,
                "volatility": None,
            }
        return h.microstructure(window_seconds)

    def quality(self, symbol: str) -> dict:
        h = self._histories.get(symbol.upper())
        if not h:
            return {
                "score": 0.0,
                "stale": True,
                "insufficient_points": True,
                "jump_count": 0,
                "has_volume": False,
            }
        return h.data_quality()

    @property
    def warmup_complete(self) -> bool:
        return self._warmup_complete

    @property
    def warmup_ts(self) -> float:
        return self._warmup_ts

    def is_degraded(self, symbol: str) -> Optional[str]:
        return self._degraded_symbols.get(symbol.upper())

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def health_status(self) -> dict:
        return {
            "connected": self._connected,
            "last_message": self._last_ws_message,
            "warmup_complete": self._warmup_complete,
            "degraded_symbols": dict(self._degraded_symbols),
        }

    @property
    def messages_per_sec(self) -> float:
        elapsed = time.time() - self._msg_count_start
        return self._msg_count / elapsed if elapsed > 0 else 0.0

    def export_history(self, symbol: str, n: int = 300) -> list:
        h = self._histories.get(symbol.upper())
        if not h:
            return []
        data = list(h._data)[-n:]
        return [{"price": p, "ts": t} for p, t in data]

    def export_all(self) -> dict:
        now = time.time()
        result = {}
        for sym_name in SYMBOLS.values():
            pp = self.get(sym_name)
            hist = self.history(sym_name)
            micro = self.microstructure(sym_name)
            quality = self.quality(sym_name)

            result[sym_name] = {
                "price": pp.price if pp else None,
                "volume_24h": hist.volume_24h if hist else 0.0,
                "age": round(pp.age, 1) if pp else None,
                "change_1m": hist.change_pct(60) if hist else None,
                "change_5m": hist.change_pct(300) if hist else None,
                "change_15m": hist.change_pct(900) if hist else None,
                "volatility_5m": hist.volatility(300) if hist else None,
                "micro": micro,
                "quality": quality,
                "history": self.export_history(sym_name, 300),
                "data_points": len(hist._data) if hist else 0,
            }

        result["_meta"] = {
            "connected": self._connected,
            "last_message_age": round(now - self._last_ws_message, 1) if self._last_ws_message else None,
            "messages_per_sec": round(self.messages_per_sec, 1),
            "total_messages": self._msg_count,
        }
        return result

    def _normalize_timeframe(self, timeframe: str) -> str:
        raw = str(timeframe or "1m").strip()
        if raw in CHART_TIMEFRAME_SPECS:
            return raw
        if raw in TIMEFRAME_ALIASES:
            mapped = TIMEFRAME_ALIASES[raw]
            if mapped in CHART_TIMEFRAME_SPECS:
                return mapped

        low = raw.lower()
        for key in CHART_TIMEFRAME_SPECS:
            if key.lower() == low:
                return key
        if low in TIMEFRAME_ALIASES:
            mapped = TIMEFRAME_ALIASES[low]
            if mapped in CHART_TIMEFRAME_SPECS:
                return mapped
        return "1m"

    def _cache_ttl_seconds(self, tf_seconds: int) -> float:
        if tf_seconds <= 300:
            return 4.0
        if tf_seconds <= 3600:
            return 8.0
        if tf_seconds <= 86400:
            return 25.0
        if tf_seconds <= 7 * 86400:
            return 60.0
        return 180.0

    def _recommended_limit(self, tf_seconds: int, requested_limit: int) -> int:
        if tf_seconds <= 3600:
            cap = 360
        elif tf_seconds <= 86400:
            cap = 320
        elif tf_seconds <= 7 * 86400:
            cap = 260
        else:
            cap = 180
        return max(40, min(int(requested_limit), cap))

    async def _fetch_klines(self, symbol: str, interval: str, limit: int) -> List[list]:
        if not self._session:
            return []
        safe_limit = max(20, min(int(limit), 1000))
        try:
            async with self._session.get(
                f"{self.cfg.BINANCE_REST}/klines",
                params={"symbol": symbol.upper(), "interval": interval, "limit": safe_limit},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status != 200:
                    return []
                payload = await r.json()
                if isinstance(payload, list):
                    return payload
                return []
        except Exception:
            return []

    def _normalize_kline_rows(self, rows: List[list]) -> List[dict]:
        out = []
        for r in rows:
            try:
                out.append(
                    {
                        "open_time": int(r[0]),
                        "open": float(r[1]),
                        "high": float(r[2]),
                        "low": float(r[3]),
                        "close": float(r[4]),
                        "volume": float(r[5]),
                        "close_time": int(r[6]),
                    }
                )
            except Exception:
                continue
        return out

    def _aggregate_klines(self, rows: List[dict], factor: int) -> List[dict]:
        if factor <= 1 or not rows:
            return rows
        if len(rows) < factor:
            return []

        # Align groups from the end so the newest bar always remains complete.
        offset = len(rows) % factor
        grouped = []
        for i in range(offset, len(rows), factor):
            chunk = rows[i:i + factor]
            if len(chunk) < factor:
                continue
            grouped.append(
                {
                    "open_time": chunk[0]["open_time"],
                    "open": chunk[0]["open"],
                    "high": max(c["high"] for c in chunk),
                    "low": min(c["low"] for c in chunk),
                    "close": chunk[-1]["close"],
                    "volume": sum(c["volume"] for c in chunk),
                    "close_time": chunk[-1]["close_time"],
                }
            )
        return grouped

    def _fallback_candles_from_history(self, symbol: str, limit: int) -> List[dict]:
        points = self.export_history(symbol, limit)
        out = []
        for p in points:
            px = float(p["price"])
            ts = int(float(p["ts"]) * 1000)
            out.append(
                {
                    "open_time": ts,
                    "open": px,
                    "high": px,
                    "low": px,
                    "close": px,
                    "volume": 0.0,
                    "close_time": ts,
                }
            )
        return out

    async def export_chart(self, symbol: str, timeframe: str = "1m", limit: int = 320) -> dict:
        sym = (symbol or "").upper()
        if sym not in BINANCE_SYMBOL_BY_NAME:
            return {"symbol": sym, "timeframe": "1m", "seconds_per_bar": 60, "candles": []}

        tf = self._normalize_timeframe(timeframe)
        base_interval, factor, tf_seconds = CHART_TIMEFRAME_SPECS[tf]
        limit = self._recommended_limit(tf_seconds, limit)

        cache_key = (sym, tf, limit)
        now = time.time()
        cache_hit = self._chart_cache.get(cache_key)
        if cache_hit:
            ttl = self._cache_ttl_seconds(tf_seconds)
            if now - float(cache_hit.get("ts", 0.0)) <= ttl:
                return cache_hit["payload"]

        base_limit = min(1000, max(limit * factor + factor * 6, 80))
        raw = await self._fetch_klines(BINANCE_SYMBOL_BY_NAME[sym], base_interval, base_limit)
        rows = self._normalize_kline_rows(raw)
        now_ms = int(now * 1000)
        # Keep only completed candles for stable signal logic.
        rows = [r for r in rows if int(r.get("close_time", 0)) <= now_ms]
        if factor > 1:
            rows = self._aggregate_klines(rows, factor)
        if not rows:
            rows = self._fallback_candles_from_history(sym, limit)

        rows = rows[-limit:]
        candles = [
            {
                "ts": float(r["close_time"]) / 1000.0,
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
                "volume": r["volume"],
            }
            for r in rows
        ]

        payload = {
            "symbol": sym,
            "timeframe": tf,
            "seconds_per_bar": tf_seconds,
            "candles": candles,
        }
        self._chart_cache[cache_key] = {"ts": now, "payload": payload}
        return payload

    async def export_chart_bundle(self, timeframe: str = "1m", limit: int = 320) -> dict:
        out = {}
        for sym in ("BTC", "ETH", "SOL"):
            out[sym] = await self.export_chart(sym, timeframe=timeframe, limit=limit)
        return out

    async def start(self):
        self._session = aiohttp.ClientSession()
        self._running = True
        await self._fetch_initial()
        asyncio.create_task(self._ws_loop())
        log.info("BinanceFeed ready")

    async def stop(self):
        self._running = False
        self._connected = False
        if self._session:
            await self._session.close()

    async def _fetch_initial(self):
        kline_limit = int(getattr(self.cfg, "BINANCE_KLINE_WARMUP", 500))
        now_s = time.time()

        for sym_key, sym_name in SYMBOLS.items():
            loaded = 0
            gaps = 0
            try:
                # Fetch historical 1m klines for indicator warm-up.
                async with self._session.get(
                    f"{self.cfg.BINANCE_REST}/klines",
                    params={
                        "symbol": sym_key.upper(),
                        "interval": "1m",
                        "limit": kline_limit,
                    },
                ) as r:
                    if r.status == 200:
                        klines = await r.json()
                        hist = self._histories[sym_name]
                        prev_close_ts = 0.0

                        for k in klines:
                            # k: [open_time, open, high, low, close, vol, close_time, quote_vol, ...]
                            close_ts = float(k[6]) / 1000.0  # ms -> s

                            # Skip the current (still open) candle: its close_time is in the future.
                            if close_ts > now_s:
                                continue

                            # Gap detection: consecutive 1m candles should be ~60s apart.
                            if prev_close_ts > 0:
                                delta = close_ts - prev_close_ts
                                if delta > 180:  # >3 min gap = missing candles
                                    gaps += 1
                            prev_close_ts = close_ts

                            close_price = float(k[4])
                            hist.add(close_price, close_ts, quote_volume_24h=0.0, base_volume_24h=0.0)
                            loaded += 1

                # Mark degraded if too many gaps or too few candles loaded.
                min_candles = int(kline_limit * 0.8)
                if loaded < min_candles:
                    self._degraded_symbols[sym_name] = f"low_candles:{loaded}/{kline_limit}"
                elif gaps > 5:
                    self._degraded_symbols[sym_name] = f"gaps:{gaps}"

                # Fetch current 24h ticker for volume + latest price.
                async with self._session.get(
                    f"{self.cfg.BINANCE_REST}/ticker/24hr",
                    params={"symbol": sym_key.upper()},
                ) as r:
                    if r.status == 200:
                        t = await r.json()
                        price = float(t.get("lastPrice", 0))
                        vol_quote = float(t.get("quoteVolume", 0))
                        vol_base = float(t.get("volume", 0))
                        if price > 0:
                            hist = self._histories[sym_name]
                            hist.add(price, time.time(), quote_volume_24h=vol_quote, base_volume_24h=vol_base)
                        deg = f" DEGRADED({self._degraded_symbols[sym_name]})" if sym_name in self._degraded_symbols else ""
                        gap_info = f" gaps={gaps}" if gaps else ""
                        log.info(f"  {sym_name}: ${price:,.4f} ({loaded} klines{gap_info}, vol=${vol_quote:,.0f}){deg}")
                    else:
                        log.info(f"  {sym_name}: {loaded} klines loaded")
            except Exception as e:
                log.error(f"init {sym_name}: {e}")
                self._degraded_symbols[sym_name] = "fetch_failed"
                # Fallback: at least try to get current price.
                try:
                    async with self._session.get(
                        f"{self.cfg.BINANCE_REST}/ticker/price",
                        params={"symbol": sym_key.upper()},
                    ) as r:
                        if r.status == 200:
                            d = await r.json()
                            p = float(d["price"])
                            self._histories[sym_name].add(p, time.time())
                            log.info(f"  {sym_name}: ${p:,.4f} (fallback, no klines)")
                except Exception as e2:
                    log.error(f"init fallback {sym_name}: {e2}")

        self._warmup_complete = True
        self._warmup_ts = time.time()
        total_degraded = len(self._degraded_symbols)
        log.info(f"Warmup complete: {len(SYMBOLS)} symbols, {total_degraded} degraded")

    async def _ws_loop(self):
        delay = 1
        while self._running:
            try:
                streams = "/".join(f"{s}@miniTicker" for s in SYMBOLS)
                base = self.cfg.BINANCE_WS.rsplit("/ws", 1)[0]
                url = f"{base}/stream?streams={streams}"

                async with self._session.ws_connect(url, heartbeat=20) as ws:
                    delay = 1
                    self._connected = True
                    log.info("Binance WS connected")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._last_ws_message = time.time()
                            self._msg_count += 1
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
            data = json.loads(raw)
            ticker = data.get("data", data)
            sym_raw = ticker.get("s", "").lower()

            sym_name = None
            for k, v in SYMBOLS.items():
                if k == sym_raw or k.replace("usdt", "") == sym_raw.replace("usdt", ""):
                    sym_name = v
                    break
            if not sym_name:
                return

            price = float(ticker.get("c", 0))
            if price <= 0:
                return

            vol_base = float(ticker.get("v", 0))
            vol_quote = float(ticker.get("q", 0))
            ts = time.time()
            self._histories[sym_name].add(price, ts, quote_volume_24h=vol_quote, base_volume_24h=vol_base)

            pp = PricePoint(sym_name, price, ts, vol_quote)
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
