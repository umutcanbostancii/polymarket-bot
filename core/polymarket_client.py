"""Polymarket client for BTC 5-min Up or Down markets."""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional

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
        self._last_rest_request_ts = 0.0

        # Cache to avoid redundant discovery calls within same 5-min window
        self._cached_market: Optional[Dict] = None
        self._cached_market_end_ts: float = 0.0
        self._cached_market_fetch_ts: float = 0.0  # Track when cache was last updated

        # Separate cache for 15-min markets
        self._cached_market_15m: Optional[Dict] = None
        self._cached_market_15m_end_ts: float = 0.0
        self._cached_market_15m_fetch_ts: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=float(self.cfg.POLY_REST_TIMEOUT_SECONDS))
        self.session = aiohttp.ClientSession(timeout=timeout)
        self._last_success = time.time()
        self._running = True
        has_creds = "with API credentials" if self.cfg.POLY_API_KEY else "without credentials"
        log.info(f"PolymarketClient ready ({has_creds})")

    async def stop(self):
        self._running = False
        if self.session:
            await self.session.close()

    def _ensure_session(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=float(self.cfg.POLY_REST_TIMEOUT_SECONDS))
            self.session = aiohttp.ClientSession(timeout=timeout)

    @property
    def healthy(self) -> bool:
        return (time.time() - self._last_success < 60) or (self._consecutive_errors < 5)

    # ------------------------------------------------------------------
    # REST resilience
    # ------------------------------------------------------------------

    def _circuit_blocked(self) -> bool:
        return time.time() < self._circuit_open_until

    def _mark_success(self):
        self._last_success = time.time()
        self._consecutive_errors = 0

    def _mark_error(self):
        self._consecutive_errors += 1
        if self._consecutive_errors >= self.cfg.POLY_CIRCUIT_BREAKER_ERRORS:
            cooldown = float(self.cfg.POLY_CIRCUIT_BREAKER_COOLDOWN_SECONDS)
            self._circuit_open_until = time.time() + cooldown

    async def _throttle(self):
        min_interval = 1.0 / max(self.cfg.POLY_MAX_REQUESTS_PER_SEC, 1.0)
        now = time.time()
        wait = self._last_rest_request_ts + min_interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_rest_request_ts = time.time()

    async def _request_json(self, path: str, params: dict = None):
        """GET request to Gamma API. Returns parsed JSON or None."""
        self._ensure_session()
        if not path.startswith("/"):
            path = "/" + path

        base = self.cfg.GAMMA_API.rstrip("/")
        max_attempts = self.cfg.POLY_RETRY_MAX_ATTEMPTS

        for attempt in range(max_attempts):
            if self._circuit_blocked():
                await asyncio.sleep(max(0.1, self._circuit_open_until - time.time()))

            await self._throttle()
            url = base + path
            try:
                async with self.session.get(url, params=params) as r:
                    if r.status == 200:
                        data = await r.json()
                        self._mark_success()
                        return data
                    self._mark_error()
                    if r.status not in (429, 500, 502, 503, 504):
                        text = await r.text()
                        log.warning(f"Polymarket HTTP {r.status} {url}: {text[:200]}")
            except Exception as exc:
                self._mark_error()
                log.warning(f"Polymarket request failed {url}: {exc}")

            if attempt < max_attempts - 1:
                await asyncio.sleep(0.15 * (2 ** attempt))

        return None

    async def _clob_request_json(self, path: str, params: dict = None) -> Optional[dict]:
        """GET request to CLOB API (for orderbook)."""
        self._ensure_session()
        if not path.startswith("/"):
            path = "/" + path

        base = self.cfg.CLOB_API.rstrip("/")
        await self._throttle()
        url = base + path
        try:
            async with self.session.get(url, params=params) as r:
                if r.status == 200:
                    data = await r.json()
                    self._mark_success()
                    return data
                self._mark_error()
        except Exception as exc:
            self._mark_error()
            log.warning(f"CLOB request failed {url}: {exc}")
        return None

    # ------------------------------------------------------------------
    # BTC 5-min market discovery
    # ------------------------------------------------------------------

    async def find_active_btc_5min_market(self) -> Optional[Dict]:
        """
        Find the currently active "Bitcoin Up or Down" 5-minute market.

        Discovery strategy (in order):
        1. Return cached market if still valid AND recently updated (3s TTL)
        2. If market exists but cache is stale, refresh prices only
        3. Try direct slug lookup for new market
        4. Fallback: search recent events
        """
        now = time.time()
        CACHE_TTL = 3.0  # Refresh prices every 3 seconds

        # 1. Check cache with TTL - if we have recent data, refresh prices
        if self._cached_market and self._cached_market_end_ts > now + 10:
            # Market is still active, check if cache is stale
            if now - self._cached_market_fetch_ts > CACHE_TTL:
                # Cache stale - refresh prices only
                market_id = self._cached_market.get("id")
                if market_id:
                    fresh = await self.get_market_by_id(market_id)
                    if fresh:
                        # Keep market ID and end time, update prices
                        fresh["id"] = market_id
                        fresh["end_date_ts"] = self._cached_market_end_ts
                        self._cached_market = fresh
                        self._cached_market_fetch_ts = now
                        log.debug(f"Cache refreshed: UP={fresh.get('up_price')} DOWN={fresh.get('down_price')}")
                        return fresh
            return self._cached_market

        # Cache miss - do full discovery
        self._cached_market = None
        self._cached_market_end_ts = 0.0

        # 2. Try direct slug lookup (fast path - single API call)
        market = await self._try_slug_discovery(now)
        if market:
            self._cached_market = market
            self._cached_market_end_ts = market.get("end_date_ts", 0)
            self._cached_market_fetch_ts = now
            return market

        # 3. Fallback: search recent events
        market = await self._try_events_discovery(now)
        if market:
            self._cached_market = market
            self._cached_market_end_ts = market.get("end_date_ts", 0)
            self._cached_market_fetch_ts = now
            return market

        return None

    async def _try_slug_discovery(self, now: float, window_seconds: int = 300, label: str = "5m") -> Optional[Dict]:
        """
        Try to find a market by constructing its slug directly.
        Slug pattern: btc-updown-{label}-{unix_start_time}
        where unix_start_time is rounded down to nearest window_seconds.
        """
        current_window_start = int(now) - (int(now) % window_seconds)

        # Try current window and next window (market might be created early)
        for offset in [0, window_seconds, -window_seconds]:
            window_start = current_window_start + offset
            window_end = window_start + window_seconds
            remaining = window_end - now

            # Skip windows that have already ended or are too far away
            if remaining < 10 or remaining > window_seconds * 2:
                continue

            slug = f"btc-updown-{label}-{window_start}"
            data = await self._request_json(f"/events/slug/{slug}")

            if not data:
                continue

            # Event found - extract the market from it
            markets = data.get("markets", [])
            if not markets:
                continue

            raw_market = markets[0]
            parsed = self._parse_market(raw_market)
            if parsed and parsed.get("end_date_ts"):
                end_ts = parsed["end_date_ts"]
                if end_ts > now + 10:
                    log.info(f"Found {label} market via slug: {parsed['question']} "
                             f"(ends in {int(end_ts - now)}s)")
                    return parsed

        return None

    async def _try_events_discovery(self, now: float, slug_prefix: str = "btc-updown-5m-") -> Optional[Dict]:
        """
        Fallback: search recent events endpoint for BTC markets.
        This handles cases where the slug pattern might change or
        markets aren't aligned to exact boundaries.
        """
        data = await self._request_json(
            "/events",
            params={
                "closed": "false",
                "order": "startDate",
                "ascending": "false",
                "limit": 20,
            },
        )
        if not data:
            return None

        best = None
        best_end = None

        for event in data:
            slug = event.get("slug", "")
            if not slug.startswith(slug_prefix):
                continue

            markets = event.get("markets", [])
            if not markets:
                continue

            raw_market = markets[0]
            parsed = self._parse_market(raw_market)
            if not parsed:
                continue

            end_ts = parsed.get("end_date_ts")
            if not end_ts:
                continue

            remaining = end_ts - now
            if remaining < 10:
                continue

            # Prefer the soonest-ending active market
            if best is None or end_ts < best_end:
                best = parsed
                best_end = end_ts

        if best:
            log.info(f"Found market via events search: {best['question']} "
                     f"(ends in {int(best_end - now)}s)")
        return best

    # ------------------------------------------------------------------
    # BTC 15-min market discovery
    # ------------------------------------------------------------------

    async def find_active_btc_15min_market(self) -> Optional[Dict]:
        """
        Find the currently active "Bitcoin Up or Down" 15-minute market.
        Same logic as 5-min but with 900s window and separate cache.
        """
        now = time.time()
        CACHE_TTL = 3.0

        # 1. Check cache with TTL
        if self._cached_market_15m and self._cached_market_15m_end_ts > now + 10:
            if now - self._cached_market_15m_fetch_ts > CACHE_TTL:
                market_id = self._cached_market_15m.get("id")
                if market_id:
                    fresh = await self.get_market_by_id(market_id)
                    if fresh:
                        fresh["id"] = market_id
                        fresh["end_date_ts"] = self._cached_market_15m_end_ts
                        self._cached_market_15m = fresh
                        self._cached_market_15m_fetch_ts = now
                        return fresh
            return self._cached_market_15m

        # Cache miss - full discovery
        self._cached_market_15m = None
        self._cached_market_15m_end_ts = 0.0

        # 2. Try direct slug lookup (15m pattern)
        market = await self._try_slug_discovery(now, window_seconds=900, label="15m")
        if market:
            self._cached_market_15m = market
            self._cached_market_15m_end_ts = market.get("end_date_ts", 0)
            self._cached_market_15m_fetch_ts = now
            return market

        # 3. Fallback: search events
        market = await self._try_events_discovery(now, slug_prefix="btc-updown-15m-")
        if market:
            self._cached_market_15m = market
            self._cached_market_15m_end_ts = market.get("end_date_ts", 0)
            self._cached_market_15m_fetch_ts = now
            return market

        return None

    # ------------------------------------------------------------------
    # Market Data
    # ------------------------------------------------------------------
    
    async def get_market_by_id(self, market_id: str) -> Optional[Dict]:
        """
        Fetch fresh market data by ID from Polymarket API.
        Returns updated prices using _parse_market for correct Up/Down mapping.
        """
        try:
            data = await self._request_json(f"/markets/{market_id}")
            if data:
                # Use _parse_market for correct price extraction
                return self._parse_market(data)
        except Exception as e:
            log.warning(f"Failed to fetch market {market_id}: {e}")
        return None

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------

    async def get_orderbook(self, token_id: str) -> Optional[Dict]:
        """Fetch orderbook for a token from CLOB API."""
        return await self._clob_request_json(
            "/book",
            params={"token_id": token_id},
        )

    def get_implied_probability(self, orderbook: Dict, side: str = "buy") -> Optional[float]:
        """
        Extract implied probability from orderbook.
        For buying: best ask = cheapest price to buy YES token.
        For selling: best bid = highest price someone will pay for YES token.
        """
        if not orderbook:
            return None

        if side == "buy":
            asks = orderbook.get("asks", [])
            if asks:
                best = min(float(a.get("price", 1.0)) for a in asks)
                return best
        else:
            bids = orderbook.get("bids", [])
            if bids:
                best = max(float(b.get("price", 0.0)) for b in bids)
                return best
        return None

    def get_orderbook_metrics(self, orderbook: dict) -> Optional[dict]:
        """Order book'tan execution metrikler. PDF s.21: OBI, liquidity depth, spread."""
        if not orderbook:
            return None

        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        if not bids or not asks:
            return None

        best_bid = max(float(b.get("price", 0)) for b in bids)
        best_ask = min(float(a.get("price", 1)) for a in asks)
        midpoint = (best_bid + best_ask) / 2.0
        spread = best_ask - best_bid

        # Top 5 depth
        sorted_bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)[:5]
        sorted_asks = sorted(asks, key=lambda x: float(x.get("price", 0)))[:5]

        bid_depth = sum(float(b.get("price", 0)) * float(b.get("size", 0)) for b in sorted_bids)
        ask_depth = sum(float(a.get("price", 0)) * float(a.get("size", 0)) for a in sorted_asks)

        total_depth = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0.0

        return {
            "midpoint": midpoint,
            "spread": spread,
            "spread_pct": spread / midpoint if midpoint > 0 else 0,
            "bid_depth_usd": bid_depth,
            "ask_depth_usd": ask_depth,
            "imbalance": imbalance,
        }

    def simulate_fill(self, orderbook: dict, side: str, size_usd: float) -> dict:
        """PDF s.32: VWAP fill simulation.
        Order book levels'i iterate ederek gercek fill fiyatini hesapla."""
        # side: "buy" -> consume asks, "sell" -> consume bids
        if side == "buy":
            levels = sorted(
                orderbook.get("asks", []),
                key=lambda x: float(x.get("price", 0)),
            )
        else:
            levels = sorted(
                orderbook.get("bids", []),
                key=lambda x: float(x.get("price", 0)),
                reverse=True,
            )

        remaining = size_usd
        total_cost = 0.0
        total_shares = 0.0
        levels_consumed = 0

        for level in levels:
            price = float(level.get("price", 0))
            size = float(level.get("size", 0))
            if price <= 0 or size <= 0:
                continue

            level_usd = price * size
            if level_usd >= remaining:
                shares_needed = remaining / price
                total_cost += shares_needed * price
                total_shares += shares_needed
                remaining = 0
                levels_consumed += 1
                break
            else:
                total_cost += level_usd
                total_shares += size
                remaining -= level_usd
                levels_consumed += 1

        filled_usd = size_usd - remaining
        vwap = total_cost / total_shares if total_shares > 0 else 0.0

        # Midpoint for slippage calc
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        best_bid = max((float(b.get("price", 0)) for b in bids), default=0)
        best_ask = min((float(a.get("price", 1)) for a in asks), default=1)
        midpoint = (best_bid + best_ask) / 2.0

        return {
            "vwap_price": vwap,
            "slippage_vs_mid": vwap - midpoint if side == "buy" else midpoint - vwap,
            "total_filled_usd": filled_usd,
            "levels_consumed": levels_consumed,
            "partial_fill": remaining > 0,
        }

    # ------------------------------------------------------------------
    # Parse helper
    # ------------------------------------------------------------------

    def _parse_market(self, raw: Dict) -> Optional[Dict]:
        """Parse a raw market dict from Gamma API into our standard format."""
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

        outcomes = raw.get("outcomes", [])
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except (json.JSONDecodeError, ValueError):
                outcomes = []

        if len(tokens) < 2 or len(outcomes) < 2:
            return None

        yes_p = float(prices[0]) if len(prices) > 0 else 0.0
        no_p = float(prices[1]) if len(prices) > 1 else 0.0

        # Parse end date and event start time
        end_date_str = raw.get("endDate", "")
        end_date_ts = self._parse_iso_ts(end_date_str)

        start_time_str = raw.get("eventStartTime", "")
        start_date_ts = self._parse_iso_ts(start_time_str)

        # Identify "Up" and "Down" tokens from outcomes
        up_token = None
        down_token = None
        up_price = 0.0
        down_price = 0.0

        for i, outcome in enumerate(outcomes):
            outcome_lower = str(outcome).lower()
            if "up" in outcome_lower and i < len(tokens):
                up_token = tokens[i]
                up_price = float(prices[i]) if i < len(prices) else 0.0
            elif "down" in outcome_lower and i < len(tokens):
                down_token = tokens[i]
                down_price = float(prices[i]) if i < len(prices) else 0.0

        # Fallback: first = Up, second = Down (Polymarket convention)
        if not up_token:
            up_token = tokens[0]
            up_price = yes_p
            down_token = tokens[1]
            down_price = no_p

        return {
            "id": raw.get("id", ""),
            "condition_id": raw.get("conditionId", ""),
            "question": raw.get("question", ""),
            "slug": raw.get("slug", ""),
            "outcomes": outcomes,
            "up_price": up_price,
            "down_price": down_price,
            "up_token": up_token,
            "down_token": down_token,
            "yes_price": yes_p,
            "no_price": no_p,
            "volume": float(raw.get("volume", 0)),
            "liquidity": float(raw.get("liquidity", 0)),
            "end_date": end_date_str,
            "end_date_ts": end_date_ts,
            "start_date": start_time_str,
            "start_date_ts": start_date_ts,
            "token_ids": tokens,
            "fees_enabled": raw.get("feesEnabled", True),
            "enable_order_book": raw.get("enableOrderBook", True),
        }

    @staticmethod
    def _parse_iso_ts(s: str) -> Optional[float]:
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Dynamic fee calculation
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_dynamic_fee(price: float) -> float:
        """
        Polymarket dynamic fee: max ~3.15% at 50c, scales down towards 0/100c.
        fee = price * (1 - price) * 0.0222 * 2
        """
        if price <= 0 or price >= 1:
            return 0.0
        return price * (1 - price) * 0.0444
