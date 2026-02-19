"""
BTC 5-min TEST strategy — rapid signal-based entry.

Independent from BTC5MinFastStrategy. Uses candle + sentiment + aggregator scoring.
strategy field: "btc_5min_test"
"""

import asyncio
import logging
import time
from typing import Optional

from core.polymarket_client import PolymarketClient
from signals import technical as tech
from signals.aggregator import SignalAggregator, SignalResult
from signals.candles import analyze_candles
from signals.sentiment import analyze_sentiment
from strategies.base import BaseStrategy

log = logging.getLogger(__name__)


class BTC5MinTestStrategy(BaseStrategy):
    name = "btc_5min_test"

    def __init__(
        self,
        config,
        polymarket,
        binance,
        db,
        risk,
        executor,
        validator=None,
        monitoring=None,
        predictor=None,
        aggregator: SignalAggregator = None,
        feedback=None,
    ):
        super().__init__(config, polymarket, binance, db, risk, executor)
        self.validator = validator
        self.monitoring = monitoring
        self.predictor = predictor
        self.aggregator = aggregator or SignalAggregator(config)
        self.feedback = feedback

        # Test-specific loosened thresholds
        self.aggregator.score_threshold = getattr(config, "TEST_SIGNAL_SCORE_THRESHOLD", 0.12)
        self.aggregator.min_agreement = getattr(config, "TEST_SIGNAL_MIN_AGREEMENT", 0.50)
        self._max_entry_price = getattr(config, "TEST_MAX_ENTRY_PRICE_5M", 0.85)
        self._cooldown_seconds = getattr(config, "TEST_COOLDOWN_SECONDS", 45)

        self._current_market = None
        self._current_market_id = None
        self._has_position = False
        self._last_trade_market_id = None
        self._scan_count = 0
        self._last_trade_ts = 0.0
        self._traded_market_ids = set()
        self._weight_update_count = 0

        from core.decision_tracker import DecisionTracker
        self._dt = DecisionTracker(DecisionTracker.STEPS_TEST)

    @property
    def interval(self) -> float:
        return self.cfg.LOOP_INTERVAL

    async def scan(self):
        """One iteration of the test mode fast loop."""
        self._scan_count += 1
        self._dt.begin(self._scan_count)

        # 1. Risk check
        ok, reason = self.risk.can_trade()
        if not self._dt.step("risk_check", ok, reason if not ok else "OK"):
            if self._scan_count % 30 == 1:
                log.warning(f"[TEST-5m] Risk blocked: {reason}")
            return

        # 2. Find active BTC 5-min market
        market = await self.poly.find_active_btc_5min_market()
        if not self._dt.step("market_discovery", bool(market), "Bulunamadi" if not market else "OK"):
            if self._scan_count % 15 == 1:
                log.info("[TEST-5m] No active BTC 5-min market found")
            return

        market_id = market.get("id", "")
        end_ts = market.get("end_date_ts", 0)
        now = time.time()
        remaining = end_ts - now

        # Store market info for dashboard
        self._dt.set_market_info(market.get("question", "")[:60], remaining)

        # 3. New market window? Reset state
        if market_id != self._current_market_id:
            self._current_market_id = market_id
            self._current_market = market
            self._has_position = False

            btc_price = self.binance.price
            if btc_price:
                self.binance.set_reference_price(btc_price)
            log.info(
                f"[TEST-5m] New market: {market.get('question', '')[:80]} "
                f"remaining={remaining:.0f}s ref=${btc_price:,.2f}"
            )

        # Update current market for dashboard
        self._current_market = market

        # 3b. COOLDOWN: son trade'den N saniye gecmemisse girme
        if time.time() - self._last_trade_ts < self._cooldown_seconds:
            self._dt.step("preparation", False, f"cooldown {self._cooldown_seconds}s")
            return

        # 3c. Ayni market'e tekrar girme
        if market_id in self._traded_market_ids:
            self._dt.step("preparation", False, "duplicate market")
            return

        # 4. Already have position?
        if self._has_position or market_id == self._last_trade_market_id:
            self._dt.step("preparation", False, "has position")
            return
        self._dt.step("preparation", True, "OK")

        # 5. WARMUP: 30s warmup (remaining > 270)
        max_remaining = getattr(self.cfg, "TEST_MAX_SECONDS_REMAINING_5M", 270)
        if remaining > max_remaining:
            self._dt.step("warmup", False, f"remaining={remaining:.0f}s > {max_remaining}")
            if self._scan_count % 30 == 1:
                log.info(f"[TEST-5m] Warmup: remaining={remaining:.0f}s > {max_remaining}")
            return
        self._dt.step("warmup", True, f"remaining={remaining:.0f}s")

        # 6. Hard floor: son 15s'de asla girme — included in timing step
        if remaining < 15:
            self._dt.step("signals", False, "hard floor <15s")
            return

        # 7. Collect rapid signals
        signals = await self._collect_rapid_signals(market)
        if not self._dt.step("signals", bool(signals), f"{len(signals)} sinyal" if signals else "sinyal yok"):
            return

        # 8. Aggregate
        result = self.aggregator.aggregate(signals)
        if not self._dt.step("aggregate", result.should_trade,
                             f"score={result.composite_score:.4f} agr={result.agreement_ratio:.2f}"):
            if self._scan_count % 15 == 1:
                log.info(
                    f"[TEST-5m] SKIP: {result.action} "
                    f"score={result.composite_score:.4f} "
                    f"agreement={result.agreement_ratio:.2f}"
                )
            return

        direction = result.direction
        if direction == "neutral":
            return

        # 8b. DYNAMIC ENTRY TIMING (sinyal gücüne göre)
        if result.composite_score >= 0.30 and result.agreement_ratio >= 0.70:
            dynamic_min = 15
            strength = "VERY_STRONG"
        elif result.composite_score >= 0.20 and result.agreement_ratio >= 0.60:
            dynamic_min = 25
            strength = "STRONG"
        elif result.composite_score >= 0.15:
            dynamic_min = 35
            strength = "MODERATE"
        else:
            dynamic_min = 45
            strength = "WEAK"

        if remaining < dynamic_min:
            self._dt.step("timing", False,
                          f"{strength} remaining={remaining:.0f}s < min={dynamic_min}s")
            if self._scan_count % 15 == 1:
                log.info(
                    f"[TEST-5m] SKIP: too late for {strength} signal "
                    f"(remaining={remaining:.0f}s < min={dynamic_min}s, "
                    f"score={result.composite_score:.4f})"
                )
            return
        self._dt.step("timing", True,
                       f"{strength} remaining={remaining:.0f}s min={dynamic_min}s")

        # 9. CLOB orderbook price (zorunlu)
        token_id = market.get("up_token" if direction == "up" else "down_token")
        orderbook = None
        if token_id:
            orderbook = await self.poly.get_orderbook(token_id)

        has_book = orderbook and orderbook.get("bids") and orderbook.get("asks")
        if not self._dt.step("orderbook", bool(has_book),
                             "OK" if has_book else "no CLOB orderbook"):
            if self._scan_count % 30 == 1:
                log.info("[TEST-5m] SKIP: no CLOB orderbook")
            return

        # Update market prices from CLOB
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        best_bid = max(float(b["price"]) for b in bids)
        best_ask = min(float(a["price"]) for a in asks)
        midpoint = (best_bid + best_ask) / 2.0
        if direction == "up":
            market["up_price"] = round(midpoint, 4)
            market["down_price"] = round(1.0 - midpoint, 4)
        else:
            market["down_price"] = round(midpoint, 4)
            market["up_price"] = round(1.0 - midpoint, 4)

        price = market.get("up_price" if direction == "up" else "down_price", 0.5)

        # 10. Price cap (test: loosened)
        if not self._dt.step("price_check", price <= self._max_entry_price,
                             f"price={price:.4f}" if price <= self._max_entry_price
                             else f"price={price:.4f} > max={self._max_entry_price}"):
            if self._scan_count % 30 == 1:
                log.info(f"[TEST-5m] SKIP: price too high {price:.4f}")
            return

        # 11. Execute
        size = self.cfg.TRADE_SIZE_USD
        edge = 0.05

        notes = (
            f"[TEST-5m] {result.action} score={result.composite_score:.4f} "
            f"agreement={result.agreement_ratio:.2f} "
            f"remaining={remaining:.0f}s "
            f"signals={len(signals)}"
        )

        log.info(
            f"[TEST-5m] SIGNAL: {direction.upper()} "
            f"score={result.composite_score:.4f} "
            f"agreement={result.agreement_ratio:.2f} "
            f"action={result.action} size=${size:.2f}"
        )

        tid = await self.executor.execute_directional_bet(
            market=market,
            direction=direction,
            size_usd=size,
            edge=edge,
            notes=notes,
            strategy_name=self.name,
        )

        self._dt.step("execution", bool(tid),
                       f"trade #{tid}" if tid else "execution failed")

        if tid:
            self._has_position = True
            self._last_trade_market_id = market_id
            self._last_trade_ts = time.time()
            self._traded_market_ids.add(market_id)

            log.info(f"[TEST-5m] Trade #{tid} placed: {direction.upper()} ${size:.2f}")

            # Record signals for feedback
            if self.feedback:
                signal_records = [
                    {
                        "name": s.name,
                        "direction": s.direction,
                        "score": s.score,
                        "confidence": s.confidence,
                    }
                    for s in signals
                ]
                self.feedback.record_signals(tid, signal_records, direction)

            # Schedule resolution
            ref_price = self.binance.reference_price
            asyncio.create_task(
                self._schedule_resolution(tid, market, direction, remaining, ref_price, size)
            )

            # Periodic weight update check
            self._weight_update_count += 1
            if self._weight_update_count % 10 == 0 and self.feedback:
                self._try_weight_update()

    async def _collect_rapid_signals(self, market: dict) -> list[SignalResult]:
        """Collect all signal sources rapidly."""
        signals = []

        try:
            # a. 1-second candle analysis
            candle_limit = getattr(self.cfg, "CANDLE_1S_LIMIT_5M", 60)
            klines = await self.binance.fetch_klines_1s(limit=candle_limit)
            if klines and len(klines) >= 10:
                candle_result = analyze_candles(klines, window="5m")
                if candle_result:
                    signals.append(SignalResult(
                        name="candle",
                        direction=candle_result["direction"],
                        score=candle_result["score"],
                        confidence=candle_result["trend_strength"],
                    ))

            # b. Polymarket orderbook sentiment
            up_token = market.get("up_token")
            down_token = market.get("down_token")
            if up_token and down_token:
                sent = await analyze_sentiment(self.poly, up_token, down_token)
                if sent:
                    signals.append(SignalResult(
                        name="sentiment",
                        direction=sent["direction"],
                        score=sent["score"],
                        confidence=1.0,
                    ))

            # c. BTC delta signal
            delta = self.binance.get_delta()
            if delta is not None:
                delta_dir = "up" if delta > 0 else "down" if delta < 0 else "neutral"
                # Scale delta to score: 0.001 delta = ~0.5 score
                delta_score = max(-1.0, min(1.0, delta * 500))
                signals.append(SignalResult(
                    name="delta",
                    direction=delta_dir,
                    score=delta_score,
                    confidence=min(1.0, abs(delta) * 2000),
                ))

            # d. Momentum signal
            momentum = self.binance.get_momentum(seconds=60)
            if momentum is not None:
                mom_dir = "up" if momentum > 0 else "down" if momentum < 0 else "neutral"
                mom_score = max(-1.0, min(1.0, momentum * 500))
                signals.append(SignalResult(
                    name="momentum",
                    direction=mom_dir,
                    score=mom_score,
                    confidence=min(1.0, abs(momentum) * 1000),
                ))

            # e. Technical indicators (RSI + MACD + BB)
            prices = self.binance.prices(200)
            if len(prices) >= 30:
                tech_score = self._compute_technical_score(prices)
                if tech_score is not None:
                    tech_dir = "up" if tech_score > 0.05 else "down" if tech_score < -0.05 else "neutral"
                    signals.append(SignalResult(
                        name="technical",
                        direction=tech_dir,
                        score=tech_score,
                        confidence=0.8,
                    ))

        except Exception as e:
            log.warning(f"[TEST-5m] Signal collection error: {e}")

        return signals

    def _compute_technical_score(self, prices: list[float]) -> Optional[float]:
        """RSI + MACD + BB combined score."""
        score = 0.0
        count = 0

        # RSI
        rsi_val = tech.rsi(prices, period=14)
        if rsi_val is not None:
            # RSI < 30 = oversold (bullish), > 70 = overbought (bearish)
            if rsi_val < 30:
                score += 0.5
            elif rsi_val < 40:
                score += 0.2
            elif rsi_val > 70:
                score -= 0.5
            elif rsi_val > 60:
                score -= 0.2
            count += 1

        # MACD
        macd_val = tech.macd(prices, fast=12, slow=26, signal_period=9)
        if macd_val is not None:
            hist = macd_val.get("histogram", 0)
            # Normalize histogram
            avg_price = sum(prices[-20:]) / 20 if len(prices) >= 20 else prices[-1]
            if avg_price > 0:
                norm_hist = hist / avg_price * 10000
                score += max(-0.5, min(0.5, norm_hist))
                count += 1

        # Bollinger Bands
        bb = tech.bollinger_bands(prices, period=20)
        if bb is not None:
            pos = bb.get("position", 0.5)
            # Position < 0.2 = near lower band (bullish), > 0.8 = near upper (bearish)
            bb_score = (0.5 - pos) * 1.0
            score += max(-0.5, min(0.5, bb_score))
            count += 1

        if count == 0:
            return None

        return max(-1.0, min(1.0, score / count))

    def _try_weight_update(self):
        """Try to update signal weights from feedback."""
        if not self.feedback:
            return
        try:
            min_trades = getattr(self.cfg, "FEEDBACK_MIN_TRADES", 20)
            smoothing = getattr(self.cfg, "FEEDBACK_SMOOTHING", 0.3)
            new_weights = self.feedback.suggest_weights(
                current_weights=self.aggregator.weights,
                min_trades=min_trades,
                smoothing=smoothing,
            )
            if new_weights:
                self.aggregator.update_weights(new_weights)
                log.info(f"[TEST-5m] Weights updated: {new_weights}")
        except Exception as e:
            log.warning(f"[TEST-5m] Weight update error: {e}")

    async def _schedule_resolution(
        self,
        trade_id: int,
        market: dict,
        direction: str,
        remaining: float,
        ref_price: Optional[float],
        actual_size: float = 0.0,
    ):
        """Wait for market to resolve, then log the result."""
        wait_time = remaining + 60
        await asyncio.sleep(wait_time)

        try:
            poly_resolved = False
            poly_up_wins = None
            exit_poly_up = None
            exit_poly_down = None

            for attempt in range(3):
                try:
                    fresh = await self.poly.get_market_by_id(market.get("id"))
                    if fresh:
                        exit_poly_up = fresh.get("up_price", 0.5)
                        exit_poly_down = fresh.get("down_price", 0.5)
                        if exit_poly_up >= 0.90:
                            poly_resolved = True
                            poly_up_wins = True
                            break
                        elif exit_poly_down >= 0.90:
                            poly_resolved = True
                            poly_up_wins = False
                            break
                except Exception:
                    pass
                if attempt < 2:
                    await asyncio.sleep(20)

            bet_correct = None
            resolution_source = "unknown"

            if poly_resolved:
                resolution_source = "polymarket"
                if poly_up_wins:
                    bet_correct = (direction == "up")
                else:
                    bet_correct = (direction == "down")
            else:
                resolution_source = "btc_fallback"
                current_price = self.binance.price
                if current_price is not None and ref_price is not None and ref_price > 0:
                    btc_went_up = current_price >= ref_price
                    bet_correct = (
                        (direction == "up" and btc_went_up)
                        or (direction == "down" and not btc_went_up)
                    )

            if bet_correct is not None:
                trade_row = self.db.get_trade(trade_id)
                if trade_row:
                    actual_cost = trade_row["cost"]
                    entry_price = trade_row["price"]
                else:
                    actual_cost = actual_size
                    entry_price = market.get(
                        "up_price" if direction == "up" else "down_price", 0.5
                    )

                if bet_correct:
                    pnl = actual_cost * ((1.0 / entry_price) - 1)
                else:
                    pnl = -actual_cost

                notes_text = (
                    f"resolved[{resolution_source}] correct={bet_correct} "
                    f"cost=${actual_cost:.2f}"
                )

                self.db.resolve_trade(
                    trade_id=trade_id,
                    pnl=round(pnl, 2),
                    exit_reason="market_resolved",
                    notes_append=notes_text,
                )
                log.info(
                    f"[TEST-5m] Trade #{trade_id} resolved[{resolution_source}]: "
                    f"{'WIN' if bet_correct else 'LOSS'} pnl=${pnl:+.2f}"
                )

                # Record outcome for feedback
                if self.feedback:
                    self.feedback.record_outcome(trade_id, bet_correct, round(pnl, 2))

                if self.monitoring:
                    self.monitoring.record_pnl(pnl)
            else:
                self.db.resolve_trade(
                    trade_id=trade_id,
                    pnl=0.0,
                    exit_reason="resolution_unknown",
                    notes_append="no outcome available",
                )
        except Exception as e:
            log.error(f"[TEST-5m] Resolution error for trade #{trade_id}: {e}")
