"""Polymarket bot entrypoint with core-only strategy orchestration."""

import asyncio
import logging
import os
import signal
from typing import Optional

from config import config
from core.binance_feed import BinanceFeed
from core.database import TradeDB
from core.executor import Executor
from core.polymarket_client import PolymarketClient
from core.risk_manager import RiskManager
from core.trade_resolver import TradeResolver
from dashboard.server import DashboardServer
from strategies.binance_spot_margin_core import BinanceSpotMarginCoreStrategy
from strategies.polymarket_core import PolymarketCoreStrategy
from utils.logger import setup_logging

setup_logging(config.LOG_LEVEL)
log = logging.getLogger("bot")


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_pid_lock(path: str) -> int:
    pid = os.getpid()

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read().strip()
            existing = int(raw) if raw else 0
        except Exception:
            existing = 0

        if existing and existing != pid and _process_exists(existing):
            raise RuntimeError(f"Another bot process is active (pid={existing}).")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(pid))

    return pid


def _release_pid_lock(path: str, pid: int):
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
        existing = int(raw) if raw else 0
    except Exception:
        existing = 0

    if existing == pid:
        try:
            os.remove(path)
        except OSError:
            pass


class Bot:

    def __init__(self):
        self.poly = PolymarketClient(config)
        self.binance = BinanceFeed(config)
        self.db = TradeDB(config.DB_PATH)
        self.risk = RiskManager(config, self.db)
        self.executor = Executor(config, self.db)

        deps = (config, self.poly, self.binance, self.db, self.risk, self.executor)
        self.strategies = [
            PolymarketCoreStrategy(*deps),
            BinanceSpotMarginCoreStrategy(*deps),
        ]

        # Optional legacy strategies (disabled by default).
        if not getattr(config, "CORE_STRATEGIES_ONLY", True):
            from strategies.binance_spot import BinanceSpotTrader
            from strategies.binance_trader import BinanceMomentumTrader
            from strategies.sum_to_one import SumToOneStrategy
            from strategies.temporal_arb import TemporalArbStrategy
            from strategies.weather_arb import WeatherArbStrategy

            self.strategies.extend(
                [
                    BinanceMomentumTrader(*deps),
                    BinanceSpotTrader(*deps),
                    SumToOneStrategy(*deps),
                    TemporalArbStrategy(*deps),
                    WeatherArbStrategy(*deps),
                ]
            )

        self.resolver = TradeResolver(config, self.db, self.poly, self.binance, self.strategies)
        self.dashboard = DashboardServer(config, self.db, self.risk, self.binance, self.poly)

        self._running = False
        self._tasks = []

    async def start(self):
        mode = "PAPER" if config.PAPER_TRADING else "LIVE"
        log.info("=" * 60)
        log.info(f"Starting bot | mode={mode} | bankroll=${config.BANKROLL:,.2f}")
        log.info(f"Strategies: {[s.name for s in self.strategies]}")
        log.info("=" * 60)

        self.db.start()
        await self.poly.start()
        await self.binance.start()
        await self.dashboard.start()
        self._running = True

        coros = [self._run_strategy(s) for s in self.strategies]
        coros.extend(
            [
                self._stats_loop(),
                self._position_monitor(),
                self._validation_loop(),
                self.resolver.run(),
            ]
        )

        self._tasks = [asyncio.create_task(c) for c in coros]

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            log.info("Tasks cancelled")

    async def stop(self):
        if not self._running:
            return

        log.info("Stopping bot")
        self._running = False

        for t in self._tasks:
            if not t.done():
                t.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

        await self.dashboard.stop()
        await self.poly.stop()
        await self.binance.stop()
        self.db.stop()

    async def _run_strategy(self, strategy):
        log.info(f"loop started: {strategy.name} interval={strategy.interval}s")
        while self._running:
            try:
                ok, reason = self.risk.can_trade_strategy(strategy.name)
                if not ok:
                    log.warning(f"{strategy.name} paused: {reason}")
                    await asyncio.sleep(10)
                    continue
                await strategy.scan()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(f"{strategy.name} error: {exc}", exc_info=True)
                await asyncio.sleep(max(2 * strategy.interval, 5))
            await asyncio.sleep(strategy.interval)

    async def _stats_loop(self):
        while self._running:
            await asyncio.sleep(300)
            try:
                stats = self.db.today_stats()
                log.info(
                    f"today trades={stats['trades']} win_rate={stats['win_rate']:.1f}% pnl=${stats['pnl']:+.2f}"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(f"stats loop error: {exc}")

    async def _position_monitor(self):
        while self._running:
            await asyncio.sleep(config.POSITION_CHECK_INTERVAL)
            try:
                count = self.db.open_positions_count()
                log.info(f"open positions={count}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(f"position monitor error: {exc}")

    async def _validation_loop(self):
        interval = max(60, int(getattr(config, "VALIDATION_RUN_INTERVAL_SECONDS", 900)))
        while self._running:
            await asyncio.sleep(interval)
            try:
                snapshot = self.db.validation_gates(
                    window_days=config.VALIDATION_WINDOW_DAYS,
                    bankroll=config.BANKROLL,
                    min_trades=config.VALIDATION_MIN_TRADES,
                    min_win_rate_pct=config.VALIDATION_MIN_WIN_RATE_PCT,
                    min_return_pct=config.VALIDATION_MIN_RETURN_PCT,
                    max_drawdown_pct=config.VALIDATION_MAX_DRAWDOWN_PCT,
                    min_profit_factor=config.VALIDATION_MIN_PROFIT_FACTOR,
                    min_median_net_trade_usd=config.VALIDATION_MIN_MEDIAN_NET_TRADE_USD,
                )
                self.db.log_validation_snapshot(snapshot, window_days=config.VALIDATION_WINDOW_DAYS)

                passed = [s["strategy"] for s in snapshot.get("strategies", []) if s.get("pass")]
                failed = [s["strategy"] for s in snapshot.get("strategies", []) if not s.get("pass")]
                log.info(
                    f"validation window={config.VALIDATION_WINDOW_DAYS}d "
                    f"pass={len(passed)} fail={len(failed)}"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(f"validation loop error: {exc}")


async def main():
    lock_pid = _acquire_pid_lock(config.PID_LOCK_PATH)
    current_bot: dict = {"bot": None}

    loop = asyncio.get_running_loop()

    def _signal_stop():
        bot = current_bot.get("bot")
        if bot is not None:
            asyncio.ensure_future(bot.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_stop)

    restart_delay = 5
    max_restarts = 1000

    try:
        for attempt in range(max_restarts):
            bot = Bot()
            current_bot["bot"] = bot

            try:
                log.info(f"Boot attempt {attempt + 1}")
                await bot.start()
                break
            except KeyboardInterrupt:
                log.info("Interrupted by user")
                break
            except Exception as exc:
                log.error(f"Bot crashed: {exc}", exc_info=True)

            try:
                await bot.stop()
            except Exception:
                pass

            await asyncio.sleep(2)
            log.info(f"Restarting in {restart_delay}s")
            await asyncio.sleep(restart_delay)
            restart_delay = min(int(restart_delay * 1.5), 60)
    finally:
        bot = current_bot.get("bot")
        if bot is not None:
            try:
                await bot.stop()
            except Exception:
                pass
        _release_pid_lock(config.PID_LOCK_PATH, lock_pid)


if __name__ == "__main__":
    asyncio.run(main())
