"""Polymarket core engine: complete-set + simplified market-rebalancing arbitrage."""

import logging
import math
import time
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

from strategies.base import BaseStrategy

log = logging.getLogger(__name__)

COIN_MAP = {
    "bitcoin": "BTC",
    "btc": "BTC",
    "ethereum": "ETH",
    "eth": "ETH",
    "solana": "SOL",
    "sol": "SOL",
    "dogecoin": "DOGE",
    "doge": "DOGE",
    "xrp": "XRP",
    "cardano": "ADA",
    "ada": "ADA",
}


class PolymarketCoreStrategy(BaseStrategy):
    name = "polymarket_core"
    model_complete_set = "polymarket_complete_set_v2"
    model_rebalance = "polymarket_rebalance_v2"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_trade_market: Dict[str, float] = {}
        self._rebalance_disabled_logged = False

    @property
    def interval(self) -> float:
        return 25.0

    async def scan(self):
        markets = await self.poly.get_crypto_markets()
        weather = await self.poly.get_weather_markets()
        all_markets = self._dedupe_markets(markets + weather)
        if not all_markets:
            self._audit(
                action="skipped",
                reason="no_markets_found",
                market_id="",
                question="polymarket scan",
                model_version=self.model_complete_set,
            )
            return

        token_ids = []
        for m in all_markets:
            tids = m.get("token_ids", [])
            if isinstance(tids, list):
                token_ids.extend([t for t in tids if t])
        await self.poly.subscribe_orderbooks(token_ids)

        opened_complete = await self._scan_complete_set(all_markets)
        if getattr(self.cfg, "POLY_ENABLE_REBALANCE", False):
            await self._scan_rebalance(all_markets, opened_complete)
        elif not self._rebalance_disabled_logged:
            log.warning("polymarket_core: rebalance engine disabled by config (POLY_ENABLE_REBALANCE=False)")
            self._rebalance_disabled_logged = True

    async def _scan_complete_set(self, markets: List[dict]) -> bool:
        open_market_ids = {p.get("market_id") for p in self.db.active_positions_by_strategy(self.name)}

        candidates = []
        for m in markets:
            market_id = m.get("id", "")
            if not market_id or market_id in open_market_ids:
                continue

            liquidity = float(m.get("liquidity", 0.0))
            if liquidity < self.cfg.POLY_MIN_MARKET_LIQUIDITY:
                continue

            yes_price = float(m.get("yes_price", 0.0))
            no_price = float(m.get("no_price", 0.0))
            if yes_price <= 0 or no_price <= 0:
                continue

            price_sum = yes_price + no_price
            raw_edge = 1.0 - price_sum
            prelim_cost_pct = self.cfg.POLY_TAKER_FEE + self.cfg.POLY_SLIPPAGE_PCT + self.cfg.POLY_EXEC_BUFFER_PCT
            prelim_net = raw_edge - prelim_cost_pct
            candidates.append((prelim_net, m))

        if not candidates:
            return False

        candidates.sort(key=lambda x: x[0], reverse=True)
        for _, market in candidates[:10]:
            if await self._maybe_open_complete_set(market):
                return True
        return False

    async def _maybe_open_complete_set(self, market: dict) -> bool:
        market_id = market.get("id", "")
        question = market.get("question", "")
        liquidity = float(market.get("liquidity", 0.0))

        yes_price = float(market.get("yes_price", 0.0))
        no_price = float(market.get("no_price", 0.0))
        if yes_price <= 0 or no_price <= 0:
            return False

        base_cap = self.cfg.BANKROLL * self.cfg.MAX_POSITION_PCT
        depth_cap = liquidity * self.cfg.POLY_MAX_DEPTH_SHARE * 0.25
        provisional_notional = min(base_cap, depth_cap)

        p_exec = self._execution_success_prob(provisional_notional, liquidity)
        p = max(0.01, min(0.99, 0.92 * p_exec))
        kelly = self._modified_kelly_fraction(p, b=1.0)
        if kelly <= 0:
            self._audit(
                action="skipped",
                reason="kelly_non_positive",
                market_id=market_id,
                question=question,
                predicted_edge=0.0,
                p_exec=p_exec,
                win_prob=p,
                model_version=self.model_complete_set,
            )
            return False

        notional = min(self.cfg.BANKROLL * self.cfg.KELLY_FRACTION * kelly, base_cap, depth_cap)
        if notional < self.cfg.MIN_TRADE_NOTIONAL_USD:
            self._audit(
                action="skipped",
                reason="notional_below_min",
                market_id=market_id,
                question=question,
                notional=notional,
                p_exec=p_exec,
                win_prob=p,
                model_version=self.model_complete_set,
            )
            return False

        yes_token, no_token = self._extract_tokens(market)
        leg_notional = notional / 2.0

        vwap_yes = await self._vwap_price(yes_token, fallback=yes_price, target_usd=leg_notional)
        vwap_no = await self._vwap_price(no_token, fallback=no_price, target_usd=leg_notional)
        if vwap_yes <= 0 or vwap_no <= 0:
            return False

        price_sum = vwap_yes + vwap_no
        if price_sum >= 1.02:
            self._audit(
                action="skipped",
                reason="invalid_leg_pricing_complete_set",
                market_id=market_id,
                question=question,
                side="YES+NO",
                predicted_edge=0.0,
                expected_fee_usd=0.0,
                expected_slippage_usd=0.0,
                expected_net_usd=0.0,
                expected_net_pct=0.0,
                p_exec=p_exec,
                win_prob=p,
                notional=notional,
                model_version=self.model_complete_set,
                meta={"vwap_yes": vwap_yes, "vwap_no": vwap_no, "price_sum": price_sum},
            )
            return False

        raw_edge = 1.0 - price_sum

        latency_ms = max(
            self.poly.orderbook_latency_ms(yes_token) or 0.0,
            self.poly.orderbook_latency_ms(no_token) or 0.0,
        )
        latency_penalty_pct = self._latency_penalty_pct(latency_ms)

        expected_fee_usd = notional * self.cfg.POLY_TAKER_FEE
        expected_slippage_usd = notional * self.cfg.POLY_SLIPPAGE_PCT
        expected_gas_usd = self._estimate_gas_usd(latency_ms)

        base_cost_pct = (
            self.cfg.POLY_TAKER_FEE
            + self.cfg.POLY_SLIPPAGE_PCT
            + self.cfg.POLY_EXEC_BUFFER_PCT
            + latency_penalty_pct
        )

        expected_net_usd = notional * (raw_edge - base_cost_pct) - expected_gas_usd
        expected_net_pct = expected_net_usd / notional if notional > 0 else 0.0

        min_gate_usd = max(
            self.cfg.DIRECTIONAL_MIN_EXPECTED_NET_USD,
            self.cfg.DIRECTIONAL_MIN_NET_FEE_MULT * expected_fee_usd,
            self.cfg.DIRECTIONAL_MIN_NOTIONAL_EDGE_PCT * notional,
        )

        if (
            expected_net_usd < min_gate_usd
            or expected_net_pct < self.cfg.DIRECTIONAL_MIN_EXPECTED_NET_PCT
        ):
            self._audit(
                action="skipped",
                reason="hard_entry_gate",
                market_id=market_id,
                question=question,
                side="YES+NO",
                predicted_edge=raw_edge,
                expected_fee_usd=expected_fee_usd,
                expected_slippage_usd=expected_slippage_usd,
                expected_net_usd=expected_net_usd,
                expected_net_pct=expected_net_pct,
                p_exec=p_exec,
                win_prob=p,
                notional=notional,
                model_version=self.model_complete_set,
                meta={
                    "expected_gas_usd": expected_gas_usd,
                    "min_gate_usd": min_gate_usd,
                    "latency_ms": latency_ms,
                },
            )
            return False

        depth_limit_per_leg = liquidity * self.cfg.POLY_MAX_DEPTH_SHARE * 0.50
        fill_prob = min(0.99, depth_limit_per_leg / max(leg_notional, 1.0))
        min_fill_prob = float(getattr(self.cfg, "POLY_MIN_FILL_PROB", 0.75))
        if leg_notional > depth_limit_per_leg or fill_prob < min_fill_prob:
            self._audit(
                action="skipped",
                reason="non_atomic_fill_risk",
                market_id=market_id,
                question=question,
                side="YES+NO",
                predicted_edge=raw_edge,
                expected_fee_usd=expected_fee_usd,
                expected_slippage_usd=expected_slippage_usd,
                expected_net_usd=expected_net_usd,
                expected_net_pct=expected_net_pct,
                p_exec=fill_prob,
                win_prob=p,
                notional=notional,
                model_version=self.model_complete_set,
                meta={"depth_limit_per_leg": depth_limit_per_leg, "latency_ms": latency_ms},
            )
            return False

        decision_reason = (
            f"complete_set sum={price_sum:.4f} edge={raw_edge:.3%} net={expected_net_pct:.3%} "
            f"fill_prob={fill_prob:.2f} lat={latency_ms:.0f}ms"
        )

        note = (
            f"complete_set | sum={price_sum:.4f} edge={raw_edge:.3%} net={expected_net_pct:.3%} "
            f"fee=${expected_fee_usd:.2f} slip=${expected_slippage_usd:.2f} gas=${expected_gas_usd:.2f}"
        )

        exec_res = await self.executor.execute_atomic_legs(
            strategy=self.name,
            market=market,
            legs=[
                {
                    "market": market,
                    "side": "YES",
                    "price": vwap_yes,
                    "size_usd": leg_notional,
                    "notes": f"{note} | leg=YES",
                    "expected_fee_usd": expected_fee_usd / 2,
                    "expected_slippage_usd": expected_slippage_usd / 2,
                    "expected_net_usd": expected_net_usd / 2,
                    "expected_net_pct": expected_net_pct,
                    "predicted_edge": raw_edge,
                    "model_version": self.model_complete_set,
                    "decision_reason": decision_reason,
                    "expected_gas_usd": expected_gas_usd / 2,
                },
                {
                    "market": market,
                    "side": "NO",
                    "price": vwap_no,
                    "size_usd": leg_notional,
                    "notes": f"{note} | leg=NO",
                    "expected_fee_usd": expected_fee_usd / 2,
                    "expected_slippage_usd": expected_slippage_usd / 2,
                    "expected_net_usd": expected_net_usd / 2,
                    "expected_net_pct": expected_net_pct,
                    "predicted_edge": raw_edge,
                    "model_version": self.model_complete_set,
                    "decision_reason": decision_reason,
                    "expected_gas_usd": expected_gas_usd / 2,
                },
            ],
            symbol=self._symbol_from_question(question) or "",
            meta={
                "fill_prob": fill_prob,
                "estimated_latency_ms": latency_ms,
                "expected_fee_usd": expected_fee_usd,
                "expected_slippage_usd": expected_slippage_usd,
                "expected_gas_usd": expected_gas_usd,
                "decision_reason": decision_reason,
            },
        )
        if not exec_res.get("ok"):
            self._audit(
                action="skipped",
                reason="execution_rejected",
                market_id=market_id,
                question=question,
                side="YES+NO",
                predicted_edge=raw_edge,
                expected_fee_usd=expected_fee_usd,
                expected_slippage_usd=expected_slippage_usd,
                expected_net_usd=expected_net_usd,
                expected_net_pct=expected_net_pct,
                p_exec=fill_prob,
                win_prob=p,
                notional=notional,
                model_version=self.model_complete_set,
                meta={"reject_reason": exec_res.get("reject_reason", ""), "latency_ms": latency_ms},
            )
            return False

        self.db.log_opportunity(
            strategy=self.name,
            market_id=market_id,
            question=question,
            edge=expected_net_pct,
            yes_price=vwap_yes,
            no_price=vwap_no,
            price_sum=price_sum,
            action="trade",
            notes=decision_reason,
        )

        self._audit(
            action="trade",
            reason="entry_ok",
            market_id=market_id,
            question=question,
            side="YES+NO",
            predicted_edge=raw_edge,
            expected_fee_usd=expected_fee_usd,
            expected_slippage_usd=expected_slippage_usd,
            expected_net_usd=expected_net_usd,
            expected_net_pct=expected_net_pct,
            p_exec=fill_prob,
            win_prob=p,
            notional=notional,
            model_version=self.model_complete_set,
            meta={
                "decision": decision_reason,
                "expected_gas_usd": expected_gas_usd,
                "latency_ms": latency_ms,
                "fill_ratio": exec_res.get("fill_ratio", 0.0),
            },
        )

        self._last_trade_market[market_id] = time.time()
        log.info(
            f"{self.name}: complete-set {market_id} notional=${notional:.2f} "
            f"edge={expected_net_pct:.3%} latency={latency_ms:.0f}ms"
        )
        return True

    async def _scan_rebalance(self, markets: List[dict], opened_complete: bool) -> bool:
        if opened_complete:
            return False

        grouped: Dict[str, List[dict]] = defaultdict(list)
        for m in markets:
            symbol = self._symbol_from_question(m.get("question", ""))
            if symbol:
                grouped[symbol].append(m)

        for symbol, items in grouped.items():
            if len(items) < 2:
                continue

            valid = [
                m
                for m in items
                if float(m.get("yes_price", 0)) > 0
                and float(m.get("no_price", 0)) > 0
                and float(m.get("liquidity", 0)) >= self.cfg.POLY_MIN_MARKET_LIQUIDITY
            ]
            if len(valid) < 2:
                continue

            low = min(valid, key=lambda x: float(x.get("yes_price", 0)))
            high = max(valid, key=lambda x: float(x.get("yes_price", 0)))
            spread = float(high.get("yes_price", 0)) - float(low.get("yes_price", 0))

            if spread < self.cfg.POLY_REBALANCE_SPREAD_THRESHOLD:
                continue

            if await self._maybe_open_rebalance(symbol, low, high, spread):
                return True

        return False

    async def _maybe_open_rebalance(
        self,
        symbol: str,
        low_market: dict,
        high_market: dict,
        spread: float,
    ) -> bool:
        low_id = low_market.get("id", "")
        high_id = high_market.get("id", "")

        open_ids = {p.get("market_id") for p in self.db.active_positions_by_strategy(self.name)}
        if low_id in open_ids or high_id in open_ids:
            return False

        min_liq = min(float(low_market.get("liquidity", 0)), float(high_market.get("liquidity", 0)))
        base_cap = self.cfg.BANKROLL * self.cfg.MAX_POSITION_PCT
        depth_cap = min_liq * self.cfg.POLY_MAX_DEPTH_SHARE * 0.20
        provisional_notional = min(base_cap, depth_cap)

        p_exec = self._execution_success_prob(provisional_notional, min_liq)
        p = max(0.01, min(0.98, 0.75 * p_exec + 0.10))
        kelly = self._modified_kelly_fraction(p, b=1.0)
        if kelly <= 0:
            return False

        notional = min(self.cfg.BANKROLL * self.cfg.KELLY_FRACTION * kelly, base_cap, depth_cap)
        if notional < self.cfg.MIN_TRADE_NOTIONAL_USD:
            return False

        high_yes_token, high_no_token = self._extract_tokens(high_market)
        low_yes_token, low_no_token = self._extract_tokens(low_market)

        leg_notional = notional / 2.0
        high_no = await self._vwap_price(high_no_token, fallback=float(high_market.get("no_price", 0)), target_usd=leg_notional)
        low_yes = await self._vwap_price(low_yes_token, fallback=float(low_market.get("yes_price", 0)), target_usd=leg_notional)
        if high_no <= 0 or low_yes <= 0:
            return False
        cross_sum = high_no + low_yes
        if cross_sum >= 1.0:
            self._audit(
                action="skipped",
                reason="invalid_leg_pricing_rebalance",
                market_id=f"{high_id}|{low_id}",
                question=f"{symbol} rebalancing pair",
                side="NO+YES",
                predicted_edge=spread,
                expected_fee_usd=0.0,
                expected_slippage_usd=0.0,
                expected_net_usd=0.0,
                expected_net_pct=0.0,
                p_exec=p_exec,
                win_prob=p,
                notional=notional,
                model_version=self.model_rebalance,
                meta={"high_no": high_no, "low_yes": low_yes, "cross_sum": cross_sum},
            )
            return False

        latency_ms = max(
            self.poly.orderbook_latency_ms(high_no_token) or 0.0,
            self.poly.orderbook_latency_ms(low_yes_token) or 0.0,
        )
        latency_penalty_pct = self._latency_penalty_pct(latency_ms)

        expected_fee_usd = notional * self.cfg.POLY_TAKER_FEE
        expected_slippage_usd = notional * self.cfg.POLY_SLIPPAGE_PCT
        expected_gas_usd = self._estimate_gas_usd(latency_ms)

        total_cost_pct = 2 * (
            self.cfg.POLY_TAKER_FEE
            + self.cfg.POLY_SLIPPAGE_PCT
            + self.cfg.POLY_EXEC_BUFFER_PCT
            + latency_penalty_pct
        )
        expected_net_pct = spread - total_cost_pct - (expected_gas_usd / max(notional, 1.0))
        expected_net_usd = notional * expected_net_pct

        min_gate_usd = max(
            self.cfg.DIRECTIONAL_MIN_EXPECTED_NET_USD,
            self.cfg.DIRECTIONAL_MIN_NET_FEE_MULT * expected_fee_usd,
            self.cfg.DIRECTIONAL_MIN_NOTIONAL_EDGE_PCT * notional,
        )
        if (
            expected_net_usd < min_gate_usd
            or expected_net_pct < self.cfg.DIRECTIONAL_MIN_EXPECTED_NET_PCT
        ):
            self._audit(
                action="skipped",
                reason="hard_entry_gate_rebalance",
                market_id=f"{high_id}|{low_id}",
                question=f"{symbol} rebalancing pair",
                side="NO+YES",
                predicted_edge=spread,
                expected_fee_usd=expected_fee_usd,
                expected_slippage_usd=expected_slippage_usd,
                expected_net_usd=expected_net_usd,
                expected_net_pct=expected_net_pct,
                p_exec=p_exec,
                win_prob=p,
                notional=notional,
                model_version=self.model_rebalance,
                meta={"expected_gas_usd": expected_gas_usd, "latency_ms": latency_ms},
            )
            return False

        depth_limit = min_liq * self.cfg.POLY_MAX_DEPTH_SHARE * 0.50
        fill_prob = min(0.99, depth_limit / max(leg_notional, 1.0))
        if leg_notional > depth_limit or fill_prob < self.cfg.POLY_MIN_FILL_PROB:
            self._audit(
                action="skipped",
                reason="non_atomic_fill_risk_rebalance",
                market_id=f"{high_id}|{low_id}",
                question=f"{symbol} rebalancing pair",
                side="NO+YES",
                predicted_edge=spread,
                expected_fee_usd=expected_fee_usd,
                expected_slippage_usd=expected_slippage_usd,
                expected_net_usd=expected_net_usd,
                expected_net_pct=expected_net_pct,
                p_exec=fill_prob,
                win_prob=p,
                notional=notional,
                model_version=self.model_rebalance,
                meta={"depth_limit": depth_limit, "latency_ms": latency_ms},
            )
            return False

        decision_reason = (
            f"rebalance spread={spread:.3%} net={expected_net_pct:.3%} "
            f"symbol={symbol} fill_prob={fill_prob:.2f} lat={latency_ms:.0f}ms"
        )

        note = (
            f"rebalance {symbol} spread={spread:.3%} net={expected_net_pct:.3%} "
            f"fee=${expected_fee_usd:.2f} slip=${expected_slippage_usd:.2f} gas=${expected_gas_usd:.2f}"
        )

        exec_res = await self.executor.execute_atomic_legs(
            strategy=self.name,
            market={"id": f"{high_id}|{low_id}", "question": f"{symbol} rebalancing pair"},
            legs=[
                {
                    "market": high_market,
                    "side": "NO",
                    "price": high_no,
                    "size_usd": leg_notional,
                    "notes": f"{note} | overpriced_leg",
                    "expected_fee_usd": expected_fee_usd / 2,
                    "expected_slippage_usd": expected_slippage_usd / 2,
                    "expected_net_usd": expected_net_usd / 2,
                    "expected_net_pct": expected_net_pct,
                    "predicted_edge": spread,
                    "model_version": self.model_rebalance,
                    "decision_reason": decision_reason,
                    "expected_gas_usd": expected_gas_usd / 2,
                },
                {
                    "market": low_market,
                    "side": "YES",
                    "price": low_yes,
                    "size_usd": leg_notional,
                    "notes": f"{note} | underpriced_leg",
                    "expected_fee_usd": expected_fee_usd / 2,
                    "expected_slippage_usd": expected_slippage_usd / 2,
                    "expected_net_usd": expected_net_usd / 2,
                    "expected_net_pct": expected_net_pct,
                    "predicted_edge": spread,
                    "model_version": self.model_rebalance,
                    "decision_reason": decision_reason,
                    "expected_gas_usd": expected_gas_usd / 2,
                },
            ],
            symbol=symbol,
            meta={
                "fill_prob": fill_prob,
                "estimated_latency_ms": latency_ms,
                "expected_fee_usd": expected_fee_usd,
                "expected_slippage_usd": expected_slippage_usd,
                "expected_gas_usd": expected_gas_usd,
                "decision_reason": decision_reason,
            },
        )
        if not exec_res.get("ok"):
            self._audit(
                action="skipped",
                reason="execution_rejected_rebalance",
                market_id=f"{high_id}|{low_id}",
                question=f"{symbol} rebalancing pair",
                side="NO+YES",
                predicted_edge=spread,
                expected_fee_usd=expected_fee_usd,
                expected_slippage_usd=expected_slippage_usd,
                expected_net_usd=expected_net_usd,
                expected_net_pct=expected_net_pct,
                p_exec=fill_prob,
                win_prob=p,
                notional=notional,
                model_version=self.model_rebalance,
                meta={"reject_reason": exec_res.get("reject_reason", "")},
            )
            return False

        self.db.log_opportunity(
            strategy=self.name,
            market_id=f"{high_id}|{low_id}",
            question=f"{symbol} rebalancing pair",
            edge=expected_net_pct,
            yes_price=low_yes,
            no_price=high_no,
            price_sum=high_no + low_yes,
            action="trade",
            notes=decision_reason,
        )

        self._audit(
            action="trade",
            reason="entry_ok_rebalance",
            market_id=f"{high_id}|{low_id}",
            question=f"{symbol} rebalancing pair",
            side="NO+YES",
            predicted_edge=spread,
            expected_fee_usd=expected_fee_usd,
            expected_slippage_usd=expected_slippage_usd,
            expected_net_usd=expected_net_usd,
            expected_net_pct=expected_net_pct,
            p_exec=fill_prob,
            win_prob=p,
            notional=notional,
            model_version=self.model_rebalance,
            meta={
                "decision": decision_reason,
                "expected_gas_usd": expected_gas_usd,
                "latency_ms": latency_ms,
                "fill_ratio": exec_res.get("fill_ratio", 0.0),
            },
        )

        log.info(
            f"{self.name}: rebalance {symbol} notional=${notional:.2f} "
            f"spread={spread:.3%} net={expected_net_pct:.3%}"
        )
        return True

    def _execution_success_prob(self, notional: float, liquidity: float) -> float:
        if liquidity <= 0:
            return 0.0
        ratio = notional / max(liquidity, 1.0)
        return max(0.35, min(0.99, 1.0 - ratio * 1.6))

    @staticmethod
    def _modified_kelly_fraction(p: float, b: float = 1.0) -> float:
        if p <= 0 or p >= 1 or b <= 0:
            return 0.0
        q = 1.0 - p
        standard = ((b * p) - q) / b
        if standard <= 0:
            return 0.0
        return max(0.0, min(0.5, standard * math.sqrt(p)))

    async def _vwap_price(self, token_id: str, fallback: float, target_usd: float) -> float:
        if not token_id:
            return fallback
        book = await self.poly.orderbook_snapshot(token_id, max_age_ms=self.cfg.POLY_ORDERBOOK_STALE_MS)
        if not book:
            return fallback
        vwap = self._vwap_from_book(book, target_usd, fallback)
        return vwap if vwap and vwap > 0 else fallback

    @staticmethod
    def _to_level(level) -> Optional[Tuple[float, float]]:
        # Supports both [price, size] and {price, size} styles.
        try:
            if isinstance(level, dict):
                p = float(level.get("price", level.get("p", 0)))
                s = float(level.get("size", level.get("s", 0)))
                return (p, s) if p > 0 and s > 0 else None
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                p = float(level[0])
                s = float(level[1])
                return (p, s) if p > 0 and s > 0 else None
        except Exception:
            return None
        return None

    def _vwap_from_book(self, book: dict, target_usd: float, fallback: float) -> Optional[float]:
        asks = book.get("asks") if isinstance(book, dict) else None
        if not isinstance(asks, list) or target_usd <= 0:
            return None

        ask_levels: List[Tuple[float, float]] = []
        for raw in asks:
            lvl = self._to_level(raw)
            if lvl:
                ask_levels.append(lvl)
        if not ask_levels:
            return None
        ask_levels.sort(key=lambda x: x[0])  # Buy-side VWAP must start from the best (lowest) ask.

        bid_levels: List[Tuple[float, float]] = []
        bids = book.get("bids") if isinstance(book, dict) else None
        if isinstance(bids, list):
            for raw in bids:
                lvl = self._to_level(raw)
                if lvl:
                    bid_levels.append(lvl)
            bid_levels.sort(key=lambda x: x[0], reverse=True)

        best_ask = ask_levels[0][0]
        best_bid = bid_levels[0][0] if bid_levels else 0.0
        if best_bid > 0 and best_ask >= best_bid:
            mid = (best_ask + best_bid) / 2.0
            spread_pct = (best_ask - best_bid) / max(mid, 1e-9)
            if spread_pct > float(getattr(self.cfg, "POLY_MAX_ORDERBOOK_SPREAD_PCT", 0.35)):
                return None

        if fallback > 0:
            dev_pct = abs(best_ask - fallback) / fallback
            if dev_pct > float(getattr(self.cfg, "POLY_MAX_VWAP_DEVIATION_PCT", 0.40)):
                return None

        remaining = float(target_usd)
        total_qty = 0.0
        total_value = 0.0

        for price, size in ask_levels:
            level_notional = price * size
            if level_notional <= 0:
                continue
            consume = min(remaining, level_notional)
            qty = consume / price
            total_qty += qty
            total_value += consume
            remaining -= consume
            if remaining <= 1e-9:
                break

        if total_qty <= 0:
            return None
        return total_value / total_qty

    def _estimate_gas_usd(self, latency_ms: float) -> float:
        base = float(getattr(self.cfg, "POLY_GAS_EST_USD", 0.60))
        max_gas = float(getattr(self.cfg, "POLY_MAX_GAS_EST_USD", 4.0))
        penalty = max(0.0, (latency_ms - 250.0) / 1000.0)
        return min(max_gas, base * (1.0 + penalty))

    def _latency_penalty_pct(self, latency_ms: float) -> float:
        bps_per_100ms = float(getattr(self.cfg, "POLY_LATENCY_PENALTY_BPS_PER_100MS", 1.5))
        return ((latency_ms / 100.0) * bps_per_100ms) / 10000.0

    @staticmethod
    def _dedupe_markets(markets: List[dict]) -> List[dict]:
        out = []
        seen = set()
        for m in markets:
            mid = m.get("id")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            out.append(m)
        return out

    @staticmethod
    def _extract_tokens(market: dict) -> Tuple[str, str]:
        tids = market.get("token_ids", [])
        if isinstance(tids, list):
            yes = str(tids[0]) if len(tids) > 0 else ""
            no = str(tids[1]) if len(tids) > 1 else ""
            return yes, no
        return "", ""

    @staticmethod
    def _symbol_from_question(question: str) -> Optional[str]:
        q = (question or "").lower()
        for key, symbol in COIN_MAP.items():
            if key in q:
                return symbol
        return None

    def _audit(
        self,
        action: str,
        reason: str,
        market_id: str,
        question: str,
        side: str = "",
        predicted_edge: float = 0.0,
        expected_fee_usd: float = 0.0,
        expected_slippage_usd: float = 0.0,
        expected_net_usd: float = 0.0,
        expected_net_pct: float = 0.0,
        model_version: str = "",
        confidence: float = 0.0,
        p_exec: float = 0.0,
        win_prob: float = 0.0,
        notional: float = 0.0,
        meta: dict = None,
    ):
        self.db.log_decision_audit(
            strategy=self.name,
            market_id=market_id,
            question=question,
            symbol=self._symbol_from_question(question) or "",
            side=side,
            action=action,
            decision_reason=reason,
            predicted_edge=predicted_edge,
            expected_fee_usd=expected_fee_usd,
            expected_slippage_usd=expected_slippage_usd,
            expected_net_usd=expected_net_usd,
            expected_net_pct=expected_net_pct,
            model_version=model_version or self.model_complete_set,
            confidence=confidence,
            p_exec=p_exec,
            win_prob=win_prob,
            notional=notional,
            data_quality=1.0,
            meta=meta or {},
        )
