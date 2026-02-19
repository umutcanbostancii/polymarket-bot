"""
BTC 5-min fast loop strategy for Polymarket "Bitcoin Up or Down" markets.

Core idea: detect BTC price movement on Binance before Polymarket odds update,
then place a directional bet on the correct side.

PDF uyumlu: AI prediction, execution validation, VWAP, monitoring entegrasyonu.
"""

import asyncio
import logging
import time
from typing import Optional

from core.polymarket_client import PolymarketClient
from core.trade_analysis import analyzer
from signals import technical as tech
from strategies.base import BaseStrategy

log = logging.getLogger(__name__)


class BTC5MinFastStrategy(BaseStrategy):
    name = "btc_5min_fast"

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
    ):
        super().__init__(config, polymarket, binance, db, risk, executor)
        self.validator = validator
        self.monitoring = monitoring
        self.predictor = predictor

        self._current_market = None
        self._current_market_id = None
        self._has_position = False
        self._last_trade_market_id = None
        self._scan_count = 0
        self._ai_prediction = None
        self._live_trade_count = 0
        self._last_trade_ts = 0.0          # son trade zamani (cooldown)
        self._traded_market_ids = set()     # bu session'da trade acilan market id'ler

        # Observation phase state
        self._obs_prices = []       # [(ts, btc_price)]
        self._obs_poly = []         # [(ts, up_price, down_price)]
        self._obs_complete = False
        self._obs_result = None

        from core.decision_tracker import DecisionTracker
        self._dt = DecisionTracker(DecisionTracker.STEPS_MAIN)

    @property
    def interval(self) -> float:
        return self.cfg.LOOP_INTERVAL

    async def scan(self):
        """One iteration of the fast loop."""
        self._scan_count += 1
        self._dt.begin(self._scan_count)

        # 0. Live trade limit check
        max_live = getattr(self.cfg, "MAX_LIVE_TEST_TRADES", 0)
        if max_live > 0 and not self.cfg.PAPER_TRADING:
            if self._live_trade_count >= max_live:
                if self._scan_count % 30 == 1:
                    log.info(f"Live test limit reached ({self._live_trade_count}/{max_live})")
                return

        # 1. Risk check
        ok, reason = self.risk.can_trade()
        if not self._dt.step("risk_check", ok, reason if not ok else "OK"):
            if self._scan_count % 30 == 1:  # log every ~60s
                log.warning(f"Risk blocked: {reason}")
            return

        # 2. Find active BTC 5-min market
        market = await self.poly.find_active_btc_5min_market()
        if not self._dt.step("market_discovery", bool(market), "found" if market else "no active market"):
            if self._scan_count % 15 == 1:
                log.info("No active BTC 5-min market found")
            return

        market_id = market.get("id", "")
        end_ts = market.get("end_date_ts", 0)
        now = time.time()
        remaining = end_ts - now
        self._dt.set_market_info(market.get("question", "")[:60], remaining)

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
                f"New market: {market.get('question', '')[:80]} "
                f"remaining={remaining:.0f}s ref=${btc_price:,.2f}"
            )

        # 3b. COOLDOWN: son trade'den 60s gecmemisse girme
        if time.time() - self._last_trade_ts < 60:
            self._dt.step("preparation", False, f"cooldown ({60 - (time.time() - self._last_trade_ts):.0f}s left)")
            if self._scan_count % 30 == 1:
                log.info(f"SKIP: cooldown active ({60 - (time.time() - self._last_trade_ts):.0f}s left)")
            return

        # 3c. Ayni market_id'ye tekrar girme
        if market_id in self._traded_market_ids:
            self._dt.step("preparation", False, f"already traded {market_id[:12]}")
            if self._scan_count % 30 == 1:
                log.info(f"SKIP: already traded this market {market_id[:12]}")
            return

        # 4. Already have a position in this market? - Track prices anyway
        if self._has_position or market_id == self._last_trade_market_id:
            self._dt.step("preparation", False, "has_position")
            await self._track_live_prices(market)
            return

        self._dt.step("preparation", True, "OK")

        # 5. OBSERVATION PHASE (remaining > MAX_SECONDS_REMAINING = ilk 120s)
        if remaining > self.cfg.MAX_SECONDS_REMAINING:
            self._collect_observation(market)
            self._dt.step("observation", False, f"collecting samples={len(self._obs_prices)}")
            if self._scan_count % 30 == 1:
                log.info(
                    f"Observation: collecting... samples={len(self._obs_prices)} "
                    f"remaining={remaining:.0f}s"
                )
            return

        # 6. Observation complete? (ilk kez remaining <= MAX_SECONDS_REMAINING)
        if not self._obs_complete:
            self._obs_result = self._analyze_observation()
            self._obs_complete = True
            if self._obs_result:
                log.info(
                    f"Observation complete: direction={self._obs_result['direction']} "
                    f"trend_pct={self._obs_result['trend_pct']:.6f} "
                    f"consistency={self._obs_result['consistency']:.2f} "
                    f"volatility={self._obs_result['volatility']:.6f} "
                    f"poly_drift={self._obs_result['poly_drift']:.4f} "
                    f"samples={self._obs_result['sample_count']}"
                )
            else:
                log.info("Observation complete: insufficient data for analysis")

            # AI prediction (gözlem verisiyle birlikte)
            if self.predictor:
                try:
                    ai_data = await self._gather_ai_data()
                    if ai_data:
                        self._ai_prediction = await self.predictor.predict(ai_data)
                        if self._ai_prediction:
                            log.info(
                                f"AI prediction: {self._ai_prediction['direction'].upper()} "
                                f"conf={self._ai_prediction['confidence']:.2f} "
                                f"reason={self._ai_prediction.get('reasoning', '')[:60]}"
                            )
                except Exception as e:
                    log.warning(f"AI prediction failed: {e}")

        # 7. Hard floor: son 15s'de asla girme (order fill süresi yok)
        if remaining < 15:
            return

        self._dt.step("observation", True, f"complete samples={len(self._obs_prices)}")

        # 8. Get BTC price data
        delta = self.binance.get_delta()
        if delta is None:
            return

        momentum = self.binance.get_momentum(seconds=30)
        vol = self.binance.get_volatility(seconds=300)

        # 9. NOISE FILTER: delta çok küçükse gürültü
        if not self._dt.step("delta_filter", abs(delta) >= self.cfg.MIN_DELTA_FOR_ENTRY,
                             f"|{delta:.6f}| vs min={self.cfg.MIN_DELTA_FOR_ENTRY}"):
            if self._scan_count % 15 == 1:
                log.info(f"SKIP: delta too small |{delta:.6f}| < {self.cfg.MIN_DELTA_FOR_ENTRY}")
            return

        # 9b. MOMENTUM CAP: cok yuksek momentum = trend uzamis, mean reversion riski
        _mom_ok = momentum is None or abs(momentum) <= self.cfg.MAX_MOMENTUM_FOR_ENTRY
        if not self._dt.step("momentum_cap", _mom_ok,
                             f"|{momentum:.6f}| vs max={self.cfg.MAX_MOMENTUM_FOR_ENTRY}" if momentum is not None else "no momentum"):
            log.info(f"SKIP: momentum too high |{momentum:.6f}| > {self.cfg.MAX_MOMENTUM_FOR_ENTRY}")
            return

        # 9c. CANDLE SIGNAL: 1-saniye mum analizi
        candle_result = await self._get_candle_signal()
        if candle_result:
            self._dt.step(
                "candle_signal", True,
                f"{candle_result['direction'].upper()} score={candle_result['score']:.4f} pattern={candle_result['pattern']}"
            )
        else:
            self._dt.step("candle_signal", True, "N/A (veri yok)")

        # 10. Determine direction — SKOR TABANLI yon belirleme
        dir_score = 0.0
        dir_details = []

        # Delta sinyali (agirlik: 1.0)
        if delta > 0:
            dir_score += 1.0
            dir_details.append(f"delta=+1.0")
        elif delta < 0:
            dir_score -= 1.0
            dir_details.append(f"delta=-1.0")

        # Momentum sinyali (agirlik: 0.5)
        if momentum is not None:
            if momentum > 0:
                dir_score += 0.5
                dir_details.append(f"mom=+0.5")
            elif momentum < 0:
                dir_score -= 0.5
                dir_details.append(f"mom=-0.5")

        # Gozlem fazi sinyali (agirlik: consistency degeri)
        if self._obs_result:
            obs_weight = self._obs_result["consistency"]
            if self._obs_result["direction"] == "up":
                dir_score += obs_weight
            else:
                dir_score -= obs_weight
            dir_details.append(f"obs={'+' if self._obs_result['direction'] == 'up' else '-'}{obs_weight:.2f}")

        # Mum analizi sinyali (agirlik: CANDLE_SIGNAL_WEIGHT)
        candle_weight = getattr(self.cfg, "CANDLE_SIGNAL_WEIGHT", 0.6)
        if candle_result:
            dir_score += candle_result["score"] * candle_weight
            dir_details.append(f"candle={candle_result['score'] * candle_weight:+.2f}")

        # Yon: esik (en az 2 kaynak uyumlu olmali)
        min_dir_score = getattr(self.cfg, "MIN_DIRECTION_SCORE", 0.5)
        if dir_score > min_dir_score:
            direction = "up"
        elif dir_score < -min_dir_score:
            direction = "down"
        else:
            detail = f"score={dir_score:+.2f} thr={min_dir_score} [{' '.join(dir_details)}]"
            self._dt.step("direction", False, f"belirsiz {detail}")
            if self._scan_count % 15 == 1:
                log.info(f"SKIP: direction uncertain {detail}")
            return
        self._dt.step("direction", True, f"{direction.upper()} score={dir_score:+.2f} [{' '.join(dir_details)}]")

        # 11. MACRO TREND FILTER: 5dk + 10dk BTC trendi
        macro_5m = self.binance.get_momentum(seconds=300)
        macro_10m = self.binance.get_momentum(seconds=600)
        macro = None
        if macro_5m is not None:
            if macro_5m > 0.0001 and (macro_10m is None or macro_10m > 0):
                macro = "up"
            elif macro_5m < -0.0001 and (macro_10m is None or macro_10m <= 0):
                macro = "down"

        if macro and macro != direction:
            macro_strength = abs(macro_5m) if macro_5m else 0
            if macro_strength > 0.001:
                self._dt.step("trend", False, f"{direction.upper()} vs macro {macro.upper()} (5m={macro_5m:.6f}) GUCLU TERS")
                log.info(f"SKIP: {direction.upper()} vs STRONG macro trend {macro.upper()} (5m={macro_5m:.6f})")
                return
            # Zayif ters trend: devam et, edge'de ceza olarak yansiyacak
            log.info(f"WARN: {direction.upper()} vs weak macro {macro.upper()} (5m={macro_5m:.6f}) — devam")

        # 12. OBSERVATION TREND FILTER — skor sistemine entegre, ayri engelleme yok
        # Observation zaten dir_score'a dahil edildi; sadece loglama
        if self._obs_result:
            obs_dir = self._obs_result["direction"]
            obs_consistency = self._obs_result["consistency"]
            if obs_dir != direction and obs_consistency >= self.cfg.OBSERVATION_CONSISTENCY_MIN:
                # Gozlem net ters yonde VE yuksek consistency: uyari (ama engelleme skor'a bagli)
                log.info(f"WARN: obs {obs_dir.upper()} (c={obs_consistency:.2f}) vs direction {direction.upper()}")

        self._dt.step("trend", True, f"macro={macro or 'neutral'} obs={'aligned' if self._obs_result else 'N/A'}")

        # ── 12b. DYNAMIC ENTRY TIMING ─────────────────────────────
        # Sinyal gücüne göre giriş zamanı kararı:
        #   Çok güçlü → 60s kala bile girebilir (yön net, fiyat henüz makul)
        #   Güçlü     → 45s kala girebilir
        #   Orta      → 30s kala bekle (daha fazla onay gerekli)
        #   Zayıf     → 45s kala dur (risk yüksek)
        signal_strength = self._calculate_signal_strength(
            delta=delta,
            momentum=momentum,
            direction=direction,
            macro=macro,
        )
        min_signal = getattr(self.cfg, "MIN_SIGNAL_STRENGTH_5M", 0.0)
        if signal_strength < min_signal:
            self._dt.step("signal_strength", False, f"score={signal_strength:.2f} < min={min_signal:.2f}")
            if self._scan_count % 15 == 1:
                log.info(
                    f"SKIP: weak signal score={signal_strength:.2f} < min={min_signal:.2f}"
                )
            return

        if signal_strength >= 0.70:
            dynamic_min_seconds = 15   # Çok güçlü: son 15s'e kadar girebilir
            strength_label = "VERY_STRONG"
        elif signal_strength >= 0.50:
            dynamic_min_seconds = 25   # Güçlü: son 25s'e kadar
            strength_label = "STRONG"
        elif signal_strength >= 0.30:
            dynamic_min_seconds = 35   # Orta: son 35s'e kadar
            strength_label = "MODERATE"
        else:
            dynamic_min_seconds = 45   # Zayıf: son 45s'de girme
            strength_label = "WEAK"

        if remaining < dynamic_min_seconds:
            self._dt.step("signal_strength", False,
                          f"{strength_label} too late remaining={remaining:.0f}s < {dynamic_min_seconds}s score={signal_strength:.2f}")
            if self._scan_count % 15 == 1:
                log.info(
                    f"SKIP: too late for {strength_label} signal "
                    f"(remaining={remaining:.0f}s < min={dynamic_min_seconds}s, "
                    f"score={signal_strength:.2f})"
                )
            return

        self._dt.step("signal_strength", True, f"{strength_label} score={signal_strength:.2f} remaining={remaining:.0f}s")

        log.info(
            f"TIMING OK: {strength_label} signal score={signal_strength:.2f} "
            f"remaining={remaining:.0f}s (min={dynamic_min_seconds}s)"
        )

        # 13. FRESH PRICE FETCH
        fresh_market = await self.poly.get_market_by_id(market.get("id"))
        if fresh_market:
            market = fresh_market
            log.info(f"FRESH PRICES: UP={market.get('up_price')} DOWN={market.get('down_price')}")

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
                log.info(f"CLOB LIVE PRICE: UP={market['up_price']} DOWN={market['down_price']}")

        # 14b. CLOB fiyat zorunlulugu — Gamma API bu marketlerde yanlis fiyat veriyor
        _ob_ok = orderbook and orderbook.get("bids") and orderbook.get("asks")
        if not self._dt.step("orderbook", bool(_ob_ok),
                             f"bids={len(orderbook.get('bids', []))} asks={len(orderbook.get('asks', []))}" if orderbook else "no orderbook"):
            log.info(f"SKIP: no CLOB orderbook — remaining={remaining:.0f}s token={token_id[:12] if token_id else 'None'}")
            return

        price = market.get("up_price" if direction == "up" else "down_price", 0.5)

        edge = self.calculate_edge_v2(
            delta_pct=delta,
            momentum=momentum,
            volatility=vol,
            seconds_remaining=remaining,
            market=market,
            direction=direction,
            ob_imbalance=(ob_metrics or {}).get("imbalance"),
        )
        if edge is None:
            self._dt.step("edge_calc", False, "edge=None")
            return

        if not self._dt.step("edge_calc", edge >= self.cfg.EDGE_THRESHOLD,
                             f"edge={edge:.4f} vs threshold={self.cfg.EDGE_THRESHOLD:.4f}"):
            if self._scan_count % 15 == 1:
                log.info(f"SKIP: low edge {edge:.4f} < {self.cfg.EDGE_THRESHOLD:.4f}")
            return

        # 15. Position sizing
        if getattr(self.cfg, "RISK_POSITION_SIZING_ENABLED", True):
            depth_usd = (ob_metrics or {}).get("ask_depth_usd", 0.0)
            size = self.risk.position_size(edge=edge, price=price, orderbook_depth_usd=depth_usd)
            if size <= 0:
                if self._scan_count % 15 == 1:
                    log.info("SKIP: position_size=0 (risk sizing)")
                return
        else:
            size = self.cfg.TRADE_SIZE_USD

        if signal_strength >= 0.70:
            size *= 1.25
        elif signal_strength < 0.40:
            size *= 0.75
        size = round(min(size, self.cfg.TRADE_SIZE_USD), 2)
        if size <= 0:
            return

        # Executor min 5 share uyguluyor; validation/execution ayni notional uzerinden ilerlesin
        if price > 0:
            import math
            shares = math.ceil(size / price * 100) / 100
            if shares < 5:
                size = round(5.0 * price, 2)

        # 15b. PRICE CAP: cok pahali giris = kotu risk/odul orani
        if price > self.cfg.MAX_ENTRY_PRICE:
            self._dt.step("price_risk", False, f"price={price:.4f} > max={self.cfg.MAX_ENTRY_PRICE}")
            log.info(f"SKIP: entry price too high {price:.4f} > {self.cfg.MAX_ENTRY_PRICE}")
            return

        # 15c. Reward/Risk gate: tek kaybin mini karlari silmesini azalt
        reward_risk = ((1.0 - price) / price) if price > 0 else 0.0
        min_rr = getattr(self.cfg, "MIN_REWARD_RISK_RATIO_5M", 0.0)
        if reward_risk < min_rr:
            self._dt.step("price_risk", False, f"R/R={reward_risk:.3f} < min={min_rr:.3f}")
            log.info(f"SKIP: reward/risk too low {reward_risk:.3f} < {min_rr:.3f}")
            return

        self._dt.step("price_risk", True, f"price={price:.4f} R/R={reward_risk:.3f}")

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
                bypass = self.cfg.PAPER_TRADING and getattr(
                    self.cfg, "ALLOW_PAPER_VALIDATION_BYPASS", False
                )
                if bypass:
                    self._dt.step("validation", True, f"bypassed (paper): {reject_reason}")
                    log.debug(f"Validation bypassed (paper mode): {reject_reason}")
                else:
                    self._dt.step("validation", False, reject_reason)
                    log.info(f"Validation REJECTED: {reject_reason}")
                    if self.monitoring:
                        self.monitoring.record_opportunity(
                            executed=False,
                            reason=reject_reason,
                        )
                    return
            else:
                self._dt.step("validation", True, f"fq={fill_quality:.2f} slip={validation['slippage_pct']:.4f}")
                log.info(
                    f"Validation PASSED: net=${validation['net_profit_usd']:.4f} "
                    f"slip={validation['slippage_pct']:.4f} "
                    f"fq={fill_quality:.2f} "
                    f"liq=${validation['available_liquidity_usd']:.2f}"
                )
        else:
            self._dt.step("validation", True, "no validator")

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
            f"{obs_info}{macro_info}"
        )
        if self._ai_prediction:
            notes += (
                f" ai={self._ai_prediction['direction']}"
                f"({self._ai_prediction['confidence']:.2f})"
            )

        log.info(
            f"SIGNAL: {direction.upper()} edge={edge:.4f} delta={delta:.6f} "
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
        )

        t_exec = time.monotonic()
        latency_ms = (t_exec - t_detect) * 1000

        if tid:
            self._dt.step("execution", True, f"Trade #{tid}")
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
                log.warning(f"Latency DB update failed: {e}")

            if not self.cfg.PAPER_TRADING:
                self._live_trade_count += 1
                log.info(
                    f"LIVE Trade #{tid} placed: {direction.upper()} ${size:.2f} "
                    f"latency={latency_ms:.0f}ms "
                    f"[{self._live_trade_count}/{getattr(self.cfg, 'MAX_LIVE_TEST_TRADES', '∞')}]"
                )
            else:
                log.info(
                    f"Trade #{tid} placed: {direction.upper()} ${size:.2f} "
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
            self._dt.step("execution", False, "execution_failed")
            if self.monitoring:
                self.monitoring.record_opportunity(
                    executed=False,
                    reason="execution_failed",
                )

    def calculate_edge(
        self,
        delta_pct: float,
        momentum: Optional[float],
        volatility: Optional[float],
        seconds_remaining: float,
        market: dict,
        direction: str,
    ) -> Optional[float]:
        """
        Calculate base net edge for a directional bet.
        p_real = estimated true probability that BTC goes in `direction`
        p_market = Polymarket implied probability (from odds)
        edge = p_real - p_market - fees - slippage - buffer
        """
        # Time factor: closer to expiry = more certain about direction
        time_factor = 1.0 - (seconds_remaining / 300.0)
        time_factor = max(0.0, min(time_factor, 1.0))

        # Momentum strength
        abs_delta = abs(delta_pct)
        momentum_strength = abs_delta * (1 + time_factor)

        # Reversal risk: higher volatility + more time = more reversal risk
        reversal_risk = 0.0
        if volatility is not None:
            reversal_risk = volatility * (seconds_remaining / 300.0)

        # Estimated true probability
        p_real = (
            0.50
            + momentum_strength * self.cfg.MOMENTUM_WEIGHT
            - reversal_risk * self.cfg.REVERSAL_RISK_WEIGHT
        )
        p_real = max(0.30, min(p_real, 0.95))

        # Market implied probability
        if direction == "up":
            pm_implied = market.get("up_price", 0.5)
        else:
            pm_implied = market.get("down_price", 0.5)

        if pm_implied <= 0 or pm_implied >= 1:
            return None

        # Dynamic fee
        fee = PolymarketClient.calculate_dynamic_fee(pm_implied)

        # Net edge
        edge = p_real - pm_implied - fee - self.cfg.SLIPPAGE_ESTIMATE - self.cfg.FEE_BUFFER

        return edge

    def _calculate_signal_strength(
        self,
        delta: float,
        momentum: Optional[float],
        direction: str,
        macro: Optional[str],
    ) -> float:
        """
        Sinyal gücü skoru (0.0 - 1.0).
        Birden fazla faktöre bakarak giriş zamanlaması kararı verir.

        Faktörler:
        - Observation consistency (gözlem trendi ne kadar tutarlı)
        - Delta büyüklüğü (BTC fiyat hareketi gücü)
        - Momentum uyumu (kısa vadeli ivme yönle aynı mı)
        - Macro uyum (5dk/10dk trend yönle aynı mı)
        """
        score = 0.0

        # 1. Observation consistency (max 0.35)
        if self._obs_result:
            obs_c = self._obs_result["consistency"]
            obs_trend = abs(self._obs_result["trend_pct"])
            # Consistency katkısı: 0.55 → 0.10, 0.65 → 0.20, 0.75+ → 0.30
            if obs_c >= 0.70:
                score += 0.30
            elif obs_c >= 0.60:
                score += 0.20
            elif obs_c >= 0.55:
                score += 0.10
            # Trend büyüklüğü bonusu
            if obs_trend > 0.001:
                score += 0.05

        # 2. Delta büyüklüğü (max 0.30)
        abs_delta = abs(delta)
        if abs_delta >= 0.002:
            score += 0.30    # Çok güçlü hareket
        elif abs_delta >= 0.001:
            score += 0.20    # Güçlü hareket
        elif abs_delta >= 0.0005:
            score += 0.10    # Orta hareket

        # 3. Momentum uyumu (max 0.20)
        if momentum is not None:
            mom_aligned = (
                (direction == "up" and momentum > 0)
                or (direction == "down" and momentum < 0)
            )
            if mom_aligned:
                abs_mom = abs(momentum)
                if abs_mom > 0.0003:
                    score += 0.20  # Güçlü momentum uyumu
                elif abs_mom > 0.0001:
                    score += 0.10  # Hafif momentum uyumu

        # 4. Macro trend uyumu (max 0.15)
        if macro == direction:
            score += 0.15
        elif macro is None:
            score += 0.05  # Nötr macro = zararsız

        return min(score, 1.0)

    def calculate_edge_v2(
        self,
        delta_pct: float,
        momentum: Optional[float],
        volatility: Optional[float],
        seconds_remaining: float,
        market: dict,
        direction: str,
        ob_imbalance: Optional[float] = None,
    ) -> Optional[float]:
        """Edge v2: base edge + AI boost + orderbook imbalance boost."""
        base_edge = self.calculate_edge(
            delta_pct=delta_pct,
            momentum=momentum,
            volatility=volatility,
            seconds_remaining=seconds_remaining,
            market=market,
            direction=direction,
        )
        if base_edge is None:
            return None

        # AI boost: AI tahmin yonu ile uyumluysa edge artir
        ai_boost = 0.0
        if (
            self._ai_prediction
            and self._ai_prediction["direction"] == direction
            and self._ai_prediction["confidence"] >= getattr(self.cfg, "AI_CONFIDENCE_THRESHOLD", 0.55)
        ):
            ai_boost = self._ai_prediction["confidence"] * getattr(self.cfg, "AI_BOOST_WEIGHT", 0.15)

        # Orderbook imbalance boost
        ob_boost = 0.0
        if ob_imbalance is not None:
            if (direction == "up" and ob_imbalance > 0.2) or \
               (direction == "down" and ob_imbalance < -0.2):
                ob_boost = abs(ob_imbalance) * 0.02

        return base_edge + ai_boost + ob_boost

    async def _get_candle_signal(self) -> dict | None:
        """1-saniye mumlarindan trend analizi yap."""
        try:
            limit = getattr(self.cfg, "CANDLE_1S_LIMIT_5M", 60)
            klines = await self.binance.fetch_klines_1s(limit=limit)
            if not klines or len(klines) < 10:
                return None
            from signals.candles import analyze_candles
            return analyze_candles(klines, window="5m")
        except Exception as e:
            log.debug(f"Candle signal failed: {e}")
            return None

    def _collect_observation(self, market: dict):
        """Gözlem fazında BTC ve Polymarket fiyatlarını kaydet."""
        now = time.time()
        btc_price = self.binance.price
        if btc_price:
            self._obs_prices.append((now, btc_price))

        up_p = market.get("up_price", 0)
        down_p = market.get("down_price", 0)
        if up_p > 0 and down_p > 0:
            self._obs_poly.append((now, up_p, down_p))

    def _analyze_observation(self) -> dict | None:
        """120s gözlem verisini analiz et. Trend yönü, tutarlılık, volatilite."""
        if len(self._obs_prices) < 5:
            return None

        prices = [p for _, p in self._obs_prices]
        first_price = prices[0]
        last_price = prices[-1]

        if first_price == 0:
            return None

        # Trend yönü ve büyüklüğü
        trend_pct = (last_price - first_price) / first_price

        if trend_pct > 0:
            direction = "up"
        elif trend_pct < 0:
            direction = "down"
        else:
            direction = "up"  # nötr durumda varsayılan

        # Tutarlılık: fiyat kaç kez trend yönünde hareket etti
        consistent_moves = 0
        total_moves = 0
        for i in range(1, len(prices)):
            diff = prices[i] - prices[i - 1]
            if diff == 0:
                continue
            total_moves += 1
            if (direction == "up" and diff > 0) or (direction == "down" and diff < 0):
                consistent_moves += 1

        consistency = consistent_moves / total_moves if total_moves > 0 else 0.0

        # Volatilite: tick-to-tick return std dev
        returns = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0:
                returns.append((prices[i] - prices[i - 1]) / prices[i - 1])

        if returns:
            mean_r = sum(returns) / len(returns)
            var_r = sum((r - mean_r) ** 2 for r in returns) / len(returns)
            volatility = var_r ** 0.5
        else:
            volatility = 0.0

        # Polymarket drift: UP fiyatının gözlem boyunca değişimi
        poly_drift = 0.0
        if len(self._obs_poly) >= 2:
            first_up = self._obs_poly[0][1]
            last_up = self._obs_poly[-1][1]
            poly_drift = last_up - first_up

        return {
            "direction": direction,
            "trend_pct": trend_pct,
            "consistency": consistency,
            "volatility": volatility,
            "poly_drift": poly_drift,
            "sample_count": len(self._obs_prices),
        }

    async def _track_live_prices(self, market: dict):
        """Pozisyon açıkken canlı Polymarket fiyatlarını takip et.
        market zaten find_active_btc_5min_market()'ten geliyor (3s TTL ile taze).
        Ekstra API call yapmaya gerek yok - sadece _current_market'i guncelle."""
        self._current_market = market
        log.debug(
            f"LIVE PRICES: UP={market.get('up_price'):.4f} "
            f"DOWN={market.get('down_price'):.4f}"
        )

    async def _gather_ai_data(self) -> Optional[dict]:
        """AI prediction icin gerekli verileri topla."""
        try:
            klines = await self.binance.fetch_klines(interval="1m", limit=200)
            if not klines or len(klines) < 30:
                return None

            depth = await self.binance.fetch_depth(limit=20)

            closes = [k["close"] for k in klines]
            volumes = [k["volume"] for k in klines]

            # Price changes
            current = closes[-1]
            pc = {}
            if len(closes) >= 60:
                pc["1h"] = round((current - closes[-60]) / closes[-60] * 100, 3)
            if len(closes) >= 200:
                pc["4h"] = round((current - closes[-200]) / closes[-200] * 100, 3)

            # Technical indicators
            rsi_val = tech.rsi(closes, period=14)
            macd_val = tech.macd(closes, fast=12, slow=26, signal_period=9)
            bb = tech.bollinger_bands(closes, period=20)
            vol_trend = tech.volume_trend(volumes, short=5, long=20)

            # Orderbook imbalance from Binance depth
            obi = None
            if depth:
                obi = tech.orderbook_imbalance(depth.get("bids", []), depth.get("asks", []))

            # Polymarket spread
            poly_spread = None
            if self._current_market:
                up_p = self._current_market.get("up_price", 0)
                down_p = self._current_market.get("down_price", 0)
                if up_p > 0 and down_p > 0:
                    poly_spread = abs(1.0 - up_p - down_p)

            return {
                "klines_30": klines[-30:],
                "price_changes": pc,
                "rsi": rsi_val,
                "macd": macd_val,
                "bollinger_position": bb["position"] if bb else None,
                "volume_trend": vol_trend,
                "ob_imbalance": obi,
                "poly_spread": poly_spread,
            }
        except Exception as e:
            log.warning(f"Failed to gather AI data: {e}")
            return None

    async def _schedule_resolution(
        self,
        trade_id: int,
        market: dict,
        direction: str,
        remaining: float,
        ref_price: Optional[float],
        actual_size: float = 0.0,
    ):
        """Wait for market to resolve, then log the result.
        PRIMARY: Polymarket outcome (up_price/down_price → 0 or 1 after resolve).
        FALLBACK: BTC price comparison (only if Polymarket outcome ambiguous).
        """
        # --- Profit Taking Monitor ---
        if self.cfg.EXIT_PROFIT_TAKE_ENABLED and remaining > self.cfg.EXIT_MIN_TIME_BEFORE_END:
            # Get token ID and entry price from DB
            trade_row = self.db.get_trade(trade_id)
            if trade_row:
                entry_price = trade_row["price"]
                token_id = None
                if direction == "up":
                    token_id = market.get("up_token")
                else:
                    token_id = market.get("down_token")
                
                if token_id and entry_price > 0:
                    log.info(f"Profit monitor started for trade #{trade_id} (entry={entry_price:.4f}, remaining={remaining:.0f}s)")
                    
                    # Calculate target price for profit taking
                    target_price = entry_price * (1 + self.cfg.EXIT_PROFIT_TARGET)
                    
                    # Monitor until market ends or profit taken
                    while remaining > self.cfg.EXIT_MIN_TIME_BEFORE_END:
                        await asyncio.sleep(self.cfg.EXIT_CHECK_INTERVAL)
                        remaining -= self.cfg.EXIT_CHECK_INTERVAL
                        
                        try:
                            # Get current best bid from CLOB orderbook (more accurate than Gamma API)
                            current_price = None
                            orderbook = await self.poly.get_orderbook(token_id)
                            if orderbook:
                                bids = orderbook.get("bids", [])
                                if bids:
                                    current_price = max(float(b["price"]) for b in bids)
                                    log.debug(
                                        f"Profit monitor #{trade_id}: best_bid={current_price:.4f} "
                                        f"entry={entry_price:.4f}"
                                    )

                            # Fallback: Gamma API if no orderbook
                            if current_price is None:
                                fresh_market = await self.poly.get_market_by_id(market.get("id"))
                                if fresh_market:
                                    current_price = fresh_market.get(
                                        "up_price" if direction == "up" else "down_price", 0.5
                                    )

                            if current_price is not None:
                                # Calculate unrealized PnL
                                unrealized_pnl = (current_price - entry_price) / entry_price

                                # Check if profit target reached
                                if unrealized_pnl >= self.cfg.EXIT_PROFIT_TARGET:
                                    log.info(f"PROFIT TAKE triggered for trade #{trade_id}: "
                                             f"entry={entry_price:.4f}, current={current_price:.4f}, "
                                             f"PnL={unrealized_pnl*100:.1f}%")
                                    
                                    # Try to sell
                                    success = await self.executor.sell_position(
                                        token_id=token_id,
                                        shares=trade_row["size"],
                                        price=current_price,
                                        trade_id=trade_id,
                                        strategy_name=self.name
                                    )
                                    
                                    if success:
                                        log.info(f"Trade #{trade_id} sold successfully via profit taking")
                                        return  # Early exit, trade already closed
                                    else:
                                        log.warning(f"Profit taking sell failed for trade #{trade_id}, falling back to resolution")
                                        break  # Exit monitoring, wait for resolution
                        except Exception as e:
                            log.warning(f"Profit monitoring error: {e}")
                            # Continue monitoring despite error
                        
                        # Update remaining time
                        if remaining <= self.cfg.EXIT_MIN_TIME_BEFORE_END:
                            break
        
        # --- Original Resolution Logic ---
        # Wait until market ends + 60s buffer for Polymarket to settle
        wait_time = remaining + 60
        await asyncio.sleep(wait_time)

        try:
            # --- PRIMARY: Polymarket outcome resolution ---
            poly_resolved = False
            poly_up_wins = None
            exit_poly_up = None
            exit_poly_down = None

            # Retry up to 3 times (market may take time to resolve)
            for attempt in range(3):
                try:
                    fresh = await self.poly.get_market_by_id(market.get("id"))
                    if fresh:
                        exit_poly_up = fresh.get("up_price", 0.5)
                        exit_poly_down = fresh.get("down_price", 0.5)
                        # Resolved market: prices converge to 0 or 1
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

            # Determine outcome
            bet_correct = None
            resolution_source = "unknown"

            if poly_resolved:
                # Polymarket outcome — definitive
                resolution_source = "polymarket"
                if poly_up_wins:
                    bet_correct = (direction == "up")
                else:
                    bet_correct = (direction == "down")
            else:
                # FALLBACK: BTC price comparison
                resolution_source = "btc_fallback"
                current_price = self.binance.price
                if current_price is not None and ref_price is not None and ref_price > 0:
                    btc_went_up = current_price >= ref_price
                    bet_correct = (
                        (direction == "up" and btc_went_up)
                        or (direction == "down" and not btc_went_up)
                    )

            if bet_correct is not None:
                # PnL calculation
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

                current_price = self.binance.price or 0
                delta_val = ((current_price - ref_price) / ref_price) if ref_price else 0

                notes_text = (
                    f"resolved[{resolution_source}] ref=${ref_price:,.2f} "
                    f"final=${current_price:,.2f} "
                    f"delta={delta_val:.6f} correct={bet_correct} "
                    f"cost=${actual_cost:.2f}"
                )
                if exit_poly_up is not None:
                    notes_text += f" poly_up={exit_poly_up:.4f} poly_down={exit_poly_down:.4f}"

                self.db.resolve_trade(
                    trade_id=trade_id,
                    pnl=round(pnl, 2),
                    exit_reason="market_resolved",
                    notes_append=notes_text,
                )
                log.info(
                    f"Trade #{trade_id} resolved[{resolution_source}]: "
                    f"{'WIN' if bet_correct else 'LOSS'} "
                    f"pnl=${pnl:+.2f} (cost=${actual_cost:.2f})"
                    f"{f' poly_up={exit_poly_up:.4f}' if exit_poly_up is not None else ''}"
                )

                # Trade Analysis
                try:
                    trade_row = self.db.get_trade(trade_id)
                    if trade_row:
                        analyzer.analyze_trade(trade_id, trade_row.get("notes", ""), pnl)
                except Exception as e:
                    log.warning(f"Trade analysis failed: {e}")

                # Monitoring
                if self.monitoring:
                    self.monitoring.record_pnl(pnl)
            else:
                self.db.resolve_trade(
                    trade_id=trade_id,
                    pnl=0.0,
                    exit_reason="resolution_unknown",
                    notes_append="no poly outcome or btc price available",
                )
        except Exception as e:
            log.error(f"Resolution error for trade #{trade_id}: {e}")

    async def force_resolve_trade(self, trade_id: int, direction: str, market_id: str = "", remaining: float = 5):
        """Force resolve a trade - for recovering open positions after restart.
        Tries Polymarket outcome first, falls back to BTC price comparison.
        """
        log.info(f"Force resolving trade #{trade_id} direction={direction}")

        # Try Polymarket outcome first
        bet_correct = None
        resolution_source = "btc_fallback"

        if market_id:
            try:
                fresh = await self.poly.get_market_by_id(market_id)
                if fresh:
                    up_p = fresh.get("up_price", 0.5)
                    down_p = fresh.get("down_price", 0.5)
                    if up_p >= 0.90:
                        bet_correct = (direction == "up")
                        resolution_source = "polymarket"
                    elif down_p >= 0.90:
                        bet_correct = (direction == "down")
                        resolution_source = "polymarket"
            except Exception:
                pass

        # Fallback: BTC price comparison
        ref_price = self.binance.reference_price
        if ref_price is None:
            ref_price = self.binance.price or 0
        current_price = self.binance.price or 0

        if bet_correct is None:
            btc_went_up = current_price >= ref_price if ref_price > 0 else False
            bet_correct = (
                (direction == "up" and btc_went_up)
                or (direction == "down" and not btc_went_up)
            )

        trade_row = self.db.get_trade(trade_id)
        if trade_row:
            actual_cost = trade_row["cost"]
            entry_price = trade_row["price"]
        else:
            actual_cost = self.cfg.TRADE_SIZE_USD
            entry_price = 0.5

        if bet_correct:
            pnl = actual_cost * ((1.0 / entry_price) - 1)
        else:
            pnl = -actual_cost

        self.db.resolve_trade(
            trade_id=trade_id,
            pnl=round(pnl, 2),
            exit_reason="force_resolved_restart",
            notes_append=f"resolved[{resolution_source}] ref=${ref_price:.2f} final=${current_price:.2f}",
        )

        log.info(f"Force resolved[{resolution_source}] trade #{trade_id}: {'WIN' if bet_correct else 'LOSS'} pnl=${pnl:.2f}")

        # Trade Analysis
        try:
            trade_row = self.db.get_trade(trade_id)
            if trade_row:
                analyzer.analyze_trade(trade_id, trade_row.get("notes", ""), pnl)
        except Exception as e:
            log.warning(f"Trade analysis failed: {e}")
