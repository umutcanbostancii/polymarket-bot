"""
Aiohttp web server for the Polymarket trading bot dashboard.

Serves a local-only dashboard UI and JSON API endpoints exposing
trading statistics, recent trades, opportunities, strategy
performance, and risk status.
"""

import logging
import os
import pathlib
import time
from typing import Optional

from aiohttp import web

logger = logging.getLogger(__name__)

# Resolve the directory that contains this file so we can locate
# index.html regardless of the working directory at runtime.
_HERE = pathlib.Path(os.path.dirname(os.path.abspath(__file__)))


class DashboardServer:
    """Local HTTP dashboard backed by aiohttp."""

    def __init__(self, config, db, risk_manager, binance_feed, polymarket_client):
        self.config = config
        self.db = db
        self.risk_manager = risk_manager
        self.binance_feed = binance_feed
        self.polymarket_client = polymarket_client
        self._spot_strategies = ("binance_spot_margin_core", "binance_spot")
        self._poly_strategies = ("polymarket_core", "sum_to_one", "temporal_arb", "weather_arb")

        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._start_time: float = 0.0

    def _decorate_trades(self, trades: list) -> list:
        out = []
        for t in trades:
            row = dict(t)
            expected_fee = float(row.get("expected_fee_usd") or 0.0)
            realized_fee = float(row.get("realized_fee_usd") or 0.0)
            expected_slip = float(row.get("expected_slippage_usd") or 0.0)
            realized_slip = float(row.get("realized_slippage_usd") or 0.0)
            borrow_fee = float(row.get("borrow_fee_usd") or 0.0)
            expected_gas = float(row.get("expected_gas_usd") or 0.0)
            realized_gas = float(row.get("realized_gas_usd") or 0.0)

            row["fee_impact_usd"] = round(realized_fee + realized_slip + borrow_fee + realized_gas, 6)
            row["expected_cost_usd"] = round(expected_fee + expected_slip + expected_gas, 6)
            row["decision_reason_display"] = row.get("decision_reason") or ""
            row["exit_reason_display"] = row.get("exit_reason") or ""
            out.append(row)
        return out

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build the aiohttp application, bind to 127.0.0.1, and
        begin serving requests."""
        self._start_time = time.time()

        self._app = web.Application()
        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_get("/api/stats", self._handle_stats)
        self._app.router.add_get("/api/trades", self._handle_trades)
        self._app.router.add_get("/api/opportunities", self._handle_opportunities)
        self._app.router.add_get("/api/strategies", self._handle_strategies)
        self._app.router.add_get("/api/risk", self._handle_risk)
        self._app.router.add_get("/api/prices", self._handle_prices)
        self._app.router.add_get("/api/positions", self._handle_positions)
        self._app.router.add_get("/api/pnl", self._handle_pnl)
        self._app.router.add_get("/api/health", self._handle_health)
        self._app.router.add_get("/api/live-feed", self._handle_live_feed)
        self._app.router.add_get("/api/markets", self._handle_markets)
        self._app.router.add_get("/api/diagnostics/decisions", self._handle_decision_diagnostics)
        self._app.router.add_get("/api/diagnostics/cost-breakdown", self._handle_cost_breakdown)
        self._app.router.add_get("/api/diagnostics/edge-drift", self._handle_edge_drift)
        self._app.router.add_get("/api/diagnostics/executions", self._handle_execution_diagnostics)
        self._app.router.add_get("/api/diagnostics/execution-quality", self._handle_execution_quality)
        self._app.router.add_get("/api/diagnostics/validation-gates", self._handle_validation_gates)

        # Binance Spot simulation endpoints
        self._app.router.add_get("/api/spot/stats", self._handle_spot_stats)
        self._app.router.add_get("/api/spot/trades", self._handle_spot_trades)
        self._app.router.add_get("/api/spot/positions", self._handle_spot_positions)
        self._app.router.add_get("/api/spot/pnl", self._handle_spot_pnl)
        self._app.router.add_get("/api/spot/risk", self._handle_spot_risk)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        port = self.config.DASHBOARD_PORT
        self._site = web.TCPSite(self._runner, "127.0.0.1", port, reuse_address=True)
        await self._site.start()

        logger.info(f"Dashboard server started at http://127.0.0.1:{port}")

    async def stop(self) -> None:
        """Gracefully shut down the server and clean up resources."""
        if self._runner is not None:
            await self._runner.cleanup()
            logger.info("Dashboard server stopped")

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    async def _handle_index(self, request: web.Request) -> web.Response:
        """Serve the static ``index.html`` file."""
        try:
            index_path = _HERE / "index.html"
            return web.FileResponse(index_path)
        except Exception:
            logger.exception("Error serving index.html")
            return web.json_response(
                {"error": "Internal server error"}, status=500
            )

    async def _handle_stats(self, request: web.Request) -> web.Response:
        """Return aggregated trading statistics."""
        try:
            today = self.db.today_stats()
            stats = {
                **today,
                "bankroll": self.config.BANKROLL,
                "mode": "PAPER" if self.config.PAPER_TRADING else "LIVE",
                "running": not self.risk_manager.is_halted,
                "uptime": int(time.time() - self._start_time),
                "total_trades": self.db.total_trades_count(),
                "consecutive_losses": self.db.consecutive_losses(),
                "halted": self.risk_manager.is_halted,
            }
            return web.json_response(stats)
        except Exception:
            logger.exception("Error in /api/stats")
            return web.json_response(
                {"error": "Internal server error"}, status=500
            )

    async def _handle_trades(self, request: web.Request) -> web.Response:
        """Return the most recent trades."""
        try:
            trades = self._decorate_trades(self.db.recent_trades(limit=50))
            return web.json_response(trades)
        except Exception:
            logger.exception("Error in /api/trades")
            return web.json_response(
                {"error": "Internal server error"}, status=500
            )

    async def _handle_opportunities(self, request: web.Request) -> web.Response:
        """Return the most recent evaluated opportunities."""
        try:
            opportunities = self.db.recent_opportunities(limit=50)
            return web.json_response(opportunities)
        except Exception:
            logger.exception("Error in /api/opportunities")
            return web.json_response(
                {"error": "Internal server error"}, status=500
            )

    async def _handle_strategies(self, request: web.Request) -> web.Response:
        """Return per-strategy performance statistics."""
        try:
            strategies = self.db.strategy_stats()
            return web.json_response(strategies)
        except Exception:
            logger.exception("Error in /api/strategies")
            return web.json_response(
                {"error": "Internal server error"}, status=500
            )

    async def _handle_risk(self, request: web.Request) -> web.Response:
        """Return current risk / drawdown status."""
        try:
            daily_pnl = self.db.daily_pnl()
            daily_limit = self.config.BANKROLL * self.config.MAX_DAILY_LOSS_PCT

            if daily_limit != 0:
                daily_pct = abs(daily_pnl) / daily_limit * 100.0
            else:
                daily_pct = 0.0

            risk = {
                "daily_pnl": daily_pnl,
                "daily_loss": daily_pnl,
                "daily_limit": daily_limit,
                "daily_loss_limit": daily_limit,
                "daily_pct": round(daily_pct, 2),
                "halted": self.risk_manager.is_halted,
                "consecutive_losses": self.db.consecutive_losses(),
                "consecutive_loss_limit": self.config.COOLDOWN_AFTER_LOSSES,
            }
            return web.json_response(risk)
        except Exception:
            logger.exception("Error in /api/risk")
            return web.json_response(
                {"error": "Internal server error"}, status=500
            )

    async def _handle_prices(self, request: web.Request) -> web.Response:
        """Return live crypto prices from binance_feed."""
        try:
            result = {}
            for symbol in ("BTC", "ETH", "SOL"):
                pp = self.binance_feed.get(symbol)
                if pp is not None:
                    hist = self.binance_feed.history(symbol)
                    change_5m = hist.change_pct(300) if hist else None
                    result[symbol] = {
                        "price": pp.price,
                        "change_5m": change_5m,
                        "age": round(pp.age, 1),
                    }
                else:
                    result[symbol] = {
                        "price": None,
                        "change_5m": None,
                        "age": None,
                    }
            return web.json_response(result)
        except Exception:
            logger.exception("Error in /api/prices")
            return web.json_response(
                {"error": "Internal server error"}, status=500
            )

    async def _handle_positions(self, request: web.Request) -> web.Response:
        """Return all open (unresolved) positions."""
        try:
            positions = self.db.active_positions()
            return web.json_response(positions)
        except Exception:
            logger.exception("Error in /api/positions")
            return web.json_response(
                {"error": "Internal server error"}, status=500
            )

    async def _handle_pnl(self, request: web.Request) -> web.Response:
        """Return daily P&L history for the last 30 days."""
        try:
            pnl = self.db.pnl_history(30)
            return web.json_response(pnl)
        except Exception:
            logger.exception("Error in /api/pnl")
            return web.json_response(
                {"error": "Internal server error"}, status=500
            )

    async def _handle_live_feed(self, request: web.Request) -> web.Response:
        """Return detailed Binance price data with history for charts."""
        try:
            timeframe = str(request.query.get("tf", "1m"))
            try:
                limit = int(request.query.get("limit", "320"))
            except Exception:
                limit = 320
            limit = max(40, min(limit, 420))

            data = self.binance_feed.export_all()
            chart_bundle = await self.binance_feed.export_chart_bundle(
                timeframe=timeframe,
                limit=limit,
            )

            for sym in ("BTC", "ETH", "SOL"):
                series = chart_bundle.get(sym, {})
                candles = series.get("candles") or []
                if sym not in data:
                    data[sym] = {}
                data[sym]["chart"] = series
                data[sym]["history"] = [
                    {"price": float(c.get("close", 0.0)), "ts": float(c.get("ts", 0.0))}
                    for c in candles
                ]
                data[sym]["chart_points"] = len(candles)

            if "_meta" not in data:
                data["_meta"] = {}
            data["_meta"]["timeframe"] = timeframe
            data["_meta"]["chart_limit"] = limit
            return web.json_response(data)
        except Exception:
            logger.exception("Error in /api/live-feed")
            return web.json_response(
                {"error": "Internal server error"}, status=500
            )

    async def _handle_markets(self, request: web.Request) -> web.Response:
        """Return Polymarket active markets being scanned."""
        try:
            crypto = await self.polymarket_client.get_crypto_markets()
            weather = await self.polymarket_client.get_weather_markets()
            result = {
                "crypto": crypto,
                "weather": weather,
                "total": len(crypto) + len(weather),
                "poly_health": self.polymarket_client.health_status,
            }
            return web.json_response(result)
        except Exception:
            logger.exception("Error in /api/markets")
            return web.json_response(
                {"error": "Internal server error"}, status=500
            )

    async def _handle_decision_diagnostics(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "200"))
            limit = max(1, min(limit, 1000))
            rows = self.db.recent_decisions(limit=limit)
            return web.json_response(rows)
        except Exception:
            logger.exception("Error in /api/diagnostics/decisions")
            return web.json_response({"error": "Internal server error"}, status=500)

    async def _handle_cost_breakdown(self, request: web.Request) -> web.Response:
        try:
            days = int(request.query.get("days", "30"))
            days = max(1, min(days, 120))
            data = self.db.diagnostics_cost_breakdown(days=days)
            return web.json_response(data)
        except Exception:
            logger.exception("Error in /api/diagnostics/cost-breakdown")
            return web.json_response({"error": "Internal server error"}, status=500)

    async def _handle_edge_drift(self, request: web.Request) -> web.Response:
        try:
            days = int(request.query.get("days", "30"))
            days = max(1, min(days, 120))
            data = self.db.diagnostics_edge_drift(days=days)
            return web.json_response(data)
        except Exception:
            logger.exception("Error in /api/diagnostics/edge-drift")
            return web.json_response({"error": "Internal server error"}, status=500)

    async def _handle_execution_diagnostics(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "200"))
            limit = max(1, min(limit, 1000))
            rows = self.db.recent_execution_audit(limit=limit)
            return web.json_response(rows)
        except Exception:
            logger.exception("Error in /api/diagnostics/executions")
            return web.json_response({"error": "Internal server error"}, status=500)

    async def _handle_execution_quality(self, request: web.Request) -> web.Response:
        try:
            days = int(request.query.get("days", "30"))
            days = max(1, min(days, 120))
            data = self.db.execution_quality_summary(days=days)
            return web.json_response(data)
        except Exception:
            logger.exception("Error in /api/diagnostics/execution-quality")
            return web.json_response({"error": "Internal server error"}, status=500)

    async def _handle_validation_gates(self, request: web.Request) -> web.Response:
        try:
            days = int(request.query.get("days", "30"))
            days = max(1, min(days, 120))
            current = self.db.validation_gates(
                window_days=days,
                bankroll=self.config.BANKROLL,
                min_trades=self.config.VALIDATION_MIN_TRADES,
                min_win_rate_pct=self.config.VALIDATION_MIN_WIN_RATE_PCT,
                min_return_pct=self.config.VALIDATION_MIN_RETURN_PCT,
                max_drawdown_pct=self.config.VALIDATION_MAX_DRAWDOWN_PCT,
                min_profit_factor=self.config.VALIDATION_MIN_PROFIT_FACTOR,
                min_median_net_trade_usd=self.config.VALIDATION_MIN_MEDIAN_NET_TRADE_USD,
            )
            snapshots = self.db.latest_validation_snapshots(limit=20)
            return web.json_response({"current": current, "snapshots": snapshots})
        except Exception:
            logger.exception("Error in /api/diagnostics/validation-gates")
            return web.json_response({"error": "Internal server error"}, status=500)

    # ------------------------------------------------------------------
    # Binance Spot simulation handlers
    # ------------------------------------------------------------------

    async def _handle_spot_stats(self, request: web.Request) -> web.Response:
        try:
            today = self.db.today_stats_by_strategy(self._spot_strategies)
            stats = {
                **today,
                "bankroll": self.config.SPOT_BANKROLL,
                "mode": "PAPER",
                "total_trades": self.db.total_trades_by_strategy(self._spot_strategies),
                "consecutive_losses": self.db.consecutive_losses_by_strategy(self._spot_strategies),
                "symbols": self.config.SPOT_SYMBOLS,
            }
            return web.json_response(stats)
        except Exception:
            logger.exception("Error in /api/spot/stats")
            return web.json_response({"error": "Internal server error"}, status=500)

    async def _handle_spot_trades(self, request: web.Request) -> web.Response:
        try:
            trades = self._decorate_trades(
                self.db.recent_trades_by_strategy(self._spot_strategies, limit=50)
            )
            return web.json_response(trades)
        except Exception:
            logger.exception("Error in /api/spot/trades")
            return web.json_response({"error": "Internal server error"}, status=500)

    async def _handle_spot_positions(self, request: web.Request) -> web.Response:
        try:
            positions = self.db.active_positions_by_strategy(self._spot_strategies)
            return web.json_response(positions)
        except Exception:
            logger.exception("Error in /api/spot/positions")
            return web.json_response({"error": "Internal server error"}, status=500)

    async def _handle_spot_pnl(self, request: web.Request) -> web.Response:
        try:
            pnl = self.db.pnl_history_by_strategy(self._spot_strategies, 30)
            return web.json_response(pnl)
        except Exception:
            logger.exception("Error in /api/spot/pnl")
            return web.json_response({"error": "Internal server error"}, status=500)

    async def _handle_spot_risk(self, request: web.Request) -> web.Response:
        try:
            daily_pnl = self.db.daily_pnl_by_strategy(self._spot_strategies)
            daily_limit = self.config.SPOT_BANKROLL * self.config.SPOT_MAX_DAILY_LOSS_PCT
            daily_pct = abs(daily_pnl) / daily_limit * 100.0 if daily_limit != 0 else 0.0
            risk = {
                "daily_pnl": daily_pnl,
                "daily_loss": daily_pnl,
                "daily_limit": daily_limit,
                "daily_loss_limit": daily_limit,
                "daily_pct": round(daily_pct, 2),
                "consecutive_losses": self.db.consecutive_losses_by_strategy(self._spot_strategies),
                "consecutive_loss_limit": self.config.COOLDOWN_AFTER_LOSSES,
            }
            return web.json_response(risk)
        except Exception:
            logger.exception("Error in /api/spot/risk")
            return web.json_response({"error": "Internal server error"}, status=500)

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Return connection health for all external services."""
        try:
            health = {
                "binance": self.binance_feed.health_status,
                "polymarket": self.polymarket_client.health_status,
                "uptime": int(time.time() - self._start_time),
                "last_update": time.time(),
            }
            return web.json_response(health)
        except Exception:
            logger.exception("Error in /api/health")
            return web.json_response(
                {"error": "Internal server error"}, status=500
            )
