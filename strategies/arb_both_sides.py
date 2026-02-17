"""
Both-sides arbitrage strategy for Polymarket BTC 5-minute markets.

Core idea (0x8dxd): If UP_price + DOWN_price < 1.00 USD, buy BOTH positions.
One side always resolves to $1.00 per share, so:
    profit_per_share = 1.00 - UP_price - DOWN_price - fees
This is risk-free arbitrage — the profit is locked in at entry regardless
of which direction BTC moves.
"""

import asyncio
import logging
import time
from typing import Optional

from core.polymarket_client import PolymarketClient
from strategies.base import BaseStrategy

log = logging.getLogger(__name__)


class ArbBothSidesStrategy(BaseStrategy):
    name = "arb_both_sides"

    def __init__(self, config, polymarket, binance, db, risk, executor):
        super().__init__(config, polymarket, binance, db, risk, executor)
        self._current_market = None
        self._current_market_id = None
        self._has_position = False
        self._last_trade_market_id = None
        self._scan_count = 0
        self._trade_count = 0
        self._max_trades = getattr(config, "MAX_ARB_TRADES", 3)

    @property
    def interval(self) -> float:
        return self.cfg.LOOP_INTERVAL

    async def scan(self):
        """One iteration of the arb scan loop."""
        self._scan_count += 1

        # 0. Trade limit check
        if self._max_trades > 0 and self._trade_count >= self._max_trades:
            if self._scan_count % 30 == 1:
                log.info(
                    f"Arb trade limit reached ({self._trade_count}/{self._max_trades})"
                )
            return

        # 1. Risk check
        ok, reason = self.risk.can_trade()
        if not ok:
            if self._scan_count % 30 == 1:
                log.warning(f"Risk blocked: {reason}")
            return

        # 2. Find active BTC 5-min market
        market = await self.poly.find_active_btc_5min_market()
        if not market:
            if self._scan_count % 15 == 1:
                log.info("No active BTC 5-min market found")
            return

        market_id = market.get("id", "")
        end_ts = market.get("end_date_ts", 0)
        now = time.time()
        remaining = end_ts - now

        # 3. New market window — set reference price
        if market_id != self._current_market_id:
            self._current_market_id = market_id
            self._current_market = market
            self._has_position = False

            btc_price = self.binance.price
            if btc_price:
                self.binance.set_reference_price(btc_price)
            log.info(
                f"[ARB] New market: {market.get('question', '')[:80]} "
                f"remaining={remaining:.0f}s ref=${btc_price or 0:,.2f}"
            )

        # 4. Already traded this market?
        if self._has_position or market_id == self._last_trade_market_id:
            return

        # 5. Time window check
        if remaining < self.cfg.MIN_SECONDS_REMAINING:
            return
        if remaining > self.cfg.MAX_SECONDS_REMAINING:
            return

        # 6. Get BOTH orderbooks for real-time prices
        up_token = market.get("up_token")
        down_token = market.get("down_token")
        if not up_token or not down_token:
            return

        up_ob, down_ob = await asyncio.gather(
            self.poly.get_orderbook(up_token),
            self.poly.get_orderbook(down_token),
        )

        # Best ask = cheapest price to buy that token
        up_price = market.get("up_price", 0.5)
        down_price = market.get("down_price", 0.5)

        if up_ob:
            asks = up_ob.get("asks", [])
            if asks:
                up_price = min(float(a["price"]) for a in asks)

        if down_ob:
            asks = down_ob.get("asks", [])
            if asks:
                down_price = min(float(a["price"]) for a in asks)

        if up_price <= 0 or up_price >= 1 or down_price <= 0 or down_price >= 1:
            return

        price_sum = up_price + down_price
        arb_gap = 1.0 - price_sum  # gross arb (before fees)

        # 7. Fee calculation
        fee_up = PolymarketClient.calculate_dynamic_fee(up_price)
        fee_down = PolymarketClient.calculate_dynamic_fee(down_price)
        total_fee = fee_up + fee_down
        net_arb = arb_gap - total_fee  # net arb (after fees)

        # 8. Log opportunity to DB
        self.db.log_opportunity(
            strategy=self.name,
            market_id=market_id,
            question=market.get("question", "")[:120],
            edge=net_arb,
            yes_price=up_price,
            no_price=down_price,
            price_sum=price_sum,
            binance_price=self.binance.price or 0,
            action="arb_check",
            notes=f"gap={arb_gap:.6f} fee={total_fee:.6f} net={net_arb:.6f}",
        )

        log.info(
            f"ARB CHECK: UP={up_price:.4f} DOWN={down_price:.4f} "
            f"sum={price_sum:.4f} gap={arb_gap:.4f} "
            f"fee={total_fee:.4f} net_arb={net_arb:.4f}"
        )

        # 9. Arb decision
        # Paper test mode: enter regardless of edge (to verify system)
        # Live mode: only enter when net_arb > 0
        min_edge = getattr(self.cfg, "MIN_ARB_EDGE", 0.0)
        force_paper = self.cfg.PAPER_TRADING and getattr(
            self.cfg, "ARB_TEST_MODE", False
        )

        if not force_paper and net_arb < min_edge:
            log.info(f"No arb: net_arb={net_arb:.4f} < min_edge={min_edge:.4f}")
            return

        # 10. Position sizing: buy EQUAL shares on both sides
        total_budget = self.cfg.TRADE_SIZE_USD
        shares = total_budget / price_sum
        up_cost = shares * up_price
        down_cost = shares * down_price
        expected_profit = shares * net_arb

        label = "ARB OPPORTUNITY" if net_arb > 0 else "ARB TEST (paper)"
        log.info(
            f"{label}: {shares:.4f} shares each side | "
            f"UP ${up_cost:.2f} + DOWN ${down_cost:.2f} = ${up_cost + down_cost:.2f} | "
            f"expected_profit=${expected_profit:+.4f}"
        )

        # 11. Execute both sides
        btc_price = self.binance.price or 0
        notes_base = (
            f"arb gap={arb_gap:.6f} net={net_arb:.6f} "
            f"sum={price_sum:.4f} remaining={remaining:.0f}s "
            f"btc=${btc_price:,.2f}"
        )

        # Update market dict with live CLOB prices
        market["up_price"] = up_price
        market["down_price"] = down_price

        tid_up = await self.executor.execute_directional_bet(
            market=market,
            direction="up",
            size_usd=up_cost,
            edge=net_arb,
            notes=f"ARB_UP {notes_base}",
            strategy_name=self.name,
        )

        tid_down = await self.executor.execute_directional_bet(
            market=market,
            direction="down",
            size_usd=down_cost,
            edge=net_arb,
            notes=f"ARB_DOWN {notes_base}",
            strategy_name=self.name,
        )

        if tid_up and tid_down:
            self._has_position = True
            self._last_trade_market_id = market_id
            self._trade_count += 1

            log.info(
                f"ARB #{self._trade_count}/{self._max_trades} PLACED: "
                f"UP=#{tid_up} DOWN=#{tid_down} | "
                f"cost=${up_cost + down_cost:.2f} expected=${expected_profit:+.4f}"
            )

            ref_price = self.binance.reference_price
            asyncio.create_task(
                self._schedule_arb_resolution(
                    tid_up, tid_down, market, remaining,
                    up_price, down_price, up_cost, down_cost,
                    ref_price,
                )
            )
        else:
            log.warning(
                f"ARB execution partial failure: UP=#{tid_up} DOWN=#{tid_down}"
            )

    async def _schedule_arb_resolution(
        self,
        tid_up: int,
        tid_down: int,
        market: dict,
        remaining: float,
        up_price: float,
        down_price: float,
        up_cost: float,
        down_cost: float,
        ref_price: Optional[float],
    ):
        """Wait for market to end, then resolve both arb legs."""
        wait_time = remaining + 15
        await asyncio.sleep(wait_time)

        try:
            current_price = self.binance.price

            if current_price is None or ref_price is None or ref_price <= 0:
                for tid in (tid_up, tid_down):
                    self.db.resolve_trade(
                        trade_id=tid,
                        pnl=0.0,
                        exit_reason="resolution_unknown",
                        notes_append="no price data for arb resolution",
                    )
                log.warning("ARB resolution: no price data available")
                return

            btc_went_up = current_price >= ref_price

            # One side wins ($1 payout per share), the other loses (payout = $0)
            if btc_went_up:
                pnl_up = up_cost * (1.0 / up_price - 1)   # WIN
                pnl_down = -down_cost                       # LOSS
            else:
                pnl_up = -up_cost                           # LOSS
                pnl_down = down_cost * (1.0 / down_price - 1)  # WIN

            net_pnl = pnl_up + pnl_down
            resolution_notes = (
                f"btc_{'up' if btc_went_up else 'down'} "
                f"ref=${ref_price:,.2f} final=${current_price:,.2f}"
            )

            self.db.resolve_trade(
                trade_id=tid_up,
                pnl=round(pnl_up, 4),
                exit_reason="arb_resolved",
                notes_append=resolution_notes,
            )
            self.db.resolve_trade(
                trade_id=tid_down,
                pnl=round(pnl_down, 4),
                exit_reason="arb_resolved",
                notes_append=resolution_notes,
            )

            log.info(
                f"ARB RESOLVED: UP=#{tid_up} ${pnl_up:+.4f} | "
                f"DOWN=#{tid_down} ${pnl_down:+.4f} | "
                f"NET=${net_pnl:+.4f} "
                f"{'PROFIT' if net_pnl > 0 else 'LOSS'}"
            )
        except Exception as e:
            log.error(f"Arb resolution error: {e}", exc_info=True)
