"""Polymarket Gamma + CLOB client with WS orderbook cache and resilient REST fallback."""

import asyncio
import contextlib
import json
import logging
import time
from typing import Dict, List, Optional, Sequence, Tuple

import aiohttp

log = logging.getLogger(__name__)


class PolymarketClient:

    def __init__(self, config):
        self.cfg = config
        self.session: Optional[aiohttp.ClientSession] = None

        self._last_success: float = 0.0
        self._consecutive_errors: int = 0
        self._circuit_open_until: float = 0.0

        self._running = False
        self._ws_connected = False
        self._ws_last_message: float = 0.0
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_disabled = False

        self._orderbook_cache: Dict[str, dict] = {}
        self._subscribed_tokens = set()
        self._subscription_lock = asyncio.Lock()

        self._last_rest_request_ts = 0.0

    # ------------------------------------------------------------------
    # Base URLs and headers
    # ------------------------------------------------------------------

    @property
    def _gamma_bases(self) -> List[str]:
        urls = [self.cfg.GAMMA_API] + list(getattr(self.cfg, "GAMMA_API_FALLBACKS", []) or [])
        return self._uniq_urls(urls)

    @property
    def _clob_bases(self) -> List[str]:
        urls = [self.cfg.CLOB_API] + list(getattr(self.cfg, "CLOB_API_FALLBACKS", []) or [])
        return self._uniq_urls(urls)

    @staticmethod
    def _uniq_urls(urls: Sequence[str]) -> List[str]:
        out = []
        seen = set()
        for u in urls:
            if not u:
                continue
            v = u.rstrip("/")
            if v in seen:
                continue
            seen.add(v)
            out.append(v)
        return out

    def _clob_headers(self) -> Dict:
        if not self.cfg.POLY_API_KEY:
            return {}
        return {
            "POLY_API_KEY": self.cfg.POLY_API_KEY,
            "POLY_API_SECRET": self.cfg.POLY_API_SECRET,
            "POLY_API_PASSPHRASE": self.cfg.POLY_API_PASSPHRASE,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=float(getattr(self.cfg, "POLY_REST_TIMEOUT_SECONDS", 10)))
        self.session = aiohttp.ClientSession(timeout=timeout)
        self._last_success = time.time()
        self._running = True

        ws_url = getattr(self.cfg, "POLY_WS_URL", "")
        if ws_url and not self._ws_disabled:
            self._ws_task = asyncio.create_task(self._ws_loop(ws_url), name="poly-ws")

        has_creds = "with API credentials" if self.cfg.POLY_API_KEY else "without credentials"
        log.info(f"PolymarketClient ready ({has_creds})")

    async def stop(self):
        self._running = False
        self._ws_connected = False

        if self._ws_task:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._ws_task
            self._ws_task = None

        if self.session:
            await self.session.close()

    def _ensure_session(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=float(getattr(self.cfg, "POLY_REST_TIMEOUT_SECONDS", 10)))
            self.session = aiohttp.ClientSession(timeout=timeout)

    @property
    def healthy(self) -> bool:
        return (time.time() - self._last_success < 60) or (self._consecutive_errors < 5)

    @property
    def health_status(self) -> dict:
        now = time.time()
        return {
            "healthy": self.healthy,
            "last_success": self._last_success,
            "consecutive_errors": self._consecutive_errors,
            "ws_connected": self._ws_connected,
            "ws_last_message_age": (now - self._ws_last_message) if self._ws_last_message else None,
            "circuit_open": now < self._circuit_open_until,
            "circuit_open_for": max(0.0, self._circuit_open_until - now),
            "cached_orderbooks": len(self._orderbook_cache),
        }

    # ------------------------------------------------------------------
    # REST request resilience
    # ------------------------------------------------------------------

    def _circuit_blocked(self) -> bool:
        return time.time() < self._circuit_open_until

    def _mark_success(self):
        self._last_success = time.time()
        self._consecutive_errors = 0

    def _mark_error(self):
        self._consecutive_errors += 1
        err_limit = int(getattr(self.cfg, "POLY_CIRCUIT_BREAKER_ERRORS", 5))
        if self._consecutive_errors >= err_limit:
            cooldown = float(getattr(self.cfg, "POLY_CIRCUIT_BREAKER_COOLDOWN_SECONDS", 25))
            self._circuit_open_until = time.time() + cooldown

    async def _throttle(self):
        max_rps = float(getattr(self.cfg, "POLY_MAX_REQUESTS_PER_SEC", 25.0))
        min_interval = 1.0 / max(max_rps, 1.0)
        now = time.time()
        wait = self._last_rest_request_ts + min_interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_rest_request_ts = time.time()

    async def _request_json(
        self,
        bases: Sequence[str],
        path: str,
        params: dict = None,
        headers: dict = None,
    ) -> Optional[dict]:
        self._ensure_session()

        if not path.startswith("/"):
            path = "/" + path

        max_attempts = int(getattr(self.cfg, "POLY_RETRY_MAX_ATTEMPTS", 3))
        headers = headers or {}

        for attempt in range(max_attempts):
            if self._circuit_blocked():
                await asyncio.sleep(max(0.1, self._circuit_open_until - time.time()))

            for base in bases:
                await self._throttle()
                url = base + path
                try:
                    async with self.session.get(url, params=params, headers=headers) as r:
                        if r.status == 200:
                            data = await r.json()
                            self._mark_success()
                            return data

                        # Retry-friendly statuses.
                        if r.status in (429, 500, 502, 503, 504):
                            self._mark_error()
                            continue

                        # Non-retryable HTTP statuses.
                        self._mark_error()
                        text = await r.text()
                        log.warning(f"Polymarket HTTP {r.status} {url}: {text[:200]}")
                        continue
                except Exception as exc:
                    self._mark_error()
                    log.warning(f"Polymarket request failed {url}: {exc}")
                    continue

            if attempt < max_attempts - 1:
                await asyncio.sleep(0.15 * (2 ** attempt))

        return None

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def get_markets(self, keyword: str, limit: int = 50) -> List[Dict]:
        data = await self._request_json(
            self._gamma_bases,
            "/markets",
            params={"active": "true", "closed": "false", "limit": limit},
        )
        if not data:
            return []

        out = []
        for raw in data:
            question = (raw.get("question") or "").lower()
            if keyword.lower() in question:
                out.append(self._parse(raw))
        return out

    async def get_crypto_markets(self) -> List[Dict]:
        all_m = []
        for kw in self.cfg.CRYPTO_MARKETS:
            all_m.extend(await self.get_markets(kw))
            await asyncio.sleep(0.05)
        all_m = self._dedupe_by_id(all_m)
        if all_m:
            return all_m

        # Fallback discovery: keyword drift is common on Polymarket.
        fallback_terms = [
            "bitcoin", "btc",
            "ethereum", "eth",
            "solana", "sol",
            "dogecoin", "doge",
            "xrp", "cardano", "ada",
            "up or down", "price",
        ]
        data = await self._request_json(
            self._gamma_bases,
            "/markets",
            params={"active": "true", "closed": "false", "limit": 250},
        )
        if not data:
            return []

        out = []
        for raw in data:
            q = (raw.get("question") or "").lower()
            if any(t in q for t in fallback_terms):
                out.append(self._parse(raw))
        return self._dedupe_by_id(out)

    async def get_weather_markets(self) -> List[Dict]:
        keywords = ["highest temperature", "lowest temperature"]
        data = await self._request_json(
            self._gamma_bases,
            "/markets",
            params={"active": "true", "closed": "false", "limit": 100},
        )
        if not data:
            return []

        out = []
        for raw in data:
            q = (raw.get("question") or "").lower()
            if any(k in q for k in keywords):
                out.append(self._parse(raw))
        return self._dedupe_by_id(out)

    @staticmethod
    def _dedupe_by_id(markets: Sequence[Dict]) -> List[Dict]:
        seen = set()
        out = []
        for m in markets:
            mid = m.get("id")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            out.append(m)
        return out

    # ------------------------------------------------------------------
    # Orderbook (WS cache + REST fallback)
    # ------------------------------------------------------------------

    async def subscribe_orderbooks(self, token_ids: Sequence[str]):
        if not token_ids:
            return
        async with self._subscription_lock:
            for t in token_ids:
                if t:
                    self._subscribed_tokens.add(str(t))

    def _orderbook_from_cache(self, token_id: str, max_age_ms: Optional[int] = None) -> Optional[dict]:
        max_age_ms = int(max_age_ms or getattr(self.cfg, "POLY_ORDERBOOK_STALE_MS", 1200))
        item = self._orderbook_cache.get(str(token_id))
        if not item:
            return None
        age_ms = (time.time() - item.get("ts", 0)) * 1000.0
        if age_ms > max_age_ms:
            return None
        return item.get("book")

    async def get_orderbook(self, token_id: str) -> Optional[Dict]:
        token_id = str(token_id)

        cached = self._orderbook_from_cache(token_id)
        if cached is not None:
            return cached

        # Keep WS subscriptions warm even when REST is used.
        await self.subscribe_orderbooks([token_id])

        book = await self._request_json(
            self._clob_bases,
            "/book",
            params={"token_id": token_id},
            headers=self._clob_headers(),
        )
        if book:
            self._orderbook_cache[token_id] = {
                "book": book,
                "ts": time.time(),
                "latency_ms": None,
                "source": "rest",
            }
        return book

    async def orderbook_snapshot(self, token_id: str, max_age_ms: Optional[int] = None) -> Optional[Dict]:
        token_id = str(token_id)
        cached = self._orderbook_from_cache(token_id, max_age_ms=max_age_ms)
        if cached is not None:
            return cached
        return await self.get_orderbook(token_id)

    def orderbook_latency_ms(self, token_id: str) -> Optional[float]:
        item = self._orderbook_cache.get(str(token_id))
        if not item:
            return None
        lat = item.get("latency_ms")
        if lat is not None:
            return lat
        return (time.time() - item.get("ts", 0)) * 1000.0

    # ------------------------------------------------------------------
    # WebSocket loop
    # ------------------------------------------------------------------

    async def _ws_loop(self, ws_url: str):
        delay = 1.0

        while self._running:
            try:
                self._ensure_session()
                async with self.session.ws_connect(ws_url, heartbeat=20) as ws:
                    self._ws_connected = True
                    delay = 1.0
                    log.info("Polymarket WS connected")

                    await self._send_ws_subscriptions(ws)

                    while self._running:
                        # Refresh subscriptions periodically (new tokens may be added).
                        await self._send_ws_subscriptions(ws)

                        msg = await ws.receive(timeout=1.0)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._ws_last_message = time.time()
                            self._handle_ws_message(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            self._ws_last_message = time.time()
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning(f"Polymarket WS error: {exc}")
                if "Invalid response status" in str(exc):
                    # Endpoint/version mismatch; keep running with REST fallback.
                    self._ws_disabled = True
                    log.warning("Polymarket WS disabled due to unsupported endpoint; using REST fallback")
                    break
            finally:
                self._ws_connected = False

            if self._running:
                await asyncio.sleep(delay)
                delay = min(delay * 1.8, 15.0)

    async def _send_ws_subscriptions(self, ws):
        async with self._subscription_lock:
            tokens = list(self._subscribed_tokens)

        if not tokens:
            return

        # The feed has changed payload schema over time; send compatible variants.
        payload_variants = [
            {"type": "subscribe", "channel": "book", "asset_ids": tokens},
            {"type": "subscribe", "channel": "book", "assets_ids": tokens},
            {"event": "subscribe", "channel": "book", "asset_ids": tokens},
        ]

        for payload in payload_variants:
            try:
                await ws.send_json(payload)
            except Exception:
                # Non-fatal; next variant may still work.
                continue

    def _handle_ws_message(self, raw: str):
        try:
            data = json.loads(raw)
        except Exception:
            return

        token_id, book, msg_ts = self._extract_orderbook(data)
        if not token_id or not book:
            return

        now = time.time()
        latency_ms = None
        if msg_ts:
            latency_ms = max(0.0, (now - msg_ts) * 1000.0)

        self._orderbook_cache[str(token_id)] = {
            "book": book,
            "ts": now,
            "latency_ms": latency_ms,
            "source": "ws",
        }

    def _extract_orderbook(self, payload: dict) -> Tuple[Optional[str], Optional[dict], Optional[float]]:
        candidates = [payload]

        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            candidates.append(data)
        elif isinstance(data, list):
            candidates.extend([d for d in data if isinstance(d, dict)])

        for c in candidates:
            token_id = (
                c.get("asset_id")
                or c.get("token_id")
                or c.get("assetId")
                or c.get("tokenId")
            )
            bids = c.get("bids")
            asks = c.get("asks")
            if token_id and isinstance(bids, list) and isinstance(asks, list):
                ts = self._extract_ts_seconds(c)
                return str(token_id), {"bids": bids, "asks": asks}, ts

        return None, None, None

    @staticmethod
    def _extract_ts_seconds(payload: dict) -> Optional[float]:
        for key in ("timestamp", "ts", "time"):
            v = payload.get(key)
            if v is None:
                continue
            try:
                f = float(v)
            except Exception:
                continue
            # Heuristic: ms epoch.
            if f > 2_000_000_000_000:
                return f / 1000.0
            # sec epoch.
            if f > 2_000_000_000:
                return f
        return None

    # ------------------------------------------------------------------
    # Parse helper
    # ------------------------------------------------------------------

    def _parse(self, raw: Dict) -> Dict:
        prices = raw.get("outcomePrices", [])
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except (json.JSONDecodeError, ValueError):
                prices = []

        tokens = raw.get("clobTokenIds", [])
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except (json.JSONDecodeError, ValueError):
                tokens = []

        yes_p = float(prices[0]) if len(prices) > 0 else 0.0
        no_p = float(prices[1]) if len(prices) > 1 else 0.0

        return {
            "id": raw.get("id", ""),
            "question": raw.get("question", ""),
            "slug": raw.get("slug", ""),
            "yes_price": yes_p,
            "no_price": no_p,
            "volume": float(raw.get("volume", 0)),
            "liquidity": float(raw.get("liquidity", 0)),
            "end_date": raw.get("endDate", ""),
            "token_ids": tokens,
            "resolution_source": raw.get("resolutionSource", ""),
        }
