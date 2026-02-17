"""PDF s.35-36: Layer 3 - Execution Validation.
Her trade oncesi order book'a karsi fill simulasyonu yapar."""

import logging

log = logging.getLogger(__name__)


class ExecutionValidator:

    def __init__(self, config):
        self.cfg = config

    def validate(
        self,
        orderbook: dict,
        side: str,
        size_usd: float,
        predicted_edge: float,
        market_price: float,
    ) -> dict:
        """
        Order book'a karsi fill simulasyonu.

        Returns: {
            "can_execute": bool,
            "vwap_fill_price": float,
            "slippage_pct": float,
            "net_profit_usd": float,
            "fill_quality": float,
            "available_liquidity_usd": float,
            "rejection_reason": str
        }
        """
        result = {
            "can_execute": False,
            "vwap_fill_price": 0.0,
            "slippage_pct": 0.0,
            "net_profit_usd": 0.0,
            "fill_quality": 0.0,
            "available_liquidity_usd": 0.0,
            "rejection_reason": "",
        }

        if not orderbook:
            result["rejection_reason"] = "no_orderbook"
            return result

        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        if not bids or not asks:
            result["rejection_reason"] = "empty_orderbook"
            return result

        midpoint = self._calculate_midpoint(orderbook)
        spread = self._calculate_spread(orderbook)

        # VWAP fill simulation
        vwap, filled_usd = self._calculate_vwap(
            asks if side == "buy" else bids,
            size_usd,
            ascending=(side == "buy"),
        )

        result["available_liquidity_usd"] = filled_usd

        # 1. Liquidity check
        if filled_usd < size_usd:
            result["rejection_reason"] = f"insufficient_liquidity: ${filled_usd:.2f} < ${size_usd:.2f}"
            return result

        if filled_usd < self.cfg.MIN_LIQUIDITY_USD:
            result["rejection_reason"] = f"low_liquidity: ${filled_usd:.2f} < ${self.cfg.MIN_LIQUIDITY_USD:.2f}"
            return result

        result["vwap_fill_price"] = vwap

        # 2. Slippage check
        if midpoint > 0:
            slippage = abs(vwap - midpoint) / midpoint
        else:
            slippage = 0.0
        result["slippage_pct"] = slippage

        if slippage > self.cfg.MAX_SLIPPAGE_PCT:
            result["rejection_reason"] = f"high_slippage: {slippage:.4f} > {self.cfg.MAX_SLIPPAGE_PCT}"
            return result

        # 3. Net profit estimate
        net_profit = self._estimate_net_profit(predicted_edge, vwap, market_price, size_usd)
        result["net_profit_usd"] = net_profit

        if net_profit < self.cfg.MIN_NET_PROFIT_USD:
            result["rejection_reason"] = f"low_profit: ${net_profit:.4f} < ${self.cfg.MIN_NET_PROFIT_USD}"
            return result

        # 4. Fill quality
        fill_quality = self._calculate_fill_quality(vwap, midpoint, spread)
        result["fill_quality"] = fill_quality

        if fill_quality < -1.0:
            result["rejection_reason"] = f"adverse_selection: fill_quality={fill_quality:.2f}"
            return result

        result["can_execute"] = True
        return result

    def _calculate_vwap(self, levels: list, size_usd: float, ascending: bool = True) -> tuple:
        """PDF s.32: VWAP = Sum(price_i * vol_i) / Sum(vol_i)
        Order book levels iterate ederek size_usd kadar doldur.
        Returns: (vwap_price, total_filled_usd)"""
        sorted_levels = sorted(
            levels,
            key=lambda x: float(x.get("price", 0)),
            reverse=(not ascending),
        )

        remaining = size_usd
        total_cost = 0.0
        total_shares = 0.0

        for level in sorted_levels:
            price = float(level.get("price", 0))
            size = float(level.get("size", 0))
            if price <= 0 or size <= 0:
                continue

            level_usd = price * size
            if level_usd >= remaining:
                shares_needed = remaining / price
                total_cost += shares_needed * price
                total_shares += shares_needed
                remaining = 0
                break
            else:
                total_cost += level_usd
                total_shares += size
                remaining -= level_usd

        filled = size_usd - remaining
        vwap = total_cost / total_shares if total_shares > 0 else 0.0
        return vwap, filled

    def _calculate_midpoint(self, orderbook: dict) -> float:
        """Best bid/ask ortasi."""
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        best_bid = max((float(b.get("price", 0)) for b in bids), default=0)
        best_ask = min((float(a.get("price", 1)) for a in asks), default=1)
        return (best_bid + best_ask) / 2.0

    def _calculate_spread(self, orderbook: dict) -> float:
        """Best ask - best bid."""
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        best_bid = max((float(b.get("price", 0)) for b in bids), default=0)
        best_ask = min((float(a.get("price", 1)) for a in asks), default=1)
        return best_ask - best_bid

    def _calculate_fill_quality(self, fill_price: float, midpoint: float, spread: float) -> float:
        """PDF s.69: Fill Quality = (Executed Price - Midpoint) / Spread
        Negative = worse than midpoint (market taker)."""
        if spread <= 0:
            return 0.0
        return (fill_price - midpoint) / spread

    def _estimate_net_profit(self, edge: float, fill_price: float, market_price: float, size_usd: float) -> float:
        """Binary market beklenen kar: p_real * payout - cost - fee.
        p_real = market_price + edge (edge zaten fee/slippage iceriyor).
        Payout: shares * $1 (dogru ise), 0 (yanlis ise)."""
        from core.polymarket_client import PolymarketClient

        if fill_price <= 0 or fill_price >= 1:
            return -size_usd

        # p_real: tahmini gercek olasilik
        p_real = market_price + edge
        p_real = max(0.01, min(0.99, p_real))

        # Shares we'd get at VWAP fill price
        shares = size_usd / fill_price

        # Expected payout = p_real * shares * $1
        expected_payout = p_real * shares

        # Fee on the position
        fee_per_share = PolymarketClient.calculate_dynamic_fee(fill_price)
        total_fee = fee_per_share * size_usd

        # Net = expected_payout - cost - fee
        net = expected_payout - size_usd - total_fee
        return net
