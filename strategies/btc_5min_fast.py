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

    @property
    def interval(self) -> float:
        return self.cfg.LOOP_INTERVAL

    async def scan(self):
        """One iteration of the fast loop."""
        self._scan_count += 1

        # 0. Live trade limit check
        max_live = getattr(self.cfg, "MAX_LIVE_TEST_TRADES", 0)
        if max_live > 0 and not self.cfg.PAPER_TRADING:
            if self._live_trade_count >= max_live:
                if self._scan_count % 30 == 1:
                    log.info(f"Live test limit reached ({self._live_trade_count}/{max_live})")
                return

        # 1. Risk check
        ok, reason = self.risk.can_trade()
        if not ok:
            if self._scan_count % 30 == 1:  # log every ~60s
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
            if self._scan_count % 30 == 1:
                log.info(f"SKIP: cooldown active ({60 - (time.time() - self._last_trade_ts):.0f}s left)")
            return

        # 3c. Ayni market_id'ye tekrar girme
        if market_id in self._traded_market_ids:
            if self._scan_count % 30 == 1:
                log.info(f"SKIP: already traded this market {market_id[:12]}")
            return

        # 4. Already have a position in this market? - Track prices anyway
        if self._has_position or market_id == self._last_trade_market_id:
            await self._track_live_prices(market)
            return

        # 5. OBSERVATION PHASE (remaining > MAX_SECONDS_REMAINING = ilk 120s)
        if remaining > self.cfg.MAX_SECONDS_REMAINING:
            self._collect_observation(market)
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

        # 7. Entry window check (30 <= remaining <= 180)
        if remaining < self.cfg.MIN_SECONDS_REMAINING:
            return

        # 8. Get BTC price data
        delta = self.binance.get_delta()
        if delta is None:
            return

        momentum = self.binance.get_momentum(seconds=30)
        vol = self.binance.get_volatility(seconds=300)

        # 9. NOISE FILTER: delta çok küçükse gürültü
        if abs(delta) < self.cfg.MIN_DELTA_FOR_ENTRY:
            return

        # 9b. MOMENTUM CAP: cok yuksek momentum = trend uzamis, mean reversion riski
        if momentum is not None and abs(momentum) > self.cfg.MAX_MOMENTUM_FOR_ENTRY:
            if self._scan_count % 30 == 1:
                log.info(f"SKIP: momentum too high |{momentum:.6f}| > {self.cfg.MAX_MOMENTUM_FOR_ENTRY}")
            return

        # 10. Determine direction — delta + momentum uyumu gerekli
        if delta > 0 and (momentum is None or momentum >= 0):
            direction = "up"
        elif delta < 0 and (momentum is None or momentum <= 0):
            direction = "down"
        else:
            return

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
            if self._scan_count % 30 == 1:
                log.info(f"SKIP: {direction.upper()} vs macro trend {macro.upper()} (5m={macro_5m:.6f})")
            return

        # 12. OBSERVATION TREND FILTER
        if self._obs_result:
            obs_dir = self._obs_result["direction"]
            obs_consistency = self._obs_result["consistency"]

            if obs_dir != direction:
                if self._scan_count % 30 == 1:
                    log.info(
                        f"SKIP: {direction.upper()} vs observation trend {obs_dir.upper()} "
                        f"(consistency={obs_consistency:.2f})"
                    )
                return

            if obs_consistency < self.cfg.OBSERVATION_CONSISTENCY_MIN:
                if self._scan_count % 30 == 1:
                    log.info(
                        f"SKIP: low observation consistency={obs_consistency:.2f} "
                        f"(min={self.cfg.OBSERVATION_CONSISTENCY_MIN})"
                    )
                return

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
        if not orderbook or not (orderbook.get("bids") and orderbook.get("asks")):
            if self._scan_count % 30 == 1:
                log.info("SKIP: no CLOB orderbook — Gamma price unreliable")
            return

        edge = 0.05  # Sabit edge

        # 15. Position sizing
        price = market.get("up_price" if direction == "up" else "down_price", 0.5)
        size = self.cfg.TRADE_SIZE_USD

        # 15b. PRICE CAP: cok pahali giris = kotu risk/odul orani
        if price > self.cfg.MAX_ENTRY_PRICE:
            if self._scan_count % 30 == 1:
                log.info(f"SKIP: entry price too high {price:.4f} > {self.cfg.MAX_ENTRY_PRICE}")
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
                    log.debug(f"Validation info (paper skip): {reject_reason}")
                else:
                    log.info(f"Validation REJECTED: {reject_reason}")
                    if self.monitoring:
                        self.monitoring.record_opportunity(
                            executed=False,
                            reason=reject_reason,
                        )
                    return
            else:
                log.info(
                    f"Validation PASSED: net=${validation['net_profit_usd']:.4f} "
                    f"slip={validation['slippage_pct']:.4f} "
                    f"fq={fill_quality:.2f} "
                    f"liq=${validation['available_liquidity_usd']:.2f}"
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
            f"{obs_info}{macro_info}"
        )
        if self._ai_prediction:
            notes += (
                f" ai={self._ai_prediction['direction']}"
                f"({self._ai_prediction['confidence']:.2f})"
            )

        log.info(
            f"SIGNAL: {direction.upper()} delta={delta:.6f} "
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
