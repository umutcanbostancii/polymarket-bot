"""Real-time web dashboard for Polymarket BTC trading bot."""

import asyncio
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import aiohttp
from aiohttp import web

from config import config as bot_config
from core.database import TradeDB

log = logging.getLogger("dashboard")

BASE_DIR = Path(__file__).parent
PROJECT_DIR = BASE_DIR.parent
load_dotenv(PROJECT_DIR / ".env")

DB_PATH = str(PROJECT_DIR / "trades.db")
STATUS_FILE = os.getenv("STATUS_FILE", "/tmp/polymarket_bot_status.json")
DECISIONS_FILE = os.getenv("DECISIONS_FILE", "/tmp/polymarket_bot_decisions.json")
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "1000"))

BINANCE_REST = "https://api.binance.com"
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
TRADE_SIZE_FILE = os.getenv("TRADE_SIZE_FILE", "/tmp/polymarket_bot_trade_size")
MARKET_15M_FILE = os.getenv("MARKET_15M_FILE", "/tmp/polymarket_bot_15m_enabled")
TEST_MODE_FILE = os.getenv("TEST_MODE_FILE", "/tmp/polymarket_bot_test_enabled")
ARB_PID_LOCK_PATH = os.getenv("ARB_PID_LOCK_PATH", "/tmp/poly_arb_bot.pid")
ARB_STATUS_FILE = os.getenv("ARB_STATUS_FILE", "/tmp/poly_arb_bot_status.json")
ARB_RUNTIME_CONFIG_FILE = os.getenv("ARB_RUNTIME_CONFIG_FILE", "/tmp/poly_arb_bot_runtime.json")
ARB_LOG_FILE = os.getenv("ARB_LOG_FILE", "/tmp/poly_arb_bot.log")
ARB_BOT_PATH = str(PROJECT_DIR / "arb_bot.py")


class Dashboard:
    def __init__(self, host="0.0.0.0", port=8080):
        self.host = host
        self.port = port
        self.app = web.Application()
        self.clients: list[web.WebSocketResponse] = []
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._last_trade_id = 0
        self._trade_pnl_cache: dict[int, float] = {}  # track PnL changes
        self._last_decisions_ts: float = 0.0  # track decisions file changes
        self._current_market = None
        self._current_market_question = ""
        self._current_market_15m = None
        self._current_market_15m_question = ""
        self._clob_client = None
        self._polymarket_balance = -1.0  # -1 = not yet fetched

        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/ws", self.websocket)
        self.app.router.add_get("/api/trades", self.api_trades)
        self.app.router.add_get("/api/stats", self.api_stats)
        self.app.router.add_get("/api/klines", self.api_klines)
        self.app.router.add_get("/api/status", self.api_status)
        self.app.router.add_get("/api/analytics", self.api_analytics)
        self.app.router.add_get("/api/trade-size", self.api_get_trade_size)
        self.app.router.add_post("/api/trade-size", self.api_set_trade_size)
        self.app.router.add_get("/api/market-15m", self.api_get_15m_toggle)
        self.app.router.add_post("/api/toggle-15m", self.api_toggle_15m)
        self.app.router.add_get("/api/test-mode", self.api_get_test_mode)
        self.app.router.add_post("/api/toggle-test", self.api_toggle_test)
        self.app.router.add_get("/api/analytics-test", self.api_analytics_test)
        self.app.router.add_get("/api/stats-test", self.api_stats_test)
        self.app.router.add_get("/api/trades-test", self.api_trades_test)
        self.app.router.add_post("/api/manual-trade", self.api_manual_trade)
        self.app.router.add_get("/api/live-stats", self.api_live_stats)
        self.app.router.add_get("/api/decisions", self.api_decisions)
        self.app.router.add_get("/api/arb/status", self.api_arb_status)
        self.app.router.add_post("/api/arb/start", self.api_arb_start)
        self.app.router.add_post("/api/arb/stop", self.api_arb_stop)
        self.app.router.add_get("/api/arb/config", self.api_arb_get_config)
        self.app.router.add_post("/api/arb/config", self.api_arb_set_config)
        self.app.router.add_get("/api/arb/summary", self.api_arb_summary)
        self.app.router.add_get("/api/arb/cycles", self.api_arb_cycles)
        self.app.router.add_get("/api/arb/events", self.api_arb_events)
        self.app.router.add_get("/api/arb/telemetry", self.api_arb_telemetry)
        self.app.router.add_static("/static", BASE_DIR / "static")

        self.app.on_startup.append(self._on_startup)
        self.app.on_shutdown.append(self._on_shutdown)

    async def index(self, req):
        return web.FileResponse(BASE_DIR / "index.html")

    # ── lifecycle ──────────────────────────────────────────────

    async def _on_startup(self, app):
        self._session = aiohttp.ClientSession()
        self._running = True
        trades = self._get_trades(limit=1)
        if trades:
            self._last_trade_id = trades[0].get("id", 0)
        self._tasks = [
            asyncio.create_task(self._binance_ws()),
            asyncio.create_task(self._market_loop()),
            asyncio.create_task(self._trade_loop()),
            asyncio.create_task(self._status_loop()),
            asyncio.create_task(self._live_stats_loop()),
            asyncio.create_task(self._decisions_loop()),
        ]

    async def _on_shutdown(self, app):
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for ws in list(self.clients):
            await ws.close()
        if self._session:
            await self._session.close()

    # ── broadcast ──────────────────────────────────────────────

    async def broadcast(self, data: dict):
        msg = json.dumps(data, default=str)
        dead = []
        for ws in self.clients:
            try:
                await ws.send_str(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.clients:
                self.clients.remove(ws)

    async def websocket(self, req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.clients.append(ws)
        log.info(f"WS client connected ({len(self.clients)} total)")
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if data.get("type") == "set_trade_size":
                            size = float(data.get("size", 0))
                            if size > 0:
                                self._write_trade_size(size)
                                await self.broadcast({
                                    "type": "trade_size",
                                    "size": size,
                                })
                        elif data.get("type") == "toggle_15m":
                            enabled = bool(data.get("enabled", False))
                            self._set_15m_enabled(enabled)
                            await self.broadcast({
                                "type": "market_15m_toggle",
                                "enabled": enabled,
                            })
                        elif data.get("type") == "toggle_test":
                            enabled = bool(data.get("enabled", False))
                            self._set_test_enabled(enabled)
                            await self.broadcast({
                                "type": "test_mode_toggle",
                                "enabled": enabled,
                            })
                    except Exception:
                        pass
                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    break
        finally:
            if ws in self.clients:
                self.clients.remove(ws)
            log.info(f"WS client disconnected ({len(self.clients)} total)")
        return ws

    # ── background: Binance WS ─────────────────────────────────

    async def _binance_ws(self):
        while self._running:
            try:
                url = f"{BINANCE_WS_URL}/btcusdt@kline_1m"
                async with self._session.ws_connect(url) as ws:
                    log.info("Binance WS connected")
                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        data = json.loads(msg.data)
                        k = data.get("k", {})
                        await self.broadcast({
                            "type": "price",
                            "price": float(k.get("c", 0)),
                            "time": int(k.get("t", 0)) // 1000,
                            "open": float(k.get("o", 0)),
                            "high": float(k.get("h", 0)),
                            "low": float(k.get("l", 0)),
                            "close": float(k.get("c", 0)),
                            "volume": float(k.get("v", 0)),
                            "closed": k.get("x", False),
                        })
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error(f"Binance WS error: {e}")
                await asyncio.sleep(3)

    # ── background: Polymarket market + orderbook ──────────────

    async def _market_loop(self):
        while self._running:
            try:
                await self._fetch_market()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error(f"Market loop error: {e}")
            try:
                await self._fetch_market_15m()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error(f"Market 15m loop error: {e}")
            await asyncio.sleep(3)

    async def _fetch_market(self):
        now = time.time()
        start_ts = int(now // 300) * 300

        for offset in [0, 300, -300, 600]:
            ts = start_ts + offset
            slug = f"btc-updown-5m-{ts}"
            url = f"{GAMMA_API}/events/slug/{slug}"
            try:
                async with self._session.get(
                    url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status != 200:
                        continue
                    event = await resp.json()
            except Exception:
                continue

            markets = event.get("markets", [])
            if not markets:
                continue
            m = markets[0]

            end_ts = ts + 300
            end_str = m.get("endDate", "")
            if end_str:
                try:
                    end_str = end_str.replace("Z", "+00:00")
                    end_ts = datetime.fromisoformat(end_str).timestamp()
                except Exception:
                    pass

            remaining = max(0, end_ts - now)
            if remaining <= 0:
                continue

            # Event slug'dan gelen outcomePrices stale olabiliyor.
            # Fresh fiyatlar icin /markets/{id} endpoint'ini kullan.
            market_id = m.get("id") or m.get("conditionId") or ""
            up_price = 0.5
            down_price = 0.5

            # 1) Fresh prices from /markets/{id}
            if market_id:
                try:
                    mkt_url = f"{GAMMA_API}/markets/{market_id}"
                    async with self._session.get(
                        mkt_url, timeout=aiohttp.ClientTimeout(total=3)
                    ) as mkt_resp:
                        if mkt_resp.status == 200:
                            fresh = await mkt_resp.json()
                            fop = fresh.get("outcomePrices")
                            if fop:
                                fp = json.loads(fop) if isinstance(fop, str) else fop
                                up_price = float(fp[0])
                                down_price = float(fp[1])
                except Exception:
                    pass

            # 2) Fallback: event'teki outcomePrices
            if up_price == 0.5 and down_price == 0.5:
                op = m.get("outcomePrices")
                if op:
                    try:
                        prices = json.loads(op) if isinstance(op, str) else op
                        up_price = float(prices[0])
                        down_price = float(prices[1])
                    except Exception:
                        pass

            token_ids = []
            raw_tokens = m.get("clobTokenIds")
            if raw_tokens:
                try:
                    token_ids = json.loads(raw_tokens) if isinstance(raw_tokens, str) else list(raw_tokens)
                except Exception:
                    pass

            market_data = {
                "type": "market",
                "question": m.get("question", ""),
                "up_price": up_price,
                "down_price": down_price,
                "volume": float(m.get("volume", 0) or 0),
                "liquidity": float(m.get("liquidity", 0) or 0),
                "remaining": int(remaining),
                "end_ts": end_ts,
                "start_ts": ts,
                "token_ids": token_ids,
            }

            if token_ids:
                try:
                    ob_url = f"{CLOB_API}/book?token_id={token_ids[0]}"
                    async with self._session.get(
                        ob_url, timeout=aiohttp.ClientTimeout(total=3)
                    ) as ob_resp:
                        if ob_resp.status == 200:
                            market_data["orderbook"] = await ob_resp.json()
                except Exception:
                    pass

            # CLOB orderbook'tan canli fiyat hesapla (Gamma API stale olabiliyor)
            if market_data.get("orderbook"):
                ob = market_data["orderbook"]
                bids = ob.get("bids", [])
                asks = ob.get("asks", [])
                if bids and asks:
                    best_bid = max(float(b["price"]) for b in bids)
                    best_ask = min(float(a["price"]) for a in asks)
                    live_up = (best_bid + best_ask) / 2.0
                    market_data["up_price"] = round(live_up, 4)
                    market_data["down_price"] = round(1.0 - live_up, 4)

            # Detect market change
            new_q = market_data.get("question", "")
            if new_q != self._current_market_question:
                self._current_market_question = new_q
                log.info(f"Market changed: {new_q}")

            self._current_market = market_data
            await self.broadcast(market_data)
            return

        # No active 5m market found — tell frontend to reset
        if self._current_market is not None:
            self._current_market = None
            self._current_market_question = ""
            await self.broadcast({"type": "no_market"})

    async def _fetch_market_15m(self):
        """Fetch active 15-min BTC market."""
        now = time.time()
        start_ts = int(now // 900) * 900

        for offset in [0, 900, -900]:
            ts = start_ts + offset
            slug = f"btc-updown-15m-{ts}"
            url = f"{GAMMA_API}/events/slug/{slug}"
            try:
                async with self._session.get(
                    url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status != 200:
                        continue
                    event = await resp.json()
            except Exception:
                continue

            markets = event.get("markets", [])
            if not markets:
                continue
            m = markets[0]

            end_ts = ts + 900
            end_str = m.get("endDate", "")
            if end_str:
                try:
                    end_str = end_str.replace("Z", "+00:00")
                    end_ts = datetime.fromisoformat(end_str).timestamp()
                except Exception:
                    pass

            remaining = max(0, end_ts - now)
            if remaining <= 0:
                continue

            market_id = m.get("id") or m.get("conditionId") or ""
            up_price = 0.5
            down_price = 0.5

            if market_id:
                try:
                    mkt_url = f"{GAMMA_API}/markets/{market_id}"
                    async with self._session.get(
                        mkt_url, timeout=aiohttp.ClientTimeout(total=3)
                    ) as mkt_resp:
                        if mkt_resp.status == 200:
                            fresh = await mkt_resp.json()
                            fop = fresh.get("outcomePrices")
                            if fop:
                                fp = json.loads(fop) if isinstance(fop, str) else fop
                                up_price = float(fp[0])
                                down_price = float(fp[1])
                except Exception:
                    pass

            if up_price == 0.5 and down_price == 0.5:
                op = m.get("outcomePrices")
                if op:
                    try:
                        prices = json.loads(op) if isinstance(op, str) else op
                        up_price = float(prices[0])
                        down_price = float(prices[1])
                    except Exception:
                        pass

            token_ids = []
            raw_tokens = m.get("clobTokenIds")
            if raw_tokens:
                try:
                    token_ids = json.loads(raw_tokens) if isinstance(raw_tokens, str) else list(raw_tokens)
                except Exception:
                    pass

            market_data = {
                "type": "market_15m",
                "question": m.get("question", ""),
                "up_price": up_price,
                "down_price": down_price,
                "volume": float(m.get("volume", 0) or 0),
                "liquidity": float(m.get("liquidity", 0) or 0),
                "remaining": int(remaining),
                "end_ts": end_ts,
                "start_ts": ts,
                "token_ids": token_ids,
            }

            if token_ids:
                try:
                    ob_url = f"{CLOB_API}/book?token_id={token_ids[0]}"
                    async with self._session.get(
                        ob_url, timeout=aiohttp.ClientTimeout(total=3)
                    ) as ob_resp:
                        if ob_resp.status == 200:
                            market_data["orderbook"] = await ob_resp.json()
                except Exception:
                    pass

            # CLOB live price
            if market_data.get("orderbook"):
                ob = market_data["orderbook"]
                bids = ob.get("bids", [])
                asks = ob.get("asks", [])
                if bids and asks:
                    best_bid = max(float(b["price"]) for b in bids)
                    best_ask = min(float(a["price"]) for a in asks)
                    live_up = (best_bid + best_ask) / 2.0
                    market_data["up_price"] = round(live_up, 4)
                    market_data["down_price"] = round(1.0 - live_up, 4)

            new_q = market_data.get("question", "")
            if new_q != self._current_market_15m_question:
                self._current_market_15m_question = new_q
                log.info(f"15m market changed: {new_q}")

            self._current_market_15m = market_data
            await self.broadcast(market_data)
            return

        if self._current_market_15m is not None:
            self._current_market_15m = None
            self._current_market_15m_question = ""
            await self.broadcast({"type": "no_market_15m"})

    # ── background: new trade detector ─────────────────────────

    async def _trade_loop(self):
        while self._running:
            try:
                trades = self._get_trades(limit=10)
                for t in reversed(trades):
                    tid = t.get("id", 0)
                    pnl = t.get("pnl", 0) or 0

                    if tid > self._last_trade_id:
                        # Brand new trade
                        self._last_trade_id = tid
                        self._trade_pnl_cache[tid] = pnl
                        await self.broadcast({"type": "new_trade", "trade": t})
                    elif tid in self._trade_pnl_cache and self._trade_pnl_cache[tid] != pnl:
                        # PnL changed (trade resolved)
                        self._trade_pnl_cache[tid] = pnl
                        await self.broadcast({"type": "trade_update", "trade": t})
                        log.info(f"Trade #{tid} resolved: pnl=${pnl:+.2f}")

                # Keep cache small
                if len(self._trade_pnl_cache) > 50:
                    oldest = sorted(self._trade_pnl_cache.keys())[:-20]
                    for k in oldest:
                        del self._trade_pnl_cache[k]
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error(f"Trade loop error: {e}")
            await asyncio.sleep(2)

    # ── background: bot status ─────────────────────────────────

    async def _status_loop(self):
        while self._running:
            try:
                status = self._read_status()
                if status:
                    status["type"] = "status"
                    await self.broadcast(status)
            except asyncio.CancelledError:
                return
            except Exception:
                pass
            await asyncio.sleep(5)

    # ── REST API ───────────────────────────────────────────────

    async def api_trades(self, req):
        limit = int(req.query.get("limit", 100))
        return web.json_response(self._get_trades(limit=limit))

    async def api_stats(self, req):
        return web.json_response(self._get_stats())

    async def api_klines(self, req):
        interval = req.query.get("interval", "5m")
        limit = int(req.query.get("limit", 300))
        url = (
            f"{BINANCE_REST}/api/v3/klines"
            f"?symbol=BTCUSDT&interval={interval}&limit={limit}"
        )
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                klines = [
                    {
                        "time": k[0] // 1000,
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                    }
                    for k in data
                ]
                return web.json_response(klines)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_status(self, req):
        return web.json_response(self._read_status() or {"running": False})

    async def api_analytics(self, req):
        return web.json_response(self._get_analytics())

    async def api_get_trade_size(self, req):
        size = self._read_trade_size()
        return web.json_response({"size": size})

    async def api_set_trade_size(self, req):
        try:
            body = await req.json()
            size = float(body.get("size", 0))
            if size <= 0:
                return web.json_response({"error": "size must be > 0"}, status=400)
            self._write_trade_size(size)
            await self.broadcast({"type": "trade_size", "size": size})
            log.info(f"Trade size set to ${size}")
            return web.json_response({"size": size})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def api_get_15m_toggle(self, req):
        enabled = os.path.exists(MARKET_15M_FILE)
        return web.json_response({"enabled": enabled})

    async def api_toggle_15m(self, req):
        try:
            body = await req.json()
            enabled = bool(body.get("enabled", False))
            self._set_15m_enabled(enabled)
            await self.broadcast({"type": "market_15m_toggle", "enabled": enabled})
            log.info(f"15m strategy {'enabled' if enabled else 'disabled'}")
            return web.json_response({"enabled": enabled})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def api_get_test_mode(self, req):
        enabled = os.path.exists(TEST_MODE_FILE)
        return web.json_response({"enabled": enabled})

    async def api_toggle_test(self, req):
        try:
            body = await req.json()
            enabled = bool(body.get("enabled", False))
            self._set_test_enabled(enabled)
            await self.broadcast({"type": "test_mode_toggle", "enabled": enabled})
            log.info(f"Test mode {'enabled' if enabled else 'disabled'}")
            return web.json_response({"enabled": enabled})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def api_analytics_test(self, req):
        return web.json_response(self._get_analytics_test())

    async def api_stats_test(self, req):
        return web.json_response(self._get_stats_test())

    async def api_trades_test(self, req):
        limit = int(req.query.get("limit", 100))
        return web.json_response(self._get_trades_test(limit=limit))

    def _set_test_enabled(self, enabled: bool):
        if enabled:
            try:
                with open(TEST_MODE_FILE, "w") as f:
                    f.write("1")
            except Exception as e:
                log.error(f"Failed to enable test mode: {e}")
        else:
            try:
                if os.path.exists(TEST_MODE_FILE):
                    os.remove(TEST_MODE_FILE)
            except Exception as e:
                log.error(f"Failed to disable test mode: {e}")

    def _set_15m_enabled(self, enabled: bool):
        if enabled:
            try:
                with open(MARKET_15M_FILE, "w") as f:
                    f.write("1")
            except Exception as e:
                log.error(f"Failed to enable 15m: {e}")
        else:
            try:
                if os.path.exists(MARKET_15M_FILE):
                    os.remove(MARKET_15M_FILE)
            except Exception as e:
                log.error(f"Failed to disable 15m: {e}")

    def _read_trade_size(self) -> float:
        try:
            with open(TRADE_SIZE_FILE, "r") as f:
                return float(f.read().strip())
        except Exception:
            return 1.0

    def _write_trade_size(self, size: float):
        try:
            with open(TRADE_SIZE_FILE, "w") as f:
                f.write(str(size))
        except Exception as e:
            log.error(f"Failed to write trade size: {e}")

    # ── DB helpers ─────────────────────────────────────────────

    def _get_trades(self, limit=100):
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            )
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            log.error(f"DB read error: {e}")
            return []

    def _get_stats(self):
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row

            row = dict(
                conn.execute(
                    """
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN pnl IS NULL OR pnl = 0 THEN 1 ELSE 0 END) as pending,
                    COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) as total_profit,
                    COALESCE(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END), 0) as total_loss,
                    COALESCE(SUM(pnl), 0) as net_pnl,
                    COALESCE(SUM(cost), 0) as total_cost
                FROM trades WHERE strategy IN ('btc_5min_fast', 'btc_15min')
            """
                ).fetchone()
            )

            today = dict(
                conn.execute(
                    """
                SELECT
                    COUNT(*) as today_trades,
                    COALESCE(SUM(pnl), 0) as today_pnl,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as today_wins,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as today_losses
                FROM trades
                WHERE strategy IN ('btc_5min_fast', 'btc_15min') AND date(ts) = date('now')
            """
                ).fetchone()
            )
            row.update(today)
            conn.close()

            resolved = (row.get("wins") or 0) + (row.get("losses") or 0)
            row["win_rate"] = (row["wins"] / resolved * 100) if resolved else 0
            row["starting_balance"] = STARTING_BALANCE
            row["current_balance"] = STARTING_BALANCE + (row.get("net_pnl") or 0)
            return row
        except Exception as e:
            log.error(f"Stats error: {e}")
            return {}

    def _parse_notes(self, notes: str) -> dict:
        """Parse trade notes like: delta=-0.001 mom=0.0 vol=0.0 remaining=72s btc=$68,500"""
        result = {}
        if not notes:
            return result
        for m in re.finditer(r"(\w+)=([^\s]+)", notes):
            key = m.group(1)
            val = m.group(2).rstrip("s").replace("$", "").replace(",", "")
            try:
                result[key] = float(val)
            except ValueError:
                result[key] = val
        return result

    def _compute_strategy_summary(self, resolved_trades: list) -> dict:
        """Compute win/loss/pnl summary for a subset of resolved trades."""
        wins = sum(1 for t in resolved_trades if t["pnl"] > 0)
        losses = sum(1 for t in resolved_trades if t["pnl"] < 0)
        total = wins + losses
        total_pnl = sum(t["pnl"] for t in resolved_trades)
        total_profit = sum(t["pnl"] for t in resolved_trades if t["pnl"] > 0)
        total_loss = sum(t["pnl"] for t in resolved_trades if t["pnl"] < 0)

        # Win/loss factors
        wf = defaultdict(int)
        lf = defaultdict(int)
        for t in resolved_trades:
            p = t["parsed"]
            delta = abs(p.get("delta", 0))
            remaining = p.get("remaining", 0)
            mom = abs(p.get("mom", 0))
            is_win = t["pnl"] > 0
            bucket = wf if is_win else lf

            if delta > 0.003:
                bucket["strong_delta"] += 1
            elif delta > 0.001:
                bucket["medium_delta"] += 1
            else:
                bucket["weak_delta"] += 1

            if remaining > 120:
                bucket["early_entry"] += 1
            elif remaining > 60:
                bucket["mid_entry"] += 1
            else:
                bucket["late_entry"] += 1

            if mom > 0.001:
                bucket["strong_momentum"] += 1
            else:
                bucket["weak_momentum"] += 1

            side = t.get("side", "")
            d = p.get("delta", 0)
            if (d > 0 and "UP" in side) or (d < 0 and "DOWN" in side):
                bucket["momentum_aligned"] += 1
            else:
                bucket["momentum_divergent"] += 1

        return {
            "wins": wins,
            "losses": losses,
            "total": total,
            "win_rate": round(wins / total * 100, 1) if total else 0,
            "pnl": round(total_pnl, 2),
            "profit": round(total_profit, 2),
            "loss": round(total_loss, 2),
            "win_factors": dict(wf),
            "loss_factors": dict(lf),
        }

    def _compute_full_analytics(self, trades: list) -> dict:
        """Core analytics computation shared by normal and test modes."""
        for t in trades:
            if "parsed" not in t:
                t["parsed"] = self._parse_notes(t.get("notes", ""))

        resolved = [t for t in trades if t.get("pnl") and t["pnl"] != 0]

        # ── 1. Hourly performance ──────────────────────────────
        hourly = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
        for t in resolved:
            ts = t.get("ts", "")
            hour = -1
            try:
                if isinstance(ts, str) and "T" in ts:
                    hour = int(ts.split("T")[1][:2])
                elif isinstance(ts, (int, float)):
                    hour = datetime.utcfromtimestamp(ts).hour
            except Exception:
                continue
            if hour < 0:
                continue
            bucket = hourly[hour]
            if t["pnl"] > 0:
                bucket["wins"] += 1
            else:
                bucket["losses"] += 1
            bucket["pnl"] += t["pnl"]

        hourly_list = []
        for h in range(24):
            b = hourly[h]
            total = b["wins"] + b["losses"]
            hourly_list.append({
                "hour": h,
                "wins": b["wins"],
                "losses": b["losses"],
                "total": total,
                "win_rate": round(b["wins"] / total * 100, 1) if total else 0,
                "pnl": round(b["pnl"], 2),
            })

        # ── 2. Delta buckets ───────────────────────────────────
        delta_buckets = [
            {"label": "< 0.0005", "min": 0, "max": 0.0005, "wins": 0, "losses": 0},
            {"label": "0.0005-0.001", "min": 0.0005, "max": 0.001, "wins": 0, "losses": 0},
            {"label": "0.001-0.003", "min": 0.001, "max": 0.003, "wins": 0, "losses": 0},
            {"label": "> 0.003", "min": 0.003, "max": 999, "wins": 0, "losses": 0},
        ]
        for t in resolved:
            delta = abs(t["parsed"].get("delta", 0))
            for b in delta_buckets:
                if b["min"] <= delta < b["max"]:
                    if t["pnl"] > 0:
                        b["wins"] += 1
                    else:
                        b["losses"] += 1
                    break
        for b in delta_buckets:
            total = b["wins"] + b["losses"]
            b["total"] = total
            b["win_rate"] = round(b["wins"] / total * 100, 1) if total else 0

        # ── 3. Daily P&L ──────────────────────────────────────
        daily = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
        for t in resolved:
            ts = t.get("ts", "")
            day = ""
            try:
                if isinstance(ts, str):
                    day = ts[:10]
                elif isinstance(ts, (int, float)):
                    day = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            except Exception:
                continue
            if not day:
                continue
            daily[day]["pnl"] += t["pnl"]
            daily[day]["trades"] += 1
            if t["pnl"] > 0:
                daily[day]["wins"] += 1
            else:
                daily[day]["losses"] += 1

        cumulative = 0
        daily_list = []
        peak = 0
        max_dd = 0
        for day in sorted(daily.keys()):
            d = daily[day]
            cumulative += d["pnl"]
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)
            daily_list.append({
                "date": day,
                "pnl": round(d["pnl"], 2),
                "cumulative": round(cumulative, 2),
                "trades": d["trades"],
                "wins": d["wins"],
                "losses": d["losses"],
                "drawdown": round(dd, 2),
            })

        # ── 4. Win/Loss reason analysis ────────────────────────
        win_factors = defaultdict(int)
        loss_factors = defaultdict(int)
        for t in resolved:
            p = t["parsed"]
            delta = abs(p.get("delta", 0))
            remaining = p.get("remaining", 0)
            mom = abs(p.get("mom", 0))
            is_win = t["pnl"] > 0
            bucket = win_factors if is_win else loss_factors

            if delta > 0.003:
                bucket["strong_delta"] += 1
            elif delta > 0.001:
                bucket["medium_delta"] += 1
            else:
                bucket["weak_delta"] += 1

            if remaining > 120:
                bucket["early_entry"] += 1
            elif remaining > 60:
                bucket["mid_entry"] += 1
            else:
                bucket["late_entry"] += 1

            if mom > 0.001:
                bucket["strong_momentum"] += 1
            else:
                bucket["weak_momentum"] += 1

            side = t.get("side", "")
            d = p.get("delta", 0)
            if (d > 0 and "UP" in side) or (d < 0 and "DOWN" in side):
                bucket["momentum_aligned"] += 1
            else:
                bucket["momentum_divergent"] += 1

        # ── 5. AI Insights ─────────────────────────────────────
        insights = []

        aligned_wins = win_factors.get("momentum_aligned", 0)
        aligned_losses = loss_factors.get("momentum_aligned", 0)
        aligned_total = aligned_wins + aligned_losses
        if aligned_total >= 3:
            wr = round(aligned_wins / aligned_total * 100)
            insights.append({
                "icon": "target",
                "text": f"Momentum uyumlu trade'lerde %{wr} başarı ({aligned_wins}/{aligned_total})",
                "type": "positive" if wr >= 60 else "negative" if wr < 40 else "neutral",
            })

        div_wins = win_factors.get("momentum_divergent", 0)
        div_losses = loss_factors.get("momentum_divergent", 0)
        div_total = div_wins + div_losses
        if div_total >= 3:
            wr = round(div_wins / div_total * 100)
            insights.append({
                "icon": "shuffle",
                "text": f"Momentum ters yön trade'lerde %{wr} başarı ({div_wins}/{div_total})",
                "type": "positive" if wr >= 60 else "negative" if wr < 40 else "neutral",
            })

        sd_wins = win_factors.get("strong_delta", 0)
        sd_losses = loss_factors.get("strong_delta", 0)
        sd_total = sd_wins + sd_losses
        if sd_total >= 2:
            wr = round(sd_wins / sd_total * 100)
            insights.append({
                "icon": "zap",
                "text": f"Güçlü delta (>0.003) trade'lerinde %{wr} başarı ({sd_wins}/{sd_total})",
                "type": "positive" if wr >= 60 else "negative" if wr < 40 else "neutral",
            })

        le_wins = win_factors.get("late_entry", 0)
        le_losses = loss_factors.get("late_entry", 0)
        le_total = le_wins + le_losses
        if le_total >= 2:
            wr = round(le_wins / le_total * 100)
            insights.append({
                "icon": "clock",
                "text": f"Son 60 saniye girişlerde %{wr} başarı ({le_wins}/{le_total})",
                "type": "positive" if wr >= 60 else "negative" if wr < 40 else "neutral",
            })

        ee_wins = win_factors.get("early_entry", 0)
        ee_losses = loss_factors.get("early_entry", 0)
        ee_total = ee_wins + ee_losses
        if ee_total >= 2:
            wr = round(ee_wins / ee_total * 100)
            insights.append({
                "icon": "sunrise",
                "text": f"Erken giriş (>120s kala) trade'lerde %{wr} başarı ({ee_wins}/{ee_total})",
                "type": "positive" if wr >= 60 else "negative" if wr < 40 else "neutral",
            })

        best_hour = max(hourly_list, key=lambda h: h["win_rate"] if h["total"] >= 2 else 0)
        if best_hour["total"] >= 2:
            insights.append({
                "icon": "star",
                "text": f"En başarılı saat: {best_hour['hour']:02d}:00 UTC — %{best_hour['win_rate']} kazanma ({best_hour['wins']}/{best_hour['total']})",
                "type": "positive",
            })

        # Streak analysis
        streak = 0
        max_win_streak = 0
        max_loss_streak = 0
        current_streak_type = None
        for t in resolved:
            if t["pnl"] > 0:
                if current_streak_type == "win":
                    streak += 1
                else:
                    streak = 1
                    current_streak_type = "win"
                max_win_streak = max(max_win_streak, streak)
            else:
                if current_streak_type == "loss":
                    streak += 1
                else:
                    streak = 1
                    current_streak_type = "loss"
                max_loss_streak = max(max_loss_streak, streak)

        if max_win_streak >= 3:
            insights.append({
                "icon": "flame",
                "text": f"En uzun kazanma serisi: {max_win_streak} trade üst üste!",
                "type": "positive",
            })

        # Open positions
        open_positions = [t for t in trades if t.get("status") == "open" or (t.get("pnl") is not None and t["pnl"] == 0 and t.get("status") != "resolved")]

        return {
            "hourly": hourly_list,
            "delta_buckets": delta_buckets,
            "daily": daily_list,
            "win_factors": dict(win_factors),
            "loss_factors": dict(loss_factors),
            "insights": insights,
            "max_drawdown": round(max_dd, 2),
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
            "open_positions": len(open_positions),
            "total_resolved": len(resolved),
            "wallet": {
                "starting": STARTING_BALANCE,
                "current": round(STARTING_BALANCE + cumulative, 2),
                "net_pnl": round(cumulative, 2),
                "peak": round(STARTING_BALANCE + peak, 2),
            },
        }

    def _get_analytics(self) -> dict:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, ts, side, price, cost, pnl, notes, decision_reason, status, strategy
                   FROM trades WHERE strategy IN ('btc_5min_fast', 'btc_15min') ORDER BY id ASC"""
            ).fetchall()
            conn.close()
        except Exception as e:
            log.error(f"Analytics DB error: {e}")
            return {}

        trades = [dict(r) for r in rows]
        for t in trades:
            t["parsed"] = self._parse_notes(t.get("notes", ""))

        result = self._compute_full_analytics(trades)

        # Per-strategy metrics (5m vs 15m)
        resolved = [t for t in trades if t.get("pnl") and t["pnl"] != 0]
        resolved_5m = [t for t in resolved if t.get("strategy") == "btc_5min_fast" or (not t.get("strategy") and "[15m]" not in (t.get("notes") or ""))]
        resolved_15m = [t for t in resolved if t.get("strategy") == "btc_15min" or "[15m]" in (t.get("notes") or "")]

        result["strategy_5m"] = self._compute_strategy_summary(resolved_5m)
        result["strategy_15m"] = self._compute_strategy_summary(resolved_15m)
        return result

    def _get_analytics_test(self) -> dict:
        """Test stratejisi metrikleri — tam analitik."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, ts, side, price, cost, pnl, notes, decision_reason, status, strategy
                   FROM trades WHERE strategy IN ('btc_5min_test', 'btc_15min_test') ORDER BY id ASC"""
            ).fetchall()

            # Signal feedback data
            signal_accuracy = {}
            try:
                sig_rows = conn.execute(
                    """SELECT signal_name,
                           COUNT(*) as total,
                           SUM(CASE WHEN (
                               (signal_direction = trade_direction AND trade_won = 1)
                               OR (signal_direction != trade_direction AND trade_won = 0)
                           ) THEN 1 ELSE 0 END) as correct
                    FROM signal_scores
                    WHERE trade_won IS NOT NULL
                    GROUP BY signal_name"""
                ).fetchall()
                for r in sig_rows:
                    row = dict(r)
                    total = row["total"]
                    correct = row["correct"]
                    signal_accuracy[row["signal_name"]] = {
                        "total": total,
                        "correct": correct,
                        "accuracy": round(correct / total * 100, 1) if total > 0 else 0,
                    }
            except Exception:
                pass

            conn.close()
        except Exception as e:
            log.error(f"Analytics test DB error: {e}")
            return {}

        trades = [dict(r) for r in rows]
        for t in trades:
            t["parsed"] = self._parse_notes(t.get("notes", ""))

        result = self._compute_full_analytics(trades)

        # Per-strategy summaries
        resolved = [t for t in trades if t.get("pnl") and t["pnl"] != 0]
        resolved_test5 = [t for t in resolved if t.get("strategy") == "btc_5min_test"]
        resolved_test15 = [t for t in resolved if t.get("strategy") == "btc_15min_test"]

        result["strategy_test5"] = self._compute_strategy_summary(resolved_test5)
        result["strategy_test15"] = self._compute_strategy_summary(resolved_test15)
        result["signal_accuracy"] = signal_accuracy
        result["total_test_trades"] = len(trades)
        result["total_test_resolved"] = len(resolved)
        return result

    def _get_stats_test(self):
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            row = dict(
                conn.execute(
                    """
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN pnl IS NULL OR pnl = 0 THEN 1 ELSE 0 END) as pending,
                    COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) as total_profit,
                    COALESCE(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END), 0) as total_loss,
                    COALESCE(SUM(pnl), 0) as net_pnl,
                    COALESCE(SUM(cost), 0) as total_cost
                FROM trades WHERE strategy IN ('btc_5min_test', 'btc_15min_test')
            """
                ).fetchone()
            )
            today = dict(
                conn.execute(
                    """
                SELECT
                    COUNT(*) as today_trades,
                    COALESCE(SUM(pnl), 0) as today_pnl,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as today_wins,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as today_losses
                FROM trades
                WHERE strategy IN ('btc_5min_test', 'btc_15min_test') AND date(ts) = date('now')
            """
                ).fetchone()
            )
            row.update(today)
            conn.close()
            resolved = (row.get("wins") or 0) + (row.get("losses") or 0)
            row["win_rate"] = (row["wins"] / resolved * 100) if resolved else 0
            row["starting_balance"] = STARTING_BALANCE
            row["current_balance"] = STARTING_BALANCE + (row.get("net_pnl") or 0)
            return row
        except Exception as e:
            log.error(f"Stats test error: {e}")
            return {}

    def _get_trades_test(self, limit=100):
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM trades WHERE strategy IN ('btc_5min_test', 'btc_15min_test') ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            log.error(f"DB read error (test): {e}")
            return []

    def _get_clob_client(self):
        """Lazy-init py-clob-client for manual trading (Gnosis Safe proxy)."""
        if self._clob_client is not None:
            return self._clob_client
        private_key = os.getenv("POLY_PRIVATE_KEY", "")
        funder = os.getenv("POLY_FUNDER", "")
        if not private_key:
            return None
        try:
            from py_clob_client.client import ClobClient
            self._clob_client = ClobClient(
                CLOB_API,
                key=private_key,
                chain_id=137,
                signature_type=2,  # Gnosis Safe proxy
                funder=funder,
            )
            api_key = os.getenv("POLY_API_KEY", "")
            if api_key:
                from py_clob_client.clob_types import ApiCreds
                self._clob_client.set_api_creds(ApiCreds(
                    api_key=api_key,
                    api_secret=os.getenv("POLY_API_SECRET", ""),
                    api_passphrase=os.getenv("POLY_API_PASSPHRASE", ""),
                ))
            else:
                self._clob_client.create_or_derive_api_creds()
            log.info(f"Dashboard CLOB client initialized (proxy={funder[:10]}...)")
            return self._clob_client
        except Exception as e:
            log.error(f"Failed to init dashboard CLOB client: {e}")
            return None

    async def api_manual_trade(self, req):
        """Execute a manual trade via CLOB API and log to DB."""
        try:
            body = await req.json()
            token_id = body.get("token_id", "")
            direction = body.get("direction", "")
            amount = float(body.get("amount", 0))
            price = float(body.get("price", 0.5))
            market_type = body.get("market_type", "5m")  # "5m" or "15m"

            if not token_id:
                return web.json_response({"success": False, "error": "token_id eksik"}, status=400)
            if direction not in ("up", "down"):
                return web.json_response({"success": False, "error": "Gecersiz yon"}, status=400)
            if amount <= 0:
                return web.json_response({"success": False, "error": "Miktar 0'dan buyuk olmali"}, status=400)
            if price <= 0 or price >= 1:
                return web.json_response({"success": False, "error": f"Gecersiz fiyat: {price}"}, status=400)

            client = self._get_clob_client()
            if not client:
                return web.json_response({"success": False, "error": "CLOB client baslatilamadi. POLY_PRIVATE_KEY kontrol edin."}, status=500)

            import math
            from py_clob_client.order_builder.constants import BUY
            from py_clob_client.clob_types import OrderArgs

            # Polymarket requires 0.01 tick size
            price = math.floor(price * 100) / 100
            if price <= 0 or price >= 1:
                return web.json_response({"success": False, "error": f"Fiyat tick yuvarlamadan sonra gecersiz: {price}"}, status=400)

            shares = math.ceil(amount / price * 100) / 100

            # Minimum share check (Polymarket bazı marketlerde min 5 share istiyor)
            MIN_SHARES = 5
            if shares < MIN_SHARES:
                min_amount = math.ceil(MIN_SHARES * price * 100) / 100
                return web.json_response({
                    "success": False,
                    "error": f"Minimum {MIN_SHARES} share gerekli. Bu fiyatta min ${min_amount:.2f} gerekiyor."
                }, status=400)

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=shares,
                side=BUY,
            )
            order = client.create_order(order_args)
            result = client.post_order(order)

            success = bool(result and result.get("success"))
            order_id = result.get("orderID", "") if result else ""

            log.info(f"Manual trade: {direction.upper()} ${amount:.2f} @ {price:.4f} shares={shares} -> success={success} order={order_id}")

            if success:
                # Log to DB so it appears in trade history
                self._log_manual_trade(
                    direction=direction,
                    price=price,
                    shares=shares,
                    amount=amount,
                    order_id=order_id,
                    market_type=market_type,
                )
                return web.json_response({"success": True, "order_id": order_id})
            else:
                error_msg = str(result) if result else "Order basarisiz"
                return web.json_response({"success": False, "error": error_msg})

        except Exception as e:
            error_str = str(e)
            log.error(f"Manual trade error: {e}", exc_info=True)
            # Parse common CLOB errors for cleaner messages
            if "lower than the minimum" in error_str:
                import re
                m = re.search(r"Size \(([\d.]+)\) lower than the minimum: (\d+)", error_str)
                if m:
                    error_str = f"Minimum {m.group(2)} share gerekli (simdi: {m.group(1)}). Miktari artirin."
            return web.json_response({"success": False, "error": error_str}, status=500)

    def _log_manual_trade(self, direction, price, shares, amount, order_id, market_type):
        """Log a successful manual trade to the DB."""
        try:
            # Get current market question
            market = self._current_market if market_type == "5m" else self._current_market_15m
            question = market.get("question", "") if market else ""

            side = "BUY_UP" if direction == "up" else "BUY_DOWN"
            strategy = "manual_5m" if market_type == "5m" else "manual_15m"

            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                """INSERT INTO trades (ts, strategy, market_id, question, side, price, size, cost,
                   is_paper, status, pnl, notes, decision_reason, execution_id)
                   VALUES (datetime('now'), ?, '', ?, ?, ?, ?, ?, 0, 'open', 0, ?, ?, ?)""",
                (
                    strategy,
                    question[:120],
                    side,
                    price,
                    shares,
                    round(amount, 2),
                    f"manual_trade market_type={market_type}",
                    f"MANUAL {direction.upper()} ${amount:.2f} @ {price:.4f}",
                    order_id,
                ),
            )
            conn.commit()
            conn.close()
            log.info(f"Manual trade logged to DB: {side} ${amount:.2f} order={order_id}")
        except Exception as e:
            log.error(f"Failed to log manual trade to DB: {e}")

    # ── Live trading stats ─────────────────────────────────────

    def _get_live_stats(self):
        """Get stats for live (non-paper) trades only."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row

            row = dict(
                conn.execute(
                    """
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN pnl IS NULL OR pnl = 0 THEN 1 ELSE 0 END) as pending,
                    COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) as total_profit,
                    COALESCE(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END), 0) as total_loss,
                    COALESCE(SUM(pnl), 0) as net_pnl,
                    COALESCE(SUM(cost), 0) as total_cost
                FROM trades WHERE is_paper = 0
            """
                ).fetchone()
            )

            today = dict(
                conn.execute(
                    """
                SELECT
                    COUNT(*) as today_trades,
                    COALESCE(SUM(pnl), 0) as today_pnl,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as today_wins,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as today_losses
                FROM trades
                WHERE is_paper = 0 AND date(ts) = date('now')
            """
                ).fetchone()
            )
            row.update(today)

            # Pending redeems count
            pending_redeems = 0
            try:
                pr = conn.execute(
                    """
                    SELECT COUNT(*) FROM trades
                    WHERE status = 'resolved' AND pnl > 0 AND redeemed = 0
                      AND is_paper = 0 AND condition_id != ''
                    """
                ).fetchone()
                pending_redeems = pr[0] if pr else 0
            except Exception:
                pass

            conn.close()

            resolved = (row.get("wins") or 0) + (row.get("losses") or 0)
            row["win_rate"] = (row["wins"] / resolved * 100) if resolved else 0
            row["polymarket_balance"] = self._polymarket_balance
            row["pending_redeems"] = pending_redeems
            return row
        except Exception as e:
            log.error(f"Live stats error: {e}")
            return {}

    def _fetch_polymarket_balance_sync(self) -> float:
        """Fetch real USDC balance from Polymarket CLOB API (sync)."""
        client = self._get_clob_client()
        if not client:
            return -1.0
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            resp = client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            balance_raw = resp.get("balance", "0")
            # USDC has 6 decimals on Polygon
            balance = float(balance_raw) / 1_000_000
            return balance
        except Exception as e:
            log.error(f"Balance fetch error: {e}")
            return -1.0

    async def _live_stats_loop(self):
        """Periodically broadcast live trading stats + real balance."""
        while self._running:
            try:
                # Fetch balance in executor (sync call)
                loop = asyncio.get_event_loop()
                balance = await loop.run_in_executor(
                    None, self._fetch_polymarket_balance_sync
                )
                if balance >= 0:
                    self._polymarket_balance = balance

                stats = self._get_live_stats()
                stats["type"] = "live_stats"
                await self.broadcast(stats)
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error(f"Live stats loop error: {e}")
            await asyncio.sleep(15)

    async def api_live_stats(self, req):
        return web.json_response(self._get_live_stats())

    # ── background: decisions pipeline ────────────────────────

    async def _decisions_loop(self):
        """Poll decisions file every 2s, broadcast if changed."""
        while self._running:
            try:
                data = self._read_decisions()
                if data:
                    ts = data.get("ts", 0)
                    if ts != self._last_decisions_ts:
                        self._last_decisions_ts = ts
                        data["type"] = "decisions"
                        await self.broadcast(data)
            except asyncio.CancelledError:
                return
            except Exception:
                pass
            await asyncio.sleep(2)

    async def api_decisions(self, req):
        data = self._read_decisions()
        return web.json_response(data or {})

    def _read_decisions(self):
        try:
            with open(DECISIONS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return None

    # ── arbitrage process + api ───────────────────────────────

    @staticmethod
    def _read_json_file(path: str):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None

    @staticmethod
    def _process_running(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _read_pid_file(self, path: str) -> int:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return int((fh.read() or "").strip() or "0")
        except Exception:
            return 0

    def _arb_default_config(self) -> dict:
        return {
            "mode": str(getattr(bot_config, "ARB_DEFAULT_MODE", "paper")).lower(),
            "entry_price_limit": float(getattr(bot_config, "ARB_ENTRY_PRICE_LIMIT", 0.45)),
            "bailout_price": float(getattr(bot_config, "ARB_BAILOUT_PRICE", 0.72)),
            "entry_delay_seconds": int(getattr(bot_config, "ARB_ENTRY_DELAY_SECONDS", 15)),
            "entry_cutoff_seconds": int(getattr(bot_config, "ARB_ENTRY_CUTOFF_SECONDS", 180)),
            "bailout_cutoff_seconds": int(getattr(bot_config, "ARB_BAILOUT_CUTOFF_SECONDS", 120)),
            "scan_interval_seconds": float(getattr(bot_config, "ARB_SCAN_INTERVAL_SECONDS", 5.0)),
            "order_poll_seconds": float(getattr(bot_config, "ARB_ORDER_POLL_SECONDS", 4.0)),
            "order_timeout_seconds": int(getattr(bot_config, "ARB_ORDER_TIMEOUT_SECONDS", 90)),
            "cycle_budget_usd": float(getattr(bot_config, "ARB_CYCLE_BUDGET_USD", 20.0)),
            "max_active_capital_usd": float(getattr(bot_config, "ARB_MAX_ACTIVE_CAPITAL_USD", 120.0)),
            "max_open_cycles": int(getattr(bot_config, "ARB_MAX_OPEN_CYCLES", 4)),
            "max_spread_pct": float(getattr(bot_config, "ARB_MAX_SPREAD_PCT", 0.04)),
            "min_liquidity_usd": float(getattr(bot_config, "ARB_MIN_LIQUIDITY_USD", 100.0)),
            "dynamic_price_min": float(getattr(bot_config, "ARB_DYNAMIC_PRICE_MIN", 0.44)),
            "dynamic_price_max": float(getattr(bot_config, "ARB_DYNAMIC_PRICE_MAX", 0.46)),
            "lock_main_strategy": bool(getattr(bot_config, "ARB_LOCK_MAIN_STRATEGY", True)),
        }

    @staticmethod
    def _to_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _normalize_arb_config(self, incoming: dict) -> dict:
        cfg = self._arb_default_config()
        if not isinstance(incoming, dict):
            return cfg

        casts = {
            "entry_price_limit": float,
            "bailout_price": float,
            "entry_delay_seconds": int,
            "entry_cutoff_seconds": int,
            "bailout_cutoff_seconds": int,
            "scan_interval_seconds": float,
            "order_poll_seconds": float,
            "order_timeout_seconds": int,
            "cycle_budget_usd": float,
            "max_active_capital_usd": float,
            "max_open_cycles": int,
            "max_spread_pct": float,
            "min_liquidity_usd": float,
            "dynamic_price_min": float,
            "dynamic_price_max": float,
        }

        for key, value in incoming.items():
            if key not in cfg:
                continue
            if key == "mode":
                mode = str(value).strip().lower()
                cfg["mode"] = "live" if mode == "live" else "paper"
                continue
            if key == "lock_main_strategy":
                cfg[key] = self._to_bool(value)
                continue
            caster = casts.get(key)
            if caster:
                try:
                    cfg[key] = caster(value)
                except Exception:
                    pass

        cfg["entry_price_limit"] = max(0.01, min(0.99, float(cfg["entry_price_limit"])))
        cfg["bailout_price"] = max(0.01, min(0.99, float(cfg["bailout_price"])))
        cfg["dynamic_price_min"] = max(0.01, min(0.99, float(cfg["dynamic_price_min"])))
        cfg["dynamic_price_max"] = max(cfg["dynamic_price_min"], min(0.99, float(cfg["dynamic_price_max"])))
        cfg["risk_notice"] = "Non-atomic fill ve queue riski vardir; garanti kar yoktur."
        return cfg

    def _read_arb_runtime_config(self) -> dict:
        current = self._read_json_file(ARB_RUNTIME_CONFIG_FILE) or {}
        cfg = self._normalize_arb_config(current)
        if not os.path.exists(ARB_RUNTIME_CONFIG_FILE):
            self._write_arb_runtime_config(cfg)
        return cfg

    def _write_arb_runtime_config(self, cfg: dict):
        normalized = self._normalize_arb_config(cfg)
        with open(ARB_RUNTIME_CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(normalized, fh, indent=2)

    def _arb_running_pid(self) -> int:
        pid = self._read_pid_file(ARB_PID_LOCK_PATH)
        return pid if self._process_running(pid) else 0

    def _arb_status_payload(self) -> dict:
        status = self._read_json_file(ARB_STATUS_FILE) or {}
        pid = self._arb_running_pid()
        cfg = self._read_arb_runtime_config()
        payload = {
            "running": bool(pid),
            "pid": pid,
            "mode": status.get("mode") or cfg.get("mode", "paper"),
            "configured_mode": cfg.get("mode", "paper"),
            "risk_notice": cfg.get("risk_notice", ""),
        }
        payload.update(status)
        payload["running"] = bool(pid)
        payload["pid"] = pid
        payload["configured_mode"] = cfg.get("mode", "paper")
        payload["risk_notice"] = cfg.get("risk_notice", "")
        return payload

    def _validate_arb_live_guards(self, cfg: dict) -> str:
        if str(cfg.get("mode", "paper")).lower() != "live":
            return ""
        required = {
            "ARB_POLY_PRIVATE_KEY": os.getenv("ARB_POLY_PRIVATE_KEY", ""),
            "ARB_POLY_FUNDER": os.getenv("ARB_POLY_FUNDER", ""),
            "ARB_POLY_API_KEY": os.getenv("ARB_POLY_API_KEY", ""),
            "ARB_POLY_API_SECRET": os.getenv("ARB_POLY_API_SECRET", ""),
            "ARB_POLY_API_PASSPHRASE": os.getenv("ARB_POLY_API_PASSPHRASE", ""),
        }
        missing = [k for k, v in required.items() if not str(v).strip()]
        if missing:
            return f"Live mode blocked: missing {', '.join(missing)}"

        main_funder = str(os.getenv("POLY_FUNDER", "")).strip().lower()
        arb_funder = str(os.getenv("ARB_POLY_FUNDER", "")).strip().lower()
        if main_funder and arb_funder and main_funder == arb_funder:
            return "Live mode blocked: ARB_POLY_FUNDER must differ from POLY_FUNDER"
        return ""

    def _start_arb_process(self) -> bool:
        if self._arb_running_pid():
            return True
        if not os.path.exists(ARB_BOT_PATH):
            log.error("arb_bot.py not found at %s", ARB_BOT_PATH)
            return False
        try:
            log_fp = open(ARB_LOG_FILE, "a", encoding="utf-8")
            try:
                subprocess.Popen(
                    [sys.executable, ARB_BOT_PATH],
                    cwd=str(PROJECT_DIR),
                    stdout=log_fp,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            finally:
                log_fp.close()
        except Exception as exc:
            log.error("Failed to spawn arb bot: %s", exc)
            return False

        for _ in range(10):
            time.sleep(0.2)
            if self._arb_running_pid():
                return True
        return False

    def _stop_arb_process(self) -> bool:
        pid = self._arb_running_pid()
        if not pid:
            # cleanup stale pid file if exists
            if os.path.exists(ARB_PID_LOCK_PATH):
                try:
                    os.remove(ARB_PID_LOCK_PATH)
                except Exception:
                    pass
            return True

        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            return False

        for _ in range(20):
            time.sleep(0.2)
            if not self._process_running(pid):
                break

        if self._process_running(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass

        if os.path.exists(ARB_PID_LOCK_PATH):
            try:
                os.remove(ARB_PID_LOCK_PATH)
            except Exception:
                pass
        return True

    async def api_arb_status(self, req):
        return web.json_response(self._arb_status_payload())

    async def api_arb_start(self, req):
        body = {}
        try:
            body = await req.json()
        except Exception:
            body = {}

        cfg = self._read_arb_runtime_config()
        if isinstance(body, dict):
            if isinstance(body.get("config"), dict):
                cfg.update(body["config"])
            if "mode" in body:
                cfg["mode"] = body.get("mode")
        cfg = self._normalize_arb_config(cfg)
        guard_error = self._validate_arb_live_guards(cfg)
        if guard_error:
            return web.json_response({"ok": False, "error": guard_error}, status=400)

        self._write_arb_runtime_config(cfg)
        ok = self._start_arb_process()
        if not ok:
            return web.json_response({"ok": False, "error": "arb process could not be started"}, status=500)
        await asyncio.sleep(0.6)
        return web.json_response({"ok": True, "status": self._arb_status_payload()})

    async def api_arb_stop(self, req):
        ok = self._stop_arb_process()
        if not ok:
            return web.json_response({"ok": False, "error": "arb process stop failed"}, status=500)
        return web.json_response({"ok": True, "status": self._arb_status_payload()})

    async def api_arb_get_config(self, req):
        cfg = self._read_arb_runtime_config()
        guard_error = self._validate_arb_live_guards(cfg)
        return web.json_response({"config": cfg, "live_guard_error": guard_error})

    async def api_arb_set_config(self, req):
        try:
            body = await req.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        merged = self._read_arb_runtime_config()
        merged.update(body)
        normalized = self._normalize_arb_config(merged)
        guard_error = self._validate_arb_live_guards(normalized)
        if guard_error:
            return web.json_response({"ok": False, "error": guard_error}, status=400)
        try:
            self._write_arb_runtime_config(normalized)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)
        return web.json_response({"ok": True, "config": normalized})

    async def api_arb_summary(self, req):
        days = int(req.query.get("days", 7))
        db = TradeDB(DB_PATH)
        try:
            db.start()
            payload = db.arb_summary(window_days=max(1, min(days, 60)))
            payload["running"] = bool(self._arb_running_pid())
            return web.json_response(payload)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)
        finally:
            try:
                db.stop()
            except Exception:
                pass

    async def api_arb_cycles(self, req):
        limit = max(1, min(int(req.query.get("limit", 100)), 500))
        db = TradeDB(DB_PATH)
        try:
            db.start()
            rows = db.recent_arb_cycles(limit=limit)
            return web.json_response(rows)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)
        finally:
            try:
                db.stop()
            except Exception:
                pass

    async def api_arb_events(self, req):
        limit = max(1, min(int(req.query.get("limit", 200)), 1000))
        cycle_id = req.query.get("cycle_id")
        cycle_id_int = int(cycle_id) if cycle_id and cycle_id.isdigit() else None
        db = TradeDB(DB_PATH)
        try:
            db.start()
            rows = db.recent_arb_events(limit=limit, cycle_id=cycle_id_int)
            return web.json_response(rows)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)
        finally:
            try:
                db.stop()
            except Exception:
                pass

    async def api_arb_telemetry(self, req):
        window_cycles = max(20, min(int(req.query.get("window_cycles", 200)), 2000))
        db = TradeDB(DB_PATH)
        try:
            db.start()
            telemetry = db.arb_telemetry(window_cycles=window_cycles)
            telemetry["running"] = bool(self._arb_running_pid())
            return web.json_response(telemetry)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)
        finally:
            try:
                db.stop()
            except Exception:
                pass

    def _read_status(self):
        try:
            with open(STATUS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return None

    # ── run ────────────────────────────────────────────────────

    def run(self):
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        )
        log.info(f"Dashboard → http://localhost:{self.port}")
        web.run_app(self.app, host=self.host, port=self.port, print=None)


if __name__ == "__main__":
    Dashboard(port=8080).run()
