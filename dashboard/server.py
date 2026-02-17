"""Real-time web dashboard for Polymarket BTC trading bot."""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import aiohttp
from aiohttp import web

log = logging.getLogger("dashboard")

BASE_DIR = Path(__file__).parent
PROJECT_DIR = BASE_DIR.parent
DB_PATH = str(PROJECT_DIR / "trades.db")
STATUS_FILE = os.getenv("STATUS_FILE", "/tmp/polymarket_bot_status.json")
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "1000"))

BINANCE_REST = "https://api.binance.com"
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
TRADE_SIZE_FILE = os.getenv("TRADE_SIZE_FILE", "/tmp/polymarket_bot_trade_size")
MARKET_15M_FILE = os.getenv("MARKET_15M_FILE", "/tmp/polymarket_bot_15m_enabled")


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
        self._current_market = None
        self._current_market_question = ""
        self._current_market_15m = None
        self._current_market_15m_question = ""

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
        total_resolved = len(resolved)

        # Momentum aligned vs divergent
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

        # Strong delta
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

        # Late entry
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

        # Early entry
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

        # Best hour
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

        # ── 6. Open positions ──────────────────────────────────
        open_positions = [t for t in trades if t.get("status") == "open" or (t.get("pnl") is not None and t["pnl"] == 0 and t.get("status") != "resolved")]

        # ── 7. Per-strategy metrics (5m vs 15m) ──────────────
        resolved_5m = [t for t in resolved if t.get("strategy") == "btc_5min_fast" or (not t.get("strategy") and "[15m]" not in (t.get("notes") or ""))]
        resolved_15m = [t for t in resolved if t.get("strategy") == "btc_15min" or "[15m]" in (t.get("notes") or "")]

        strategy_5m = self._compute_strategy_summary(resolved_5m)
        strategy_15m = self._compute_strategy_summary(resolved_15m)

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
            "total_resolved": total_resolved,
            "strategy_5m": strategy_5m,
            "strategy_15m": strategy_15m,
            "wallet": {
                "starting": STARTING_BALANCE,
                "current": round(STARTING_BALANCE + cumulative, 2),
                "net_pnl": round(cumulative, 2),
                "peak": round(STARTING_BALANCE + peak, 2),
            },
        }

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
