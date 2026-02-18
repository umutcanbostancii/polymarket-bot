"""V1 arbitrage limit engine (safe core) for BTC/ETH 15m markets."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

log = logging.getLogger(__name__)


OPEN_CYCLE_STATES = {"waiting_fill", "paired", "partial_unhedged"}


@dataclass
class CycleRuntime:
    cycle_id: int
    symbol: str
    market_id: str
    question: str
    mode: str
    start_ts: float
    end_ts: float
    created_at: float
    entry_price_limit: float
    bailout_price: float
    cycle_budget_usd: float
    shares_target: float
    up_token_id: str
    down_token_id: str
    up_order_id: str = ""
    down_order_id: str = ""
    status: str = "waiting_fill"
    up_fill_shares: float = 0.0
    down_fill_shares: float = 0.0
    up_avg_fill_price: float = 0.0
    down_avg_fill_price: float = 0.0
    up_fill_cost: float = 0.0
    down_fill_cost: float = 0.0
    last_poll_at: float = 0.0
    closed: bool = False


class ArbLimitEngine:
    """Safe-core arbitrage engine for separate process execution."""

    def __init__(self, config, db, mode: str = "paper"):
        self.cfg = config
        self.db = db
        self.mode = "live" if str(mode).lower() == "live" else "paper"

        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._started_at = 0.0

        self._cycles: Dict[int, CycleRuntime] = {}
        self._market_cycle_opened: set[str] = set()
        self._market_reject_logged: set[Tuple[str, str]] = set()

        self._consecutive_errors = 0
        self._consecutive_bails = 0
        self._circuit_open_until = 0.0
        self._halt_reason = ""
        self._ws_connected = False  # kept for telemetry; V1 runs with REST polling

        self._last_rest_request_ts = 0.0
        self._clob_client = None
        self._runtime: Dict[str, Any] = self._default_runtime()

        self._tick_count = 0
        self._last_pipeline: List[tuple] = []  # [(step_name, passed, detail), ...]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=float(getattr(self.cfg, "POLY_REST_TIMEOUT_SECONDS", 10)))
        self._session = aiohttp.ClientSession(timeout=timeout)
        self._running = True
        self._started_at = time.time()
        self._recover_open_cycles()
        log.info("Arb engine started | mode=%s", self.mode.upper())

    async def stop(self):
        self._running = False
        if self.mode == "live":
            await self._cancel_all_open_orders()
        if self._session:
            await self._session.close()
            self._session = None
        log.info("Arb engine stopped")

    @property
    def running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Runtime config
    # ------------------------------------------------------------------

    def _default_runtime(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "entry_price_limit": float(getattr(self.cfg, "ARB_ENTRY_PRICE_LIMIT", 0.45)),
            "bailout_price": float(getattr(self.cfg, "ARB_BAILOUT_PRICE", 0.72)),
            "entry_delay_seconds": int(getattr(self.cfg, "ARB_ENTRY_DELAY_SECONDS", 15)),
            "entry_cutoff_seconds": int(getattr(self.cfg, "ARB_ENTRY_CUTOFF_SECONDS", 180)),
            "bailout_cutoff_seconds": int(getattr(self.cfg, "ARB_BAILOUT_CUTOFF_SECONDS", 120)),
            "scan_interval_seconds": float(getattr(self.cfg, "ARB_SCAN_INTERVAL_SECONDS", 5.0)),
            "order_poll_seconds": float(getattr(self.cfg, "ARB_ORDER_POLL_SECONDS", 4.0)),
            "order_timeout_seconds": int(getattr(self.cfg, "ARB_ORDER_TIMEOUT_SECONDS", 90)),
            "cycle_budget_usd": float(getattr(self.cfg, "ARB_CYCLE_BUDGET_USD", 20.0)),
            "max_active_capital_usd": float(getattr(self.cfg, "ARB_MAX_ACTIVE_CAPITAL_USD", 120.0)),
            "max_open_cycles": int(getattr(self.cfg, "ARB_MAX_OPEN_CYCLES", 4)),
            "max_spread_pct": float(getattr(self.cfg, "ARB_MAX_SPREAD_PCT", 0.04)),
            "min_liquidity_usd": float(getattr(self.cfg, "ARB_MIN_LIQUIDITY_USD", 100.0)),
            "dynamic_price_min": float(getattr(self.cfg, "ARB_DYNAMIC_PRICE_MIN", 0.44)),
            "dynamic_price_max": float(getattr(self.cfg, "ARB_DYNAMIC_PRICE_MAX", 0.46)),
            "lock_main_strategy": bool(getattr(self.cfg, "ARB_LOCK_MAIN_STRATEGY", True)),
        }

    def apply_runtime_config(self, overrides: Dict[str, Any]):
        if not overrides:
            return
        merged = dict(self._runtime)
        for key, value in overrides.items():
            if key not in merged:
                continue
            merged[key] = value
        self._runtime = merged
        self.mode = "live" if str(self._runtime.get("mode", self.mode)).lower() == "live" else "paper"

    def runtime_config(self) -> Dict[str, Any]:
        return dict(self._runtime)

    # ------------------------------------------------------------------
    # Tick loop
    # ------------------------------------------------------------------

    async def tick(self):
        if not self._running:
            return

        self._tick_count += 1
        if self._tick_count % 12 == 1:
            log.info(
                "Arb tick #%d | cycles=%d capital=$%.1f bails=%d errors=%d",
                self._tick_count, len(self._cycles),
                self.db.arb_active_capital_usd(),
                self._consecutive_bails, self._consecutive_errors,
            )

        try:
            await self._process_active_cycles()
            if not self._entry_blocked():
                markets = await self._discover_markets()
                for market in markets:
                    await self._maybe_open_cycle(market)
            self._consecutive_errors = 0
        except Exception as exc:
            self._consecutive_errors += 1
            log.error("Arb tick error: %s", exc, exc_info=True)
            if self._consecutive_errors >= int(getattr(self.cfg, "ARB_MAX_CONSECUTIVE_ERRORS", 8)):
                self._circuit_open_until = time.time() + 30
        finally:
            self._write_runtime_snapshot()

    def _entry_blocked(self) -> bool:
        now = time.time()
        if self._halt_reason:
            return True
        if now < self._circuit_open_until:
            return True
        return False

    async def sleep_interval(self):
        await asyncio.sleep(float(self._runtime["scan_interval_seconds"]))

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def _discover_markets(self) -> List[dict]:
        symbols_raw = str(getattr(self.cfg, "ARB_SYMBOLS", "BTC,ETH"))
        symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
        tasks = [self._discover_symbol_market(symbol) for symbol in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        markets = []
        for result in results:
            if isinstance(result, Exception) or not result:
                continue
            markets.append(result)
        if self._tick_count % 12 == 1:
            log.info(
                "Arb scan: found %d markets (%s)",
                len(markets),
                ", ".join(m["symbol"] for m in markets) or "none",
            )
        return markets

    async def _discover_symbol_market(self, symbol: str) -> Optional[dict]:
        now = time.time()
        window = 900
        start_base = int(now // window) * window
        slug_symbol = symbol.lower()

        for offset in (0, window, -window):
            start_ts = start_base + offset
            slug = f"{slug_symbol}-updown-15m-{start_ts}"
            data = await self._request_gamma_json(f"/events/slug/{slug}")
            if not data:
                continue
            markets = data.get("markets") or []
            if not markets:
                continue
            parsed = self._parse_market(markets[0], symbol=symbol, fallback_start_ts=start_ts)
            if not parsed:
                continue
            if parsed["end_ts"] <= now:
                continue
            return parsed

        # fallback search
        events = await self._request_gamma_json(
            "/events",
            params={"closed": "false", "order": "startDate", "ascending": "false", "limit": 40},
        )
        if not isinstance(events, list):
            return None
        prefix = f"{slug_symbol}-updown-15m-"
        best: Optional[dict] = None
        best_end = 0.0
        for event in events:
            slug = str(event.get("slug", ""))
            if not slug.startswith(prefix):
                continue
            markets = event.get("markets") or []
            if not markets:
                continue
            parsed = self._parse_market(markets[0], symbol=symbol)
            if not parsed:
                continue
            if parsed["end_ts"] <= now:
                continue
            if best is None or parsed["end_ts"] < best_end:
                best = parsed
                best_end = parsed["end_ts"]
        return best

    def _parse_market(self, raw: dict, *, symbol: str, fallback_start_ts: Optional[int] = None) -> Optional[dict]:
        token_ids = raw.get("clobTokenIds", [])
        outcomes = raw.get("outcomes", [])
        prices = raw.get("outcomePrices", [])

        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except Exception:
                token_ids = []
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = []
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except Exception:
                prices = []

        if not isinstance(token_ids, list) or len(token_ids) < 2:
            return None

        up_idx = 0
        down_idx = 1
        if isinstance(outcomes, list) and len(outcomes) >= 2:
            for idx, outcome in enumerate(outcomes):
                name = str(outcome).lower()
                if "up" in name:
                    up_idx = idx
                elif "down" in name:
                    down_idx = idx

        up_token = str(token_ids[up_idx])
        down_token = str(token_ids[down_idx])
        up_price = float(prices[up_idx]) if up_idx < len(prices) else 0.5
        down_price = float(prices[down_idx]) if down_idx < len(prices) else 0.5

        start_ts = self._parse_iso_ts(raw.get("eventStartTime")) or float(fallback_start_ts or 0)
        end_ts = self._parse_iso_ts(raw.get("endDate")) or (start_ts + 900 if start_ts else 0.0)
        if not start_ts or not end_ts:
            return None

        return {
            "symbol": symbol,
            "market_id": str(raw.get("id") or raw.get("conditionId") or ""),
            "condition_id": str(raw.get("conditionId") or ""),
            "question": str(raw.get("question", "")),
            "start_ts": float(start_ts),
            "end_ts": float(end_ts),
            "up_token_id": up_token,
            "down_token_id": down_token,
            "up_price": float(up_price),
            "down_price": float(down_price),
        }

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    async def _maybe_open_cycle(self, market: dict):
        market_id = market["market_id"]
        symbol = market["symbol"]
        pipeline: List[tuple] = []

        if not market_id:
            return
        if market_id in self._market_cycle_opened:
            log.debug("Arb skip %s: already cycled", symbol)
            return
        if any(c.market_id == market_id and not c.closed for c in self._cycles.values()):
            log.debug("Arb skip %s: active cycle exists", symbol)
            return

        pipeline.append(("market_found", True, f"{symbol} {market['question'][:40]}"))

        now = time.time()
        since_start = now - float(market["start_ts"])
        delay = float(self._runtime["entry_delay_seconds"])
        cutoff = float(self._runtime["entry_cutoff_seconds"])
        if since_start < delay:
            log.debug("Arb skip %s: entry delay %.0fs < %ds", symbol, since_start, delay)
            pipeline.append(("timing", False, f"elapsed={since_start:.0f}s < delay={delay:.0f}s"))
            self._last_pipeline = pipeline
            return
        if since_start > cutoff:
            log.debug("Arb skip %s: cutoff %.0fs > %ds", symbol, since_start, cutoff)
            pipeline.append(("timing", False, f"elapsed={since_start:.0f}s > cutoff={cutoff:.0f}s"))
            self._last_pipeline = pipeline
            return
        pipeline.append(("timing", True, f"elapsed={since_start:.0f}s delay={delay:.0f} cutoff={cutoff:.0f}"))

        open_cycles = self.db.arb_open_cycles_count()
        max_cycles = int(self._runtime["max_open_cycles"])
        if open_cycles >= max_cycles:
            log.info("Arb skip: max cycles (%d/%d)", open_cycles, max_cycles)
            pipeline.append(("max_cycles", False, f"{open_cycles}/{max_cycles}"))
            self._last_pipeline = pipeline
            return
        pipeline.append(("max_cycles", True, f"{open_cycles}/{max_cycles}"))

        active_capital = self.db.arb_active_capital_usd()
        budget = float(self._runtime["cycle_budget_usd"])
        max_capital = float(self._runtime["max_active_capital_usd"])
        if active_capital + budget > max_capital:
            log.info("Arb skip: max capital ($%.0f+$%.0f > $%.0f)", active_capital, budget, max_capital)
            pipeline.append(("max_capital", False, f"${active_capital:.0f}+${budget:.0f} > ${max_capital:.0f}"))
            self._last_pipeline = pipeline
            return
        pipeline.append(("max_capital", True, f"${active_capital:.0f}/${max_capital:.0f}"))

        if bool(self._runtime["lock_main_strategy"]) and self.db.has_open_directional_position_for_market(market_id):
            self._log_reject_once(
                market_id=market_id,
                reason_key="lock",
                symbol=symbol,
                question=market["question"],
                reason="lock_main_open_position",
                lock_skipped=True,
            )
            pipeline.append(("lock_check", False, "main strategy has open position"))
            self._last_pipeline = pipeline
            return
        pipeline.append(("lock_check", True, "no conflict"))

        up_book, down_book = await asyncio.gather(
            self._get_orderbook(market["up_token_id"]),
            self._get_orderbook(market["down_token_id"]),
        )
        if not up_book or not down_book:
            log.debug("Arb skip %s: no orderbook", symbol)
            pipeline.append(("orderbook", False, "no orderbook data"))
            self._last_pipeline = pipeline
            return
        pipeline.append(("orderbook", True, "both sides available"))

        max_spread = max(self._spread_pct(up_book), self._spread_pct(down_book))
        max_spread_limit = float(self._runtime["max_spread_pct"])
        if max_spread > max_spread_limit:
            self._log_reject_once(
                market_id=market_id,
                reason_key="spread",
                symbol=symbol,
                question=market["question"],
                reason="spread_reject",
                spread_reject=True,
            )
            pipeline.append(("spread", False, f"{max_spread*100:.1f}% > max {max_spread_limit*100:.1f}%"))
            self._last_pipeline = pipeline
            return
        pipeline.append(("spread", True, f"{max_spread*100:.1f}% <= {max_spread_limit*100:.1f}%"))

        entry_limit = float(self._runtime["entry_price_limit"])
        min_liq = float(self._runtime["min_liquidity_usd"])

        total_depth_up = self._total_ask_depth(up_book)
        total_depth_down = self._total_ask_depth(down_book)
        min_depth = min(total_depth_up, total_depth_down)
        if min_depth < min_liq:
            self._log_reject_once(
                market_id=market_id,
                reason_key="liquidity",
                symbol=symbol,
                question=market["question"],
                reason="liquidity_reject",
                liquidity_reject=True,
            )
            pipeline.append(("liquidity", False, f"${min_depth:.0f} < min ${min_liq:.0f}"))
            self._last_pipeline = pipeline
            return
        pipeline.append(("liquidity", True, f"${min_depth:.0f} >= ${min_liq:.0f}"))

        pipeline.append(("entry_price", True, f"limit={entry_limit:.4f}"))

        competition_score = (self._competition_score(up_book, entry_limit) + self._competition_score(down_book, entry_limit)) / 2.0
        suggested_price = self._suggested_price(competition_score)
        pipeline.append(("competition", True, f"score={competition_score:.2f} suggested={suggested_price:.4f}"))
        self._last_pipeline = pipeline

        per_leg_budget = float(self._runtime["cycle_budget_usd"]) / 2.0
        shares_target = max(5.0 if self.mode == "live" else 1.0, round(per_leg_budget / max(entry_limit, 0.01), 2))

        cycle_id = self.db.create_arb_cycle(
            symbol=market["symbol"],
            market_id=market_id,
            question=market["question"],
            mode=self.mode,
            status="waiting_fill",
            timeframe="15m",
            entry_price_limit=entry_limit,
            bailout_price=float(self._runtime["bailout_price"]),
            cycle_budget_usd=float(self._runtime["cycle_budget_usd"]),
            shares_target=shares_target,
            up_token_id=market["up_token_id"],
            down_token_id=market["down_token_id"],
            competition_score=competition_score,
            dynamic_price_suggested=suggested_price,
        )

        cycle = CycleRuntime(
            cycle_id=cycle_id,
            symbol=market["symbol"],
            market_id=market_id,
            question=market["question"],
            mode=self.mode,
            start_ts=float(market["start_ts"]),
            end_ts=float(market["end_ts"]),
            created_at=time.time(),
            entry_price_limit=entry_limit,
            bailout_price=float(self._runtime["bailout_price"]),
            cycle_budget_usd=float(self._runtime["cycle_budget_usd"]),
            shares_target=float(shares_target),
            up_token_id=market["up_token_id"],
            down_token_id=market["down_token_id"],
        )

        ok = await self._place_entry_orders(cycle)
        if not ok:
            self.db.close_arb_cycle(
                cycle.cycle_id,
                status="bailed",
                exit_reason="entry_order_failed",
                pnl_usd=0.0,
                notes_append="place_failed",
            )
            self._consecutive_bails += 1
            return

        self._cycles[cycle.cycle_id] = cycle
        self._market_cycle_opened.add(market_id)
        self._consecutive_bails = 0
        log.info(
            "Arb cycle #%s OPENED: %s %s limit=%.4f shares=%.2f budget=$%.2f",
            cycle.cycle_id, market["symbol"], market["question"][:60],
            entry_limit, shares_target, float(self._runtime["cycle_budget_usd"]),
        )

    def _log_reject_once(
        self,
        *,
        market_id: str,
        reason_key: str,
        symbol: str,
        question: str,
        reason: str,
        lock_skipped: bool = False,
        liquidity_reject: bool = False,
        spread_reject: bool = False,
    ):
        key = (market_id, reason_key)
        if key in self._market_reject_logged:
            return
        self._market_reject_logged.add(key)
        self.db.log_arb_cycle_reject(
            symbol=symbol,
            market_id=market_id,
            question=question,
            mode=self.mode,
            reason=reason,
            lock_skipped=lock_skipped,
            liquidity_reject=liquidity_reject,
            spread_reject=spread_reject,
            dynamic_price_suggested=self._suggested_price(0.0),
            competition_score=0.0,
        )

    async def _place_entry_orders(self, cycle: CycleRuntime) -> bool:
        price = self._tick_price(cycle.entry_price_limit)
        size = cycle.shares_target

        if self.mode == "paper":
            cycle.up_order_id = f"paper-{cycle.cycle_id}-up"
            cycle.down_order_id = f"paper-{cycle.cycle_id}-down"
            self.db.update_arb_cycle(cycle.cycle_id, up_order_id=cycle.up_order_id, down_order_id=cycle.down_order_id)
            self.db.log_arb_order_event(
                cycle_id=cycle.cycle_id,
                symbol=cycle.symbol,
                market_id=cycle.market_id,
                side="UP",
                action="place",
                order_id=cycle.up_order_id,
                price=price,
                size=size,
                state=cycle.status,
                message="paper_limit",
            )
            self.db.log_arb_order_event(
                cycle_id=cycle.cycle_id,
                symbol=cycle.symbol,
                market_id=cycle.market_id,
                side="DOWN",
                action="place",
                order_id=cycle.down_order_id,
                price=price,
                size=size,
                state=cycle.status,
                message="paper_limit",
            )
            return True

        up_order_id = self._place_live_order(cycle.up_token_id, price, size)
        down_order_id = self._place_live_order(cycle.down_token_id, price, size)
        cycle.up_order_id = up_order_id or ""
        cycle.down_order_id = down_order_id or ""
        self.db.update_arb_cycle(cycle.cycle_id, up_order_id=cycle.up_order_id, down_order_id=cycle.down_order_id)

        self.db.log_arb_order_event(
            cycle_id=cycle.cycle_id,
            symbol=cycle.symbol,
            market_id=cycle.market_id,
            side="UP",
            action="place",
            order_id=cycle.up_order_id,
            price=price,
            size=size,
            state=cycle.status,
            message="live_limit",
        )
        self.db.log_arb_order_event(
            cycle_id=cycle.cycle_id,
            symbol=cycle.symbol,
            market_id=cycle.market_id,
            side="DOWN",
            action="place",
            order_id=cycle.down_order_id,
            price=price,
            size=size,
            state=cycle.status,
            message="live_limit",
        )

        if not cycle.up_order_id or not cycle.down_order_id:
            if cycle.up_order_id:
                self._cancel_live_order(cycle.up_order_id)
            if cycle.down_order_id:
                self._cancel_live_order(cycle.down_order_id)
            return False
        return True

    # ------------------------------------------------------------------
    # Active cycle processing
    # ------------------------------------------------------------------

    async def _process_active_cycles(self):
        if not self._cycles:
            return
        for cycle in list(self._cycles.values()):
            if cycle.closed:
                continue
            try:
                await self._process_cycle(cycle)
            except Exception as exc:
                self._consecutive_errors += 1
                log.error("Cycle process error #%s: %s", cycle.cycle_id, exc, exc_info=True)

    async def _process_cycle(self, cycle: CycleRuntime):
        now = time.time()
        if now - cycle.last_poll_at < float(self._runtime["order_poll_seconds"]):
            return
        cycle.last_poll_at = now

        if cycle.mode == "paper":
            await self._simulate_paper_fills(cycle)
        else:
            await self._poll_live_fills(cycle)

        elapsed = now - cycle.created_at
        log.info(
            "Arb cycle #%d poll: up=%.1f/%.1f down=%.1f/%.1f status=%s elapsed=%.0fs",
            cycle.cycle_id, cycle.up_fill_shares, cycle.shares_target,
            cycle.down_fill_shares, cycle.shares_target, cycle.status, elapsed,
        )

        # Persist rolling fills/state
        self.db.update_arb_cycle(
            cycle.cycle_id,
            status=cycle.status,
            up_fill_shares=cycle.up_fill_shares,
            down_fill_shares=cycle.down_fill_shares,
            cost_usd=cycle.up_fill_cost + cycle.down_fill_cost,
        )

        # Fully paired -> resolve locked pnl
        if cycle.up_fill_shares >= cycle.shares_target and cycle.down_fill_shares >= cycle.shares_target:
            cycle.status = "paired"
            self.db.update_arb_cycle(
                cycle.cycle_id,
                status="paired",
                up_fill_shares=cycle.up_fill_shares,
                down_fill_shares=cycle.down_fill_shares,
                cost_usd=cycle.up_fill_cost + cycle.down_fill_cost,
            )
            pnl = self._paired_locked_pnl(cycle)
            payout = cycle.shares_target
            self.db.close_arb_cycle(
                cycle.cycle_id,
                status="resolved",
                exit_reason="paired_locked",
                pnl_usd=pnl,
                payout_usd=payout,
                cost_usd=cycle.up_fill_cost + cycle.down_fill_cost,
            )
            self.db.log_arb_order_event(
                cycle_id=cycle.cycle_id,
                symbol=cycle.symbol,
                market_id=cycle.market_id,
                action="resolve",
                state="resolved",
                message=f"paired_locked pnl={pnl:.4f}",
            )
            cycle.closed = True
            self._consecutive_bails = 0
            self._cycles.pop(cycle.cycle_id, None)
            return

        if (cycle.up_fill_shares > 0 and cycle.down_fill_shares <= 0) or (cycle.down_fill_shares > 0 and cycle.up_fill_shares <= 0):
            cycle.status = "partial_unhedged"
        else:
            cycle.status = "waiting_fill"

        elapsed = now - cycle.created_at
        time_left = max(0.0, cycle.end_ts - now)
        timeout = float(self._runtime["order_timeout_seconds"])
        bailout_cutoff = float(self._runtime["bailout_cutoff_seconds"])

        if elapsed >= timeout:
            await self._cleanup_cycle(cycle, reason="timeout")
            return
        if time_left <= bailout_cutoff and cycle.status == "partial_unhedged":
            await self._cleanup_cycle(cycle, reason="bailout_cutoff")
            return
        if time_left <= 2:
            await self._cleanup_cycle(cycle, reason="market_expired")
            return

    async def _simulate_paper_fills(self, cycle: CycleRuntime):
        up_book, down_book = await asyncio.gather(
            self._get_orderbook(cycle.up_token_id),
            self._get_orderbook(cycle.down_token_id),
        )
        if not up_book or not down_book:
            return

        up_best_ask = self._best_ask(up_book)
        down_best_ask = self._best_ask(down_book)

        if cycle.up_fill_shares < cycle.shares_target and up_best_ask and up_best_ask <= cycle.entry_price_limit:
            fill_qty = cycle.shares_target - cycle.up_fill_shares
            cycle.up_fill_shares = cycle.shares_target
            cycle.up_avg_fill_price = up_best_ask
            cycle.up_fill_cost += fill_qty * up_best_ask
            log.info(
                "Arb PAPER FILL cycle #%d UP: price=%.4f shares=%.1f",
                cycle.cycle_id, up_best_ask, fill_qty,
            )
            self.db.log_arb_order_event(
                cycle_id=cycle.cycle_id,
                symbol=cycle.symbol,
                market_id=cycle.market_id,
                side="UP",
                action="fill",
                order_id=cycle.up_order_id,
                price=up_best_ask,
                size=cycle.shares_target,
                filled_size=cycle.up_fill_shares,
                remaining_size=max(0.0, cycle.shares_target - cycle.up_fill_shares),
                state=cycle.status,
                message="paper_fill",
            )

        if cycle.down_fill_shares < cycle.shares_target and down_best_ask and down_best_ask <= cycle.entry_price_limit:
            fill_qty = cycle.shares_target - cycle.down_fill_shares
            cycle.down_fill_shares = cycle.shares_target
            cycle.down_avg_fill_price = down_best_ask
            cycle.down_fill_cost += fill_qty * down_best_ask
            log.info(
                "Arb PAPER FILL cycle #%d DOWN: price=%.4f shares=%.1f",
                cycle.cycle_id, down_best_ask, fill_qty,
            )
            self.db.log_arb_order_event(
                cycle_id=cycle.cycle_id,
                symbol=cycle.symbol,
                market_id=cycle.market_id,
                side="DOWN",
                action="fill",
                order_id=cycle.down_order_id,
                price=down_best_ask,
                size=cycle.shares_target,
                filled_size=cycle.down_fill_shares,
                remaining_size=max(0.0, cycle.shares_target - cycle.down_fill_shares),
                state=cycle.status,
                message="paper_fill",
            )

    async def _poll_live_fills(self, cycle: CycleRuntime):
        up_state = self._get_live_order_state(cycle.up_order_id) if cycle.up_order_id else None
        down_state = self._get_live_order_state(cycle.down_order_id) if cycle.down_order_id else None

        self._update_live_fill_from_state(cycle, "UP", up_state)
        self._update_live_fill_from_state(cycle, "DOWN", down_state)

    def _update_live_fill_from_state(self, cycle: CycleRuntime, side: str, state: Optional[dict]):
        if not state:
            return

        filled = float(state.get("filled_size", 0.0) or 0.0)
        price = float(state.get("avg_price", cycle.entry_price_limit) or cycle.entry_price_limit)
        order_status = str(state.get("status", "")).lower()

        if side == "UP":
            prev = cycle.up_fill_shares
            if filled > prev:
                delta = filled - prev
                cycle.up_fill_shares = filled
                cycle.up_avg_fill_price = price
                cycle.up_fill_cost += delta * price
                self.db.log_arb_order_event(
                    cycle_id=cycle.cycle_id,
                    symbol=cycle.symbol,
                    market_id=cycle.market_id,
                    side="UP",
                    action="partial" if filled < cycle.shares_target else "fill",
                    order_id=cycle.up_order_id,
                    price=price,
                    size=cycle.shares_target,
                    filled_size=filled,
                    remaining_size=max(0.0, cycle.shares_target - filled),
                    state=cycle.status,
                    message=order_status or "live_update",
                )
        else:
            prev = cycle.down_fill_shares
            if filled > prev:
                delta = filled - prev
                cycle.down_fill_shares = filled
                cycle.down_avg_fill_price = price
                cycle.down_fill_cost += delta * price
                self.db.log_arb_order_event(
                    cycle_id=cycle.cycle_id,
                    symbol=cycle.symbol,
                    market_id=cycle.market_id,
                    side="DOWN",
                    action="partial" if filled < cycle.shares_target else "fill",
                    order_id=cycle.down_order_id,
                    price=price,
                    size=cycle.shares_target,
                    filled_size=filled,
                    remaining_size=max(0.0, cycle.shares_target - filled),
                    state=cycle.status,
                    message=order_status or "live_update",
                )

    async def _cleanup_cycle(self, cycle: CycleRuntime, *, reason: str):
        await self._cancel_cycle_orders(cycle)

        matched = min(cycle.up_fill_shares, cycle.down_fill_shares)
        unmatched = abs(cycle.up_fill_shares - cycle.down_fill_shares)
        cost = cycle.up_fill_cost + cycle.down_fill_cost

        paired_pnl = self._paired_locked_pnl(cycle, matched_shares=matched) if matched > 0 else 0.0

        if matched <= 0 and unmatched <= 0:
            log.info(
                "Arb cycle #%d CLOSED: reason=%s status=expired pnl=$0.00 cost=$%.2f",
                cycle.cycle_id, reason, cost,
            )
            self.db.close_arb_cycle(
                cycle.cycle_id,
                status="expired",
                exit_reason=f"{reason}_no_fill",
                pnl_usd=0.0,
                cost_usd=0.0,
                payout_usd=0.0,
                notes_append="cleanup_no_fill",
            )
            cycle.closed = True
            self._cycles.pop(cycle.cycle_id, None)
            return

        if unmatched > 0:
            bailout_pnl, bailout_proceeds, bailout_msg = await self._bailout_unhedged(cycle, unmatched)
            total_pnl = paired_pnl + bailout_pnl
            log.info(
                "Arb cycle #%d CLOSED: reason=%s status=bailed pnl=$%.4f cost=$%.2f",
                cycle.cycle_id, reason, total_pnl, cost,
            )
            self.db.close_arb_cycle(
                cycle.cycle_id,
                status="bailed",
                exit_reason=f"{reason}_bailout",
                pnl_usd=total_pnl,
                cost_usd=cycle.up_fill_cost + cycle.down_fill_cost,
                payout_usd=max(0.0, matched) + max(0.0, bailout_proceeds),
                notes_append=bailout_msg,
            )
            self.db.log_arb_order_event(
                cycle_id=cycle.cycle_id,
                symbol=cycle.symbol,
                market_id=cycle.market_id,
                action="bail",
                state="bailed",
                message=f"{reason} unmatched={unmatched:.4f}",
            )
            self._consecutive_bails += 1
            if self._consecutive_bails >= int(getattr(self.cfg, "ARB_MAX_CONSECUTIVE_BAILS", 5)):
                self._halt_reason = "max_consecutive_bails"
            cycle.closed = True
            self._cycles.pop(cycle.cycle_id, None)
            return

        # matched but no unmatched
        self.db.close_arb_cycle(
            cycle.cycle_id,
            status="resolved",
            exit_reason=f"{reason}_matched_only",
            pnl_usd=paired_pnl,
            cost_usd=cycle.up_fill_cost + cycle.down_fill_cost,
            payout_usd=matched,
        )
        cycle.closed = True
        self._cycles.pop(cycle.cycle_id, None)

    async def _bailout_unhedged(self, cycle: CycleRuntime, unmatched: float) -> Tuple[float, float, str]:
        up_excess = max(0.0, cycle.up_fill_shares - cycle.down_fill_shares)
        down_excess = max(0.0, cycle.down_fill_shares - cycle.up_fill_shares)

        if up_excess > 0:
            token_id = cycle.up_token_id
            avg_cost = cycle.up_avg_fill_price or cycle.entry_price_limit
            side = "UP"
        else:
            token_id = cycle.down_token_id
            avg_cost = cycle.down_avg_fill_price or cycle.entry_price_limit
            side = "DOWN"

        if self.mode == "paper":
            orderbook = await self._get_orderbook(token_id)
            best_bid = self._best_bid(orderbook) if orderbook else 0.0
            sell_price = best_bid if best_bid > 0 else min(avg_cost, cycle.bailout_price)
            proceeds = unmatched * sell_price
            pnl = unmatched * (sell_price - avg_cost)
            return pnl, proceeds, f"paper_bail {side} px={sell_price:.4f}"

        sell_price = min(cycle.bailout_price, max(0.01, avg_cost))
        success = self._place_live_sell_order(token_id, self._tick_price(sell_price), unmatched)
        if success:
            proceeds = unmatched * sell_price
            pnl = unmatched * (sell_price - avg_cost)
            return pnl, proceeds, f"live_bail {side} px={sell_price:.4f}"

        # fallback worst-case no bailout fill
        pnl = -unmatched * avg_cost
        return pnl, 0.0, f"live_bail_failed {side}"

    # ------------------------------------------------------------------
    # Runtime snapshot
    # ------------------------------------------------------------------

    def _write_runtime_snapshot(self):
        summary = self.db.arb_summary(window_days=7)
        telemetry = self.db.arb_telemetry(window_cycles=200)

        open_orders = 0
        for cycle in self._cycles.values():
            if cycle.closed:
                continue
            if cycle.up_fill_shares < cycle.shares_target and cycle.up_order_id:
                open_orders += 1
            if cycle.down_fill_shares < cycle.shares_target and cycle.down_order_id:
                open_orders += 1

        snap = {
            "running": self._running,
            "mode": self.mode,
            "uptime_seconds": int(max(0.0, time.time() - self._started_at)),
            "open_orders": int(open_orders),
            "active_cycles": int(self.db.arb_open_cycles_count()),
            "active_capital_usd": float(self.db.arb_active_capital_usd()),
            "net_pnl_usd": float(summary.get("net_pnl_usd", 0.0) or 0.0),
            "fill_rate": float(telemetry.get("fill_rate", 0.0) or 0.0),
            "bailout_rate": float(telemetry.get("bailout_rate", 0.0) or 0.0),
            "liquidity_reject_rate": float(telemetry.get("liquidity_reject_rate", 0.0) or 0.0),
            "lock_skip_rate": float(telemetry.get("lock_skip_rate", 0.0) or 0.0),
            "suggested_price": float(telemetry.get("suggested_price", self._suggested_price(0.0)) or 0.0),
            "consecutive_bails": int(self._consecutive_bails),
            "consecutive_errors": int(self._consecutive_errors),
            "circuit_open": time.time() < self._circuit_open_until,
            "circuit_open_until": int(self._circuit_open_until),
            "halted": bool(self._halt_reason),
            "halt_reason": self._halt_reason,
            "ws_connected": self._ws_connected,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "pipeline": [
                {"step": s[0], "passed": s[1], "detail": s[2]}
                for s in self._last_pipeline
            ],
        }
        self.db.log_arb_runtime_snapshot(snap)

    # ------------------------------------------------------------------
    # Helper calculations
    # ------------------------------------------------------------------

    def _paired_locked_pnl(self, cycle: CycleRuntime, matched_shares: Optional[float] = None) -> float:
        matched = matched_shares if matched_shares is not None else min(cycle.up_fill_shares, cycle.down_fill_shares)
        if matched <= 0:
            return 0.0
        up_px = cycle.up_avg_fill_price or cycle.entry_price_limit
        down_px = cycle.down_avg_fill_price or cycle.entry_price_limit
        return matched * (1.0 - up_px - down_px)

    def _suggested_price(self, competition_score: float) -> float:
        base = float(self._runtime["entry_price_limit"])
        telemetry = self.db.arb_telemetry(window_cycles=120)
        fill_rate = float(telemetry.get("fill_rate", 0.0) or 0.0)
        bail_rate = float(telemetry.get("bailout_rate", 0.0) or 0.0)
        suggested = base
        if fill_rate < 0.20:
            suggested += 0.005
        if fill_rate > 0.45:
            suggested -= 0.003
        if bail_rate > 0.35:
            suggested -= 0.005
        if competition_score > 0.70:
            suggested += 0.001
        pmin = float(self._runtime["dynamic_price_min"])
        pmax = float(self._runtime["dynamic_price_max"])
        return max(pmin, min(pmax, round(suggested, 4)))

    @staticmethod
    def _tick_price(price: float) -> float:
        return math.floor(float(price) * 100.0) / 100.0

    @staticmethod
    def _parse_iso_ts(raw: Any) -> Optional[float]:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    @staticmethod
    def _best_bid(orderbook: Optional[dict]) -> float:
        if not orderbook:
            return 0.0
        bids = orderbook.get("bids") or []
        best = 0.0
        for b in bids:
            try:
                price = float(b.get("price", 0.0))
            except Exception:
                continue
            if price > best:
                best = price
        return best

    @staticmethod
    def _best_ask(orderbook: Optional[dict]) -> float:
        if not orderbook:
            return 0.0
        asks = orderbook.get("asks") or []
        best = 1.0
        found = False
        for a in asks:
            try:
                price = float(a.get("price", 1.0))
            except Exception:
                continue
            if price <= 0:
                continue
            if price < best:
                best = price
                found = True
        return best if found else 0.0

    def _spread_pct(self, orderbook: dict) -> float:
        bid = self._best_bid(orderbook)
        ask = self._best_ask(orderbook)
        if bid <= 0 or ask <= 0:
            return 1.0
        mid = (bid + ask) / 2.0
        if mid <= 0:
            return 1.0
        return (ask - bid) / mid

    @staticmethod
    def _total_ask_depth(orderbook: dict) -> float:
        """Total USD depth on the ask side of the orderbook."""
        asks = orderbook.get("asks") or []
        total = 0.0
        for a in asks:
            try:
                price = float(a.get("price", 0.0))
                size = float(a.get("size", 0.0))
            except Exception:
                continue
            if price > 0 and size > 0:
                total += price * size
        return total

    def _liquidity_at_or_below(self, orderbook: dict, price_limit: float) -> float:
        asks = orderbook.get("asks") or []
        total = 0.0
        for a in asks:
            try:
                price = float(a.get("price", 0.0))
                size = float(a.get("size", 0.0))
            except Exception:
                continue
            if price <= 0 or size <= 0:
                continue
            if price <= price_limit:
                total += price * size
        return total

    def _competition_score(self, orderbook: dict, target: float) -> float:
        asks = orderbook.get("asks") or []
        count = 0
        size_total = 0.0
        for a in asks:
            try:
                price = float(a.get("price", 0.0))
                size = float(a.get("size", 0.0))
            except Exception:
                continue
            if abs(price - target) <= 0.01:
                count += 1
                size_total += size
        return max(0.0, min(1.0, (count / 10.0) + (size_total / 2000.0)))

    # ------------------------------------------------------------------
    # API clients (Gamma/CLOB)
    # ------------------------------------------------------------------

    async def _request_gamma_json(self, path: str, params: Optional[dict] = None) -> Optional[Any]:
        if not self._session:
            return None
        if not path.startswith("/"):
            path = "/" + path
        url = self.cfg.GAMMA_API.rstrip("/") + path
        return await self._request_json(url, params=params)

    async def _request_clob_json(self, path: str, params: Optional[dict] = None) -> Optional[Any]:
        if not self._session:
            return None
        if not path.startswith("/"):
            path = "/" + path
        url = self.cfg.CLOB_API.rstrip("/") + path
        return await self._request_json(url, params=params)

    async def _request_json(self, url: str, params: Optional[dict] = None) -> Optional[Any]:
        attempts = int(getattr(self.cfg, "POLY_RETRY_MAX_ATTEMPTS", 3))
        for attempt in range(attempts):
            await self._throttle()
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status not in (429, 500, 502, 503, 504):
                        return None
            except Exception:
                pass
            await asyncio.sleep(0.2 * (2 ** attempt))
        return None

    async def _throttle(self):
        max_rps = float(getattr(self.cfg, "POLY_MAX_REQUESTS_PER_SEC", 25.0))
        min_interval = 1.0 / max(max_rps, 1.0)
        now = time.time()
        wait = self._last_rest_request_ts + min_interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_rest_request_ts = time.time()

    async def _get_orderbook(self, token_id: str) -> Optional[dict]:
        return await self._request_clob_json("/book", params={"token_id": token_id})

    # ------------------------------------------------------------------
    # Live order integration
    # ------------------------------------------------------------------

    def _ensure_live_client(self):
        if self._clob_client is not None:
            return self._clob_client
        if self.mode != "live":
            return None

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            client = ClobClient(
                self.cfg.CLOB_API,
                key=getattr(self.cfg, "ARB_POLY_PRIVATE_KEY", ""),
                chain_id=137,
                signature_type=2,
                funder=getattr(self.cfg, "ARB_POLY_FUNDER", ""),
            )
            api_key = getattr(self.cfg, "ARB_POLY_API_KEY", "")
            if api_key:
                client.set_api_creds(
                    ApiCreds(
                        api_key=api_key,
                        api_secret=getattr(self.cfg, "ARB_POLY_API_SECRET", ""),
                        api_passphrase=getattr(self.cfg, "ARB_POLY_API_PASSPHRASE", ""),
                    )
                )
            else:
                client.create_or_derive_api_creds()
            self._clob_client = client
            return client
        except Exception as exc:
            log.error("Failed to init ARB clob client: %s", exc)
            return None

    def _place_live_order(self, token_id: str, price: float, size: float) -> Optional[str]:
        client = self._ensure_live_client()
        if not client:
            return None
        try:
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY

            order = client.create_order(OrderArgs(token_id=token_id, price=price, size=size, side=BUY))
            result = client.post_order(order)
            if not result:
                return None
            return str(result.get("orderID") or result.get("orderId") or "")
        except Exception as exc:
            log.error("Live order place failed: %s", exc)
            return None

    def _place_live_sell_order(self, token_id: str, price: float, size: float) -> bool:
        client = self._ensure_live_client()
        if not client:
            return False
        try:
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import SELL

            order = client.create_order(OrderArgs(token_id=token_id, price=price, size=size, side=SELL))
            result = client.post_order(order)
            return bool(result and result.get("success"))
        except Exception as exc:
            log.error("Live bailout sell failed: %s", exc)
            return False

    def _cancel_live_order(self, order_id: str):
        client = self._ensure_live_client()
        if not client or not order_id:
            return
        try:
            client.cancel(order_id)
        except Exception:
            pass

    def _get_live_order_state(self, order_id: str) -> Optional[dict]:
        client = self._ensure_live_client()
        if not client or not order_id:
            return None
        try:
            raw = client.get_order(order_id)
            if not isinstance(raw, dict):
                return None
            filled = float(
                raw.get("filled_size")
                or raw.get("sizeMatched")
                or raw.get("size_matched")
                or raw.get("matchedSize")
                or 0.0
            )
            size = float(raw.get("size") or raw.get("original_size") or raw.get("initialSize") or 0.0)
            avg_price = float(raw.get("avg_price") or raw.get("price") or 0.0)
            status = str(raw.get("status") or raw.get("state") or "").lower()
            return {
                "filled_size": filled,
                "size": size,
                "avg_price": avg_price,
                "status": status,
            }
        except Exception:
            return None

    async def _cancel_cycle_orders(self, cycle: CycleRuntime):
        if cycle.mode == "paper":
            if cycle.up_fill_shares < cycle.shares_target and cycle.up_order_id:
                self.db.log_arb_order_event(
                    cycle_id=cycle.cycle_id,
                    symbol=cycle.symbol,
                    market_id=cycle.market_id,
                    side="UP",
                    action="cancel",
                    order_id=cycle.up_order_id,
                    state=cycle.status,
                    message="paper_cancel",
                )
            if cycle.down_fill_shares < cycle.shares_target and cycle.down_order_id:
                self.db.log_arb_order_event(
                    cycle_id=cycle.cycle_id,
                    symbol=cycle.symbol,
                    market_id=cycle.market_id,
                    side="DOWN",
                    action="cancel",
                    order_id=cycle.down_order_id,
                    state=cycle.status,
                    message="paper_cancel",
                )
            return

        if cycle.up_fill_shares < cycle.shares_target and cycle.up_order_id:
            self._cancel_live_order(cycle.up_order_id)
            self.db.log_arb_order_event(
                cycle_id=cycle.cycle_id,
                symbol=cycle.symbol,
                market_id=cycle.market_id,
                side="UP",
                action="cancel",
                order_id=cycle.up_order_id,
                state=cycle.status,
                message="live_cancel",
            )
        if cycle.down_fill_shares < cycle.shares_target and cycle.down_order_id:
            self._cancel_live_order(cycle.down_order_id)
            self.db.log_arb_order_event(
                cycle_id=cycle.cycle_id,
                symbol=cycle.symbol,
                market_id=cycle.market_id,
                side="DOWN",
                action="cancel",
                order_id=cycle.down_order_id,
                state=cycle.status,
                message="live_cancel",
            )

    async def _cancel_all_open_orders(self):
        for cycle in list(self._cycles.values()):
            try:
                await self._cancel_cycle_orders(cycle)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def _recover_open_cycles(self):
        rows = self.db.open_arb_cycles()
        now = time.time()
        for row in rows:
            try:
                cycle = CycleRuntime(
                    cycle_id=int(row["id"]),
                    symbol=str(row.get("symbol", "")),
                    market_id=str(row.get("market_id", "")),
                    question=str(row.get("question", "")),
                    mode=str(row.get("mode", self.mode)),
                    start_ts=self._parse_iso_ts(row.get("ts_open")) or now,
                    end_ts=(self._parse_iso_ts(row.get("ts_open")) or now) + 900,
                    created_at=self._parse_iso_ts(row.get("ts_open")) or now,
                    entry_price_limit=float(row.get("entry_price_limit", self._runtime["entry_price_limit"]) or self._runtime["entry_price_limit"]),
                    bailout_price=float(row.get("bailout_price", self._runtime["bailout_price"]) or self._runtime["bailout_price"]),
                    cycle_budget_usd=float(row.get("cycle_budget_usd", self._runtime["cycle_budget_usd"]) or self._runtime["cycle_budget_usd"]),
                    shares_target=float(row.get("shares_target", 0.0) or 0.0),
                    up_token_id=str(row.get("up_token_id", "")),
                    down_token_id=str(row.get("down_token_id", "")),
                    up_order_id=str(row.get("up_order_id", "")),
                    down_order_id=str(row.get("down_order_id", "")),
                    status=str(row.get("status", "waiting_fill")),
                    up_fill_shares=float(row.get("up_fill_shares", 0.0) or 0.0),
                    down_fill_shares=float(row.get("down_fill_shares", 0.0) or 0.0),
                    up_fill_cost=float(row.get("cost_usd", 0.0) or 0.0) / 2.0,
                    down_fill_cost=float(row.get("cost_usd", 0.0) or 0.0) / 2.0,
                )
                self._cycles[cycle.cycle_id] = cycle
                if cycle.market_id:
                    self._market_cycle_opened.add(cycle.market_id)
            except Exception:
                continue
