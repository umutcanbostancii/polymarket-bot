"""BTC 5-min fast loop bot for Polymarket Up or Down markets."""

import asyncio
import json
import logging
import os
import signal
import time

from config import config
from core.binance_feed import BinanceFeed
from core.database import TradeDB
from core.execution_validator import ExecutionValidator
from core.executor import Executor
from core.monitoring import MonitoringEngine
from core.polymarket_client import PolymarketClient
from core.redeemer import PositionRedeemer
from core.risk_manager import RiskManager
from strategies.btc_5min_fast import BTC5MinFastStrategy
from strategies.btc_15min import BTC15MinStrategy
from strategies.btc_5min_test import BTC5MinTestStrategy
from strategies.btc_15min_test import BTC15MinTestStrategy
from signals.aggregator import SignalAggregator
from core.signal_feedback import SignalFeedback
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


def _write_status(status: dict):
    """Write bot status to a JSON file for external scripts to read."""
    try:
        with open(config.STATUS_FILE, "w", encoding="utf-8") as fh:
            json.dump(status, fh, indent=2)
    except Exception:
        pass


class Bot:

    def __init__(self):
        self.poly = PolymarketClient(config)
        self.binance = BinanceFeed(config)
        self.db = TradeDB(config.DB_PATH)
        self.risk = RiskManager(config, self.db)
        self.executor = Executor(config, self.db)
        self.validator = ExecutionValidator(config)
        self.monitoring = MonitoringEngine(config)
        self.redeemer = PositionRedeemer(config, db_path=config.DB_PATH)

        # DeepSeek AI predictor (opsiyonel)
        self.predictor = None
        if config.DEEPSEEK_API_KEY:
            try:
                from core.deepseek_predictor import DeepSeekPredictor
                self.predictor = DeepSeekPredictor(
                    config.DEEPSEEK_API_KEY, config.DEEPSEEK_MODEL
                )
                log.info("DeepSeek AI predictor enabled")
            except Exception as e:
                log.warning(f"DeepSeek init failed: {e}")

        self.strategy = BTC5MinFastStrategy(
            config, self.poly, self.binance, self.db, self.risk, self.executor,
            validator=self.validator,
            monitoring=self.monitoring,
            predictor=self.predictor,
        )

        self.strategy_15m = BTC15MinStrategy(
            config, self.poly, self.binance, self.db, self.risk, self.executor,
            validator=self.validator,
            monitoring=self.monitoring,
            predictor=self.predictor,
        )

        # Test mode strategies
        self.aggregator = SignalAggregator(config)
        self.feedback = SignalFeedback(config.DB_PATH) if getattr(config, "FEEDBACK_ENABLED", True) else None

        self.strategy_test5 = BTC5MinTestStrategy(
            config, self.poly, self.binance, self.db, self.risk, self.executor,
            validator=self.validator,
            monitoring=self.monitoring,
            predictor=self.predictor,
            aggregator=self.aggregator,
            feedback=self.feedback,
        )
        self.strategy_test15 = BTC15MinTestStrategy(
            config, self.poly, self.binance, self.db, self.risk, self.executor,
            validator=self.validator,
            monitoring=self.monitoring,
            predictor=self.predictor,
            aggregator=self.aggregator,
            feedback=self.feedback,
        )

        self._running = False
        self._tasks = []
        self._start_time = 0.0
        self._15m_enabled_cache = False
        self._15m_enabled_check_ts = 0.0
        self._test_enabled_cache = False
        self._test_enabled_check_ts = 0.0

    async def _recover_open_positions(self):
        """Botrecover başladığında açık pozisyonları bul ve resolution zamanla."""
        log.info("Recovering open positions...")
        open_trades = self.db.get_open_trades()
        if not open_trades:
            log.info("No open positions to recover")
            return
        
        log.info(f"Found {len(open_trades)} open positions, scheduling resolution...")
        
        for trade in open_trades:
            trade_id = trade["id"]
            # Market'in ne zaman bittiğini bul (ts'ye bakarak)
            trade_ts = trade.get("ts", "")
            # Eski trade'ler için yaklaşık 5 dk bekleme
            # Yeni trade'ler için market end time'ı hesapla
            import time
            now = time.time()
            
            # Market genellikle 5 dk sürüyor, trade zamanından itibaren kalan süre
            # Basitçe 5 dakika sonrasını bekle
            remaining = 5  # saniye - hemen resolve et
            
            asyncio.create_task(
                self.strategy.force_resolve_trade(
                    trade_id=trade_id,
                    direction="up" if "UP" in trade.get("side", "") else "down",
                    market_id=trade.get("market_id", ""),
                    remaining=remaining,
                )
            )
            log.info(f"Scheduled resolution for trade #{trade_id}")

    def _is_15m_enabled(self) -> bool:
        """Check if 15m strategy is enabled (cached for 10s)."""
        now = time.time()
        if now - self._15m_enabled_check_ts < 10:
            return self._15m_enabled_cache
        self._15m_enabled_check_ts = now
        self._15m_enabled_cache = os.path.exists(config.MARKET_15M_FILE)
        return self._15m_enabled_cache

    def _is_test_mode_enabled(self) -> bool:
        """Check if test mode is enabled (cached for 10s)."""
        now = time.time()
        if now - self._test_enabled_check_ts < 10:
            return self._test_enabled_cache
        self._test_enabled_check_ts = now
        self._test_enabled_cache = os.path.exists(
            getattr(config, "TEST_MODE_FILE", "/tmp/polymarket_bot_test_enabled")
        )
        return self._test_enabled_cache

    async def start(self):
        # Pre-flight kontrol: live mode icin credential dogrulama
        if not config.PAPER_TRADING:
            required = {
                "POLY_API_KEY": config.POLY_API_KEY,
                "POLY_API_SECRET": config.POLY_API_SECRET,
                "POLY_API_PASSPHRASE": config.POLY_API_PASSPHRASE,
                "POLY_PRIVATE_KEY": config.POLY_PRIVATE_KEY,
                "POLY_FUNDER": config.POLY_FUNDER,
            }
            missing = [k for k, v in required.items() if not str(v).strip()]
            if missing:
                raise RuntimeError(f"LIVE MODE BLOCKED: missing: {', '.join(missing)}")
            if config.BANKROLL < 10:
                raise RuntimeError(f"LIVE MODE BLOCKED: BANKROLL=${config.BANKROLL} < $10")
            log.info("PRE-FLIGHT OK: credentials verified, BANKROLL=$%.2f", config.BANKROLL)

        mode = "PAPER" if config.PAPER_TRADING else "LIVE"
        log.info("=" * 60)
        log.info(f"BTC 15-min Fast Loop Bot | mode={mode}")
        log.info(f"Trade size: ${config.TRADE_SIZE_USD} | Edge threshold: {config.EDGE_THRESHOLD}")
        log.info(f"Loop interval: {config.LOOP_INTERVAL}s | 5m: {'ON' if getattr(config, 'MARKET_5M_ENABLED', True) else 'OFF'} | 15m: {'ON' if self._is_15m_enabled() else 'OFF'} | Test: {'ON' if self._is_test_mode_enabled() else 'OFF'}")
        log.info("=" * 60)

        self.db.start()
        await self.poly.start()
        await self.binance.start()
        self._running = True
        self._start_time = time.time()

        # Acık pozisyonları Kurtar (restart sonrasi)
        await self._recover_open_positions()

        _write_status({
            "running": True,
            "mode": mode,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "pid": os.getpid(),
        })

        self._tasks = [
            asyncio.create_task(self._fast_loop()),
            asyncio.create_task(self._stats_loop()),
            asyncio.create_task(self._status_writer()),
            asyncio.create_task(self._redeem_loop()),
        ]

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

        await self.poly.stop()
        await self.binance.stop()
        self.db.stop()

        _write_status({"running": False, "stopped_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())})

    async def _fast_loop(self):
        """Core fast loop - runs strategy.scan() every LOOP_INTERVAL seconds."""
        # Wait for Binance warmup
        while self._running and not self.binance.warmup_complete:
            await asyncio.sleep(0.5)

        log.info(f"Fast loop started (interval={config.LOOP_INTERVAL}s)")

        while self._running:
            # 5-min strategy (if enabled via config)
            if getattr(config, "MARKET_5M_ENABLED", True):
                try:
                    await self.strategy.scan()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error(f"Strategy scan error: {exc}", exc_info=True)
                    await asyncio.sleep(5)

            # 15m strategy (if enabled via dashboard toggle)
            if self._is_15m_enabled():
                try:
                    await self.strategy_15m.scan()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error(f"15m strategy scan error: {exc}", exc_info=True)

            # Test mode strategies
            if self._is_test_mode_enabled():
                try:
                    await self.strategy_test5.scan()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error(f"Test 5m strategy scan error: {exc}", exc_info=True)

                try:
                    await self.strategy_test15.scan()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error(f"Test 15m strategy scan error: {exc}", exc_info=True)

            self._write_decisions()
            await asyncio.sleep(config.LOOP_INTERVAL)

    async def _stats_loop(self):
        """Log stats every 5 minutes."""
        while self._running:
            await asyncio.sleep(300)
            try:
                stats = self.db.today_stats()
                risk_status = self.risk.status()
                btc = self.binance.price
                mon = self.monitoring.metrics
                alarms = self.monitoring.check_alarms()

                btc_str = f"${btc:,.2f}" if btc else "N/A"
                log.info(
                    f"STATS: trades={stats['trades']} "
                    f"W={stats['wins']} L={stats['losses']} "
                    f"win_rate={stats['win_rate']:.1f}% "
                    f"pnl=${stats['pnl']:+.2f} "
                    f"BTC={btc_str} "
                    f"halted={risk_status['halted']}"
                )
                log.info(
                    f"Monitoring: {mon['opportunities_per_min']} opp/min, "
                    f"{mon['executions_per_min']} exec/min, "
                    f"rate={mon['execution_rate_pct']:.0f}%, "
                    f"latency={mon['avg_latency_ms']:.0f}ms, "
                    f"profit=${mon['total_profit']:+.2f}, "
                    f"dd={mon['drawdown_pct']:.1f}%"
                )
                if alarms:
                    log.warning(f"ALARMS: {', '.join(alarms)}")

                # Save monitoring snapshot to DB
                try:
                    self.db.log_monitoring_snapshot(mon, alarms)
                except Exception:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(f"Stats loop error: {exc}")

    async def _redeem_loop(self):
        """Redeem resolved winning positions every 5 minutes."""
        # Skip in paper mode
        if config.PAPER_TRADING:
            log.info("Redeem loop disabled (paper mode)")
            return

        # Wait 60s before first scan (let markets settle)
        await asyncio.sleep(60)
        log.info("Redeem loop started (interval=300s)")

        while self._running:
            try:
                results = await asyncio.get_event_loop().run_in_executor(
                    None, self.redeemer.scan_and_redeem
                )
                for r in results:
                    log.info(
                        f"Redeemed: trade=#{r['trade_id']} "
                        f"condition={r['condition_id'][:12]}... "
                        f"tx={r['tx_hash']}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"Redeem loop error: {e}")

            await asyncio.sleep(300)

    def _write_decisions(self):
        """Write decision pipeline snapshots from all active strategies."""
        try:
            decisions = {}
            # 5m strategy
            if getattr(config, "MARKET_5M_ENABLED", True):
                snap = self.strategy.decision_snapshot()
                if snap:
                    decisions["btc_5min_fast"] = snap
            # 15m strategy
            if self._is_15m_enabled():
                snap = self.strategy_15m.decision_snapshot()
                if snap:
                    decisions["btc_15min"] = snap
            # Test strategies
            if self._is_test_mode_enabled():
                snap = self.strategy_test5.decision_snapshot()
                if snap:
                    decisions["btc_5min_test"] = snap
                snap = self.strategy_test15.decision_snapshot()
                if snap:
                    decisions["btc_15min_test"] = snap

            if decisions:
                import time as _t
                payload = {"ts": _t.time(), "strategies": decisions}
                with open(config.DECISIONS_FILE, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
        except Exception:
            pass

    def _check_trade_size_update(self):
        """Dashboard'dan gelen trade size değişikliğini oku."""
        try:
            path = config.TRADE_SIZE_FILE
            if not os.path.exists(path):
                return
            with open(path, "r") as f:
                new_size = float(f.read().strip())
            # Live mode'da dashboard override'i TRADE_SIZE_USD ile sinirla
            if not config.PAPER_TRADING:
                max_allowed = min(config.TRADE_SIZE_USD, config.BANKROLL * 0.25)
                if new_size > max_allowed:
                    log.warning(f"Trade size override ${new_size} capped to ${max_allowed} (live mode)")
                    new_size = max_allowed
            if new_size > 0 and new_size != config.TRADE_SIZE_USD:
                old_size = config.TRADE_SIZE_USD
                config.TRADE_SIZE_USD = new_size
                log.info(f"Trade size updated: ${old_size} -> ${new_size}")
        except Exception:
            pass

    async def _status_writer(self):
        """Periodically write status file for external scripts."""
        while self._running:
            await asyncio.sleep(10)
            self._check_trade_size_update()
            try:
                stats = self.db.today_stats()
                btc = self.binance.price
                risk_status = self.risk.status()
                uptime = time.time() - self._start_time

                # Monitoring metrics
                mon = self.monitoring.metrics
                alarms = self.monitoring.check_alarms()

                # Current market prices from strategy
                current_market = self.strategy._current_market
                market_prices = {}
                if current_market:
                    market_prices = {
                        "up_price": current_market.get("up_price"),
                        "down_price": current_market.get("down_price"),
                        "market_id": current_market.get("id", ""),
                        "question": current_market.get("question", "")[:80],
                    }

                # 15m market info
                market_15m_prices = {}
                current_market_15m = self.strategy_15m._current_market
                if current_market_15m:
                    market_15m_prices = {
                        "up_price": current_market_15m.get("up_price"),
                        "down_price": current_market_15m.get("down_price"),
                        "market_id": current_market_15m.get("id", ""),
                        "question": current_market_15m.get("question", "")[:80],
                    }

                _write_status({
                    "running": True,
                    "mode": "PAPER" if config.PAPER_TRADING else "LIVE",
                    "pid": os.getpid(),
                    "uptime_seconds": int(uptime),
                    "btc_price": btc,
                    "btc_connected": self.binance.connected,
                    "poly_healthy": self.poly.healthy,
                    "today_trades": stats["trades"],
                    "today_wins": stats["wins"],
                    "today_losses": stats["losses"],
                    "today_win_rate": round(stats["win_rate"], 1),
                    "today_pnl": round(stats["pnl"], 2),
                    "risk_halted": risk_status["halted"],
                    "risk_reason": risk_status["reason"],
                    "daily_pnl": round(risk_status["daily_pnl"], 2),
                    "consecutive_losses": risk_status["consecutive_losses"],
                    "active_positions": risk_status.get("active_positions", 0),
                    "market_prices": market_prices,
                    "market_15m_prices": market_15m_prices,
                    "market_15m_enabled": self._is_15m_enabled(),
                    "test_mode_enabled": self._is_test_mode_enabled(),
                    "monitoring": mon,
                    "alarms": alarms,
                    "trade_size": config.TRADE_SIZE_USD,
                    "ai_enabled": self.predictor is not None,
                    "pending_redeems": self.db.pending_redeem_count() if not config.PAPER_TRADING else 0,
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                })
            except Exception:
                pass


async def main():
    lock_pid = _acquire_pid_lock(config.PID_LOCK_PATH)
    current_bot = {"bot": None}

    loop = asyncio.get_running_loop()

    def _signal_stop():
        bot = current_bot.get("bot")
        if bot is not None:
            asyncio.ensure_future(bot.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_stop)

    try:
        for attempt in range(100):
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

            delay = min(5 * (1.5 ** attempt), 60)
            log.info(f"Restarting in {delay:.0f}s")
            await asyncio.sleep(delay)
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
