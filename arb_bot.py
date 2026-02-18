"""Separate-process arbitrage bot runner."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from typing import Any, Dict

from config import config
from core.arb_limit_engine import ArbLimitEngine
from core.database import TradeDB
from utils.logger import setup_logging

setup_logging(config.LOG_LEVEL)
log = logging.getLogger("arb_bot")


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
                existing = int((fh.read() or "").strip() or "0")
        except Exception:
            existing = 0
        if existing and existing != pid and _process_exists(existing):
            raise RuntimeError(f"Arb bot already running (pid={existing})")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(pid))
    return pid


def _release_pid_lock(path: str, pid: int):
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            existing = int((fh.read() or "").strip() or "0")
    except Exception:
        existing = 0
    if existing == pid:
        try:
            os.remove(path)
        except OSError:
            pass


def _write_json(path: str, payload: dict):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except Exception:
        pass


class ArbBot:
    def __init__(self):
        self.db = TradeDB(config.DB_PATH)
        self.engine = ArbLimitEngine(config, self.db, mode=getattr(config, "ARB_DEFAULT_MODE", "paper"))
        self._running = False
        self._started_at = 0.0

    # ------------------------------------------------------------------
    # Runtime config file
    # ------------------------------------------------------------------

    def _default_runtime_config(self) -> Dict[str, Any]:
        return self.engine.runtime_config()

    def _ensure_runtime_config(self):
        if os.path.exists(config.ARB_RUNTIME_CONFIG_FILE):
            return
        _write_json(config.ARB_RUNTIME_CONFIG_FILE, self._default_runtime_config())

    def _read_runtime_config(self) -> Dict[str, Any]:
        self._ensure_runtime_config()
        try:
            with open(config.ARB_RUNTIME_CONFIG_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        merged = self._default_runtime_config()
        for key, value in data.items():
            if key in merged:
                merged[key] = value
        return merged

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def _validate_live_mode(self, runtime_cfg: Dict[str, Any]):
        mode = str(runtime_cfg.get("mode", "paper")).lower()
        if mode != "live":
            return

        required = {
            "ARB_POLY_PRIVATE_KEY": getattr(config, "ARB_POLY_PRIVATE_KEY", ""),
            "ARB_POLY_FUNDER": getattr(config, "ARB_POLY_FUNDER", ""),
            "ARB_POLY_API_KEY": getattr(config, "ARB_POLY_API_KEY", ""),
            "ARB_POLY_API_SECRET": getattr(config, "ARB_POLY_API_SECRET", ""),
            "ARB_POLY_API_PASSPHRASE": getattr(config, "ARB_POLY_API_PASSPHRASE", ""),
        }
        missing = [k for k, v in required.items() if not str(v).strip()]
        if missing:
            raise RuntimeError(f"Live mode blocked: missing credentials {', '.join(missing)}")

        main_funder = str(getattr(config, "POLY_FUNDER", "")).strip().lower()
        arb_funder = str(getattr(config, "ARB_POLY_FUNDER", "")).strip().lower()
        if main_funder and arb_funder and main_funder == arb_funder:
            raise RuntimeError("Live mode blocked: ARB_POLY_FUNDER must be different from POLY_FUNDER")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _write_status(self):
        snap = self.db.latest_arb_runtime_snapshot()
        payload = {
            "running": self._running,
            "pid": os.getpid(),
            "mode": self.engine.mode,
            "uptime_seconds": int(max(0.0, time.time() - self._started_at)) if self._started_at else 0,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        }
        payload.update(snap or {})
        _write_json(config.ARB_STATUS_FILE, payload)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        self.db.start()
        runtime_cfg = self._read_runtime_config()
        self._validate_live_mode(runtime_cfg)
        self.engine.apply_runtime_config(runtime_cfg)

        await self.engine.start()
        self._running = True
        self._started_at = time.time()
        self._write_status()
        log.info("Arb bot started | mode=%s", self.engine.mode.upper())

        while self._running:
            runtime_cfg = self._read_runtime_config()
            self._validate_live_mode(runtime_cfg)
            self.engine.apply_runtime_config(runtime_cfg)
            await self.engine.tick()
            self._write_status()
            await self.engine.sleep_interval()

    async def stop(self):
        was_running = self._running
        self._running = False
        try:
            if was_running:
                await self.engine.stop()
        finally:
            try:
                self.db.stop()
            except Exception:
                pass
            _write_json(
                config.ARB_STATUS_FILE,
                {
                    "running": False,
                    "pid": os.getpid(),
                    "mode": self.engine.mode,
                    "stopped_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                },
            )
            log.info("Arb bot stopped")


async def main():
    lock_pid = _acquire_pid_lock(config.ARB_PID_LOCK_PATH)
    current = {"bot": None}

    loop = asyncio.get_running_loop()

    def _signal_stop():
        bot = current.get("bot")
        if bot is not None:
            asyncio.ensure_future(bot.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_stop)

    try:
        bot = ArbBot()
        current["bot"] = bot
        await bot.start()
    finally:
        bot = current.get("bot")
        if bot is not None:
            try:
                await bot.stop()
            except Exception:
                pass
        _release_pid_lock(config.ARB_PID_LOCK_PATH, lock_pid)


if __name__ == "__main__":
    asyncio.run(main())
