"""
BTC 15-min strategy for Polymarket "Bitcoin Up or Down" markets.

Inherits from BTC5MinFastStrategy with timing overrides for 15-minute windows.
Longer observation period, more stable trends, lower reversal risk.
"""

import logging
import time
from typing import Optional

from core.polymarket_client import PolymarketClient
from strategies.btc_5min_fast import BTC5MinFastStrategy

log = logging.getLogger(__name__)


class BTC15MinStrategy(BTC5MinFastStrategy):
    name = "btc_15min"
    WINDOW_DURATION = 900  # 15 minutes

    async def scan(self):
        """One iteration of the fast loop — 15-min market variant."""
        self._scan_count += 1

        # 0. Live trade limit check
        max_live = getattr(self.cfg, "MAX_LIVE_TEST_TRADES", 0)
        if max_live > 0 and not self.cfg.PAPER_TRADING:
            if self._live_trade_count >= max_live:
                if self._scan_count % 30 == 1:
                    log.info(f"[15m] Live test limit reached ({self._live_trade_count}/{max_live})")
                return

        # 1. Risk check
        ok, reason = self.risk.can_trade()
        if not ok:
            if self._scan_count % 30 == 1:
                log.warning(f"[15m] Risk blocked: {reason}")
            return

        # 2. Find active BTC 15-min market
        market = await self.poly.find_active_btc_15min_market()
        if not market:
            if self._scan_count % 15 == 1:
                log.info("[15m] No active BTC 15-min market found")
            return

        market_id = market.get("id", "")
        end_ts = market.get("end_date_ts", 0)
        now = time.time()
        remaining = end_ts - now

        # 3. New market window? Set reference price + reset observation
        if market_id != self._current_market_id:
            self._current_market_id = market_id
            self._current_market = market
            self._has_position = False
            self._ai_prediction = None
            self._obs_prices = []
            self._obs_poly = []
            self._obs_complete = False
            self._obs_result = None
            if not self.cfg.PAPER_TRADING:
                self._live_trade_count = 0

            btc_price = self.binance.price
            if btc_price:
                self.binance.set_reference_price(btc_price)
            log.info(
                f"[15m] New market: {market.get('question', '')[:80]} "
                f"remaining={remaining:.0f}s ref=${btc_price:,.2f}"
            )

        # 3b. COOLDOWN: son trade'den 60s gecmemisse girme
        if time.time() - self._last_trade_ts < 60:
            if self._scan_count % 30 == 1:
                log.info(f"[15m] SKIP: cooldown active ({60 - (time.time() - self._last_trade_ts):.0f}s left)")
            return

        # 3c. Ayni market_id'ye tekrar girme
        if market_id in self._traded_market_ids:
            if self._scan_count % 30 == 1:
                log.info(f"[15m] SKIP: already traded this market {market_id[:12]}")
            return

        # 4. Already have a position in this market?
        if self._has_position or market_id == self._last_trade_market_id:
            await self._track_live_prices(market)
            return

        # 5. OBSERVATION PHASE (remaining > MAX_SECONDS_REMAINING_15M)
        max_remaining = getattr(self.cfg, "MAX_SECONDS_REMAINING_15M", 540)
        if remaining > max_remaining:
            self._collect_observation(market)
            if self._scan_count % 30 == 1:
                log.info(
                    f"[15m] Observation: collecting... samples={len(self._obs_prices)} "
                    f"remaining={remaining:.0f}s"
                )
            return

        # 6. Observation complete?
        if not self._obs_complete:
            self._obs_result = self._analyze_observation()
            self._obs_complete = True
            if self._obs_result:
                log.info(
                    f"[15m] Observation complete: direction={self._obs_result['direction']} "
                    f"trend_pct={self._obs_result['trend_pct']:.6f} "
                    f"consistency={self._obs_result['consistency']:.2f} "
                    f"volatility={self._obs_result['volatility']:.6f} "
                    f"poly_drift={self._obs_result['poly_drift']:.4f} "
                    f"samples={self._obs_result['sample_count']}"
                )
            else:
                log.info("[15m] Observation complete: insufficient data for analysis")

            # AI prediction
            if self.predictor:
                try:
                    ai_data = await self._gather_ai_data()
                    if ai_data:
                        self._ai_prediction = await self.predictor.predict(ai_data)
                        if self._ai_prediction:
                            log.info(
                                f"[15m] AI prediction: {self._ai_prediction['direction'].upper()} "
                                f"conf={self._ai_prediction['confidence']:.2f}"
                            )
                except Exception as e:
                    log.warning(f"[15m] AI prediction failed: {e}")

        # 7. Entry window check
        min_remaining = getattr(self.cfg, "MIN_SECONDS_REMAINING_15M", 60)
        if remaining < min_remaining:
            return

        # 8. Get BTC price data
        delta = self.binance.get_delta()
        if delta is None:
            return

        momentum = self.binance.get_momentum(seconds=30)
        vol = self.binance.get_volatility(seconds=900)

        # 9. NOISE FILTER: 15m uses MIN_DELTA_FOR_ENTRY_15M
        min_delta = getattr(self.cfg, "MIN_DELTA_FOR_ENTRY_15M", 0.0005)
        if abs(delta) < min_delta:
            return

        # 9b. MOMENTUM CAP: cok yuksek momentum = trend uzamis, mean reversion riski
        max_mom = getattr(self.cfg, "MAX_MOMENTUM_FOR_ENTRY_15M", 0.0006)
        if momentum is not None and abs(momentum) > max_mom:
            if self._scan_count % 30 == 1:
                log.info(f"[15m] SKIP: momentum too high |{momentum:.6f}| > {max_mom}")
            return

        # 10. Determine direction
        if delta > 0 and (momentum is None or momentum >= 0):
            direction = "up"
        elif delta < 0 and (momentum is None or momentum <= 0):
            direction = "down"
        else:
            return

        # 11. MACRO TREND FILTER: 15m + 30m BTC trend
        macro_15m = self.binance.get_momentum(seconds=900)
        macro_30m = self.binance.get_momentum(seconds=1800)
        macro = None
        if macro_15m is not None:
            if macro_15m > 0.0001 and (macro_30m is None or macro_30m > 0):
                macro = "up"
            elif macro_15m < -0.0001 and (macro_30m is None or macro_30m <= 0):
                macro = "down"

        if macro and macro != direction:
            if self._scan_count % 30 == 1:
                log.info(f"[15m] SKIP: {direction.upper()} vs macro trend {macro.upper()} (15m={macro_15m:.6f})")
            return

        # 12. OBSERVATION TREND FILTER
        obs_consistency_min = getattr(self.cfg, "OBSERVATION_CONSISTENCY_MIN_15M", 0.50)
        if self._obs_result:
            obs_dir = self._obs_result["direction"]
            obs_consistency = self._obs_result["consistency"]

            if obs_dir != direction:
                if self._scan_count % 30 == 1:
                    log.info(
                        f"[15m] SKIP: {direction.upper()} vs observation trend {obs_dir.upper()} "
                        f"(consistency={obs_consistency:.2f})"
                    )
                return

            if obs_consistency < obs_consistency_min:
                if self._scan_count % 30 == 1:
                    log.info(
                        f"[15m] SKIP: low observation consistency={obs_consistency:.2f} "
                        f"(min={obs_consistency_min})"
                    )
                return

        # 13-17: Fresh price, orderbook, edge, validation, execution
        # Reuse parent logic from here via _execute_entry
        await self._execute_entry(market, market_id, direction, delta, momentum, vol, remaining, macro)

    def calculate_edge(
        self,
        delta_pct: float,
        momentum: Optional[float],
        volatility: Optional[float],
        seconds_remaining: float,
        market: dict,
        direction: str,
    ) -> Optional[float]:
        """15-min edge: time factor uses 900s window instead of 300s."""
        time_factor = 1.0 - (seconds_remaining / 900.0)
        time_factor = max(0.0, min(time_factor, 1.0))

        abs_delta = abs(delta_pct)
        momentum_strength = abs_delta * (1 + time_factor)

        reversal_risk = 0.0
        if volatility is not None:
            reversal_risk = volatility * (seconds_remaining / 900.0)

        p_real = (
            0.50
            + momentum_strength * self.cfg.MOMENTUM_WEIGHT
            - reversal_risk * self.cfg.REVERSAL_RISK_WEIGHT
        )
        p_real = max(0.30, min(p_real, 0.95))

        if direction == "up":
            pm_implied = market.get("up_price", 0.5)
        else:
            pm_implied = market.get("down_price", 0.5)

        if pm_implied <= 0 or pm_implied >= 1:
            return None

        fee = PolymarketClient.calculate_dynamic_fee(pm_implied)
        edge = p_real - pm_implied - fee - self.cfg.SLIPPAGE_ESTIMATE - self.cfg.FEE_BUFFER
        return edge

    async def _execute_entry(self, market, market_id, direction, delta, momentum, vol, remaining, macro):
        """Execute trade entry — extracted from parent scan() steps 13-17."""
        import asyncio

        # 13. FRESH PRICE FETCH
        fresh_market = await self.poly.get_market_by_id(market.get("id"))
        if fresh_market:
            market = fresh_market
            log.info(f"[15m] FRESH PRICES: UP={market.get('up_price')} DOWN={market.get('down_price')}")

        # 14. Orderbook + CLOB price
        token_id = market.get("up_token" if direction == "up" else "down_token")
        orderbook = None
        ob_metrics = None
        if token_id:
            orderbook = await self.poly.get_orderbook(token_id)
            if orderbook:
                ob_metrics = self.poly.get_orderbook_metrics(orderbook)

        if orderbook:
            bids = orderbook.get("bids", [])
            asks = orderbook.get("asks", [])
            if bids and asks:
                best_bid = max(float(b["price"]) for b in bids)
                best_ask = min(float(a["price"]) for a in asks)
                midpoint = (best_bid + best_ask) / 2.0
                if direction == "up":
                    market["up_price"] = round(midpoint, 4)
                    market["down_price"] = round(1.0 - midpoint, 4)
                else:
                    market["down_price"] = round(midpoint, 4)
                    market["up_price"] = round(1.0 - midpoint, 4)
                log.info(f"[15m] CLOB LIVE PRICE: UP={market['up_price']} DOWN={market['down_price']}")

        # 14b. CLOB fiyat zorunlulugu — Gamma API bu marketlerde yanlis fiyat veriyor
        if not orderbook or not (orderbook.get("bids") and orderbook.get("asks")):
            if self._scan_count % 30 == 1:
                log.info("[15m] SKIP: no CLOB orderbook — Gamma price unreliable")
            return

        edge = 0.05  # Fixed edge

        # 15. Position sizing
        price = market.get("up_price" if direction == "up" else "down_price", 0.5)
        size = self.cfg.TRADE_SIZE_USD

        # 15b. PRICE CAP: cok pahali giris = kotu risk/odul orani
        max_price = getattr(self.cfg, "MAX_ENTRY_PRICE_15M", 0.80)
        if price > max_price:
            if self._scan_count % 30 == 1:
                log.info(f"[15m] SKIP: entry price too high {price:.4f} > {max_price}")
            return

        # 16. Execution Validation
        t_detect = time.monotonic()
        fill_quality = 0.0
        if self.validator and orderbook:
            validation = self.validator.validate(
                orderbook=orderbook,
                side="buy",
                size_usd=size,
                predicted_edge=edge,
                market_price=price,
            )
            fill_quality = validation.get("fill_quality", 0.0)

            if not validation["can_execute"]:
                reject_reason = validation["rejection_reason"]
                if self.cfg.PAPER_TRADING:
                    log.debug(f"[15m] Validation info (paper skip): {reject_reason}")
                else:
                    log.info(f"[15m] Validation REJECTED: {reject_reason}")
                    return
            else:
                log.info(
                    f"[15m] Validation PASSED: net=${validation['net_profit_usd']:.4f} "
                    f"slip={validation['slippage_pct']:.4f} fq={fill_quality:.2f}"
                )

        # 17. Execute
        btc_price = self.binance.price or 0
        obs_info = ""
        if self._obs_result:
            obs_info = (
                f" obs={self._obs_result['direction']}"
                f"(c={self._obs_result['consistency']:.2f})"
            )
        macro_info = f" macro={macro or 'neutral'}"

        notes = (
            f"delta={delta:.6f} mom={momentum:.6f} "
            f"vol={f'{vol:.6f}' if vol else 'N/A'} "
            f"remaining={remaining:.0f}s btc=${btc_price:,.2f}"
            f"{obs_info}{macro_info} [15m]"
        )
        if self._ai_prediction:
            notes += (
                f" ai={self._ai_prediction['direction']}"
                f"({self._ai_prediction['confidence']:.2f})"
            )

        log.info(
            f"[15m] SIGNAL: {direction.upper()} delta={delta:.6f} "
            f"size=${size:.2f} remaining={remaining:.0f}s"
            f"{obs_info}{macro_info}"
        )

        tid = await self.executor.execute_directional_bet(
            market=market,
            direction=direction,
            size_usd=size,
            edge=edge,
            notes=notes,
            fill_quality=fill_quality,
            latency_ms=0.0,
            strategy_name=self.name,
        )

        t_exec = time.monotonic()
        latency_ms = (t_exec - t_detect) * 1000

        if tid:
            self._has_position = True
            self._last_trade_market_id = market_id
            self._last_trade_ts = time.time()
            self._traded_market_ids.add(market_id)

            try:
                self.db.conn.execute(
                    "UPDATE trades SET entry_latency_ms=? WHERE id=?",
                    (round(latency_ms, 1), tid),
                )
                self.db.conn.commit()
            except Exception as e:
                log.warning(f"[15m] Latency DB update failed: {e}")

            if not self.cfg.PAPER_TRADING:
                self._live_trade_count += 1

            log.info(
                f"[15m] Trade #{tid} placed: {direction.upper()} ${size:.2f} "
                f"latency={latency_ms:.0f}ms"
            )

            if self.monitoring:
                self.monitoring.record_opportunity(
                    executed=True,
                    latency_ms=latency_ms,
                    fill_quality=fill_quality,
                )

            ref_price = self.binance.reference_price
            asyncio.create_task(
                self._schedule_resolution(tid, market, direction, remaining, ref_price, size)
            )
        else:
            if self.monitoring:
                self.monitoring.record_opportunity(
                    executed=False,
                    reason="execution_failed",
                )
