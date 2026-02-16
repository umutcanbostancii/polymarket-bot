"""Trade execution layer with paper-mode atomic multi-leg simulation."""

import logging
import time
import uuid
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


class Executor:

    def __init__(self, config, db):
        self.cfg = config
        self.db = db

    async def execute(
        self,
        strategy: str,
        market: dict,
        side: str,
        price: float,
        size_usd: float,
        notes: str = "",
        **trade_meta,
    ):
        """
        Execute a single leg.
        Paper mode logs to DB. Live mode is intentionally disabled in this project.
        """
        if not self.cfg.PAPER_TRADING:
            raise NotImplementedError(
                "Live trading not implemented. Set PAPER_TRADING=True in config."
            )

        shares = size_usd / price if price > 0 else 0.0
        question = market.get("question", "")[:120]

        tid = self.db.log_trade(
            strategy=strategy,
            market_id=market.get("id", ""),
            question=question,
            side=side,
            price=price,
            size=shares,
            is_paper=True,
            notes=notes,
            **trade_meta,
        )
        log.info(
            f"PAPER EXEC: {strategy} | {side} notional=${size_usd:.2f} "
            f"price={price:.6f} market={market.get('id','')}"
        )
        return tid

    async def execute_atomic_legs(
        self,
        strategy: str,
        market: dict,
        legs: List[Dict],
        symbol: str = "",
        execution_id: str = "",
        meta: Optional[dict] = None,
    ) -> dict:
        """
        Simulate atomic multi-leg execution.

        Returns:
            {
                "ok": bool,
                "execution_id": str,
                "trade_ids": [int],
                "fill_ratio": float,
                "latency_ms": float,
                "reject_reason": str,
            }
        """
        if not legs:
            return {
                "ok": False,
                "execution_id": execution_id or "",
                "trade_ids": [],
                "fill_ratio": 0.0,
                "latency_ms": 0.0,
                "reject_reason": "no_legs",
            }

        meta = meta or {}
        execution_id = execution_id or str(uuid.uuid4())

        requested_notional = sum(max(0.0, float(l.get("size_usd", 0.0))) for l in legs)
        est_latency_ms = float(meta.get("estimated_latency_ms", 0.0) or 0.0)
        fill_prob = float(meta.get("fill_prob", 1.0) or 1.0)
        fill_prob = max(0.0, min(fill_prob, 1.0))

        min_fill_prob = float(getattr(self.cfg, "POLY_MIN_FILL_PROB", 0.75))
        if fill_prob < min_fill_prob:
            self.db.log_execution_audit(
                strategy=strategy,
                execution_id=execution_id,
                market_id=market.get("id", ""),
                side="+".join(str(l.get("side", "")) for l in legs),
                symbol=symbol,
                status="rejected",
                requested_notional=requested_notional,
                filled_notional=0.0,
                fill_ratio=0.0,
                estimated_latency_ms=est_latency_ms,
                actual_latency_ms=0.0,
                estimated_fee_usd=float(meta.get("expected_fee_usd", 0.0) or 0.0),
                estimated_slippage_usd=float(meta.get("expected_slippage_usd", 0.0) or 0.0),
                estimated_gas_usd=float(meta.get("expected_gas_usd", 0.0) or 0.0),
                reject_reason="fill_prob_below_threshold",
                meta={**meta, "fill_prob": fill_prob},
            )
            return {
                "ok": False,
                "execution_id": execution_id,
                "trade_ids": [],
                "fill_ratio": 0.0,
                "latency_ms": 0.0,
                "reject_reason": "fill_prob_below_threshold",
            }

        start = time.time()
        trade_ids = []

        # Paper-mode approximation: if fill_prob passes threshold we fill atomically.
        actual_fill_ratio = 1.0

        for leg in legs:
            leg_side = str(leg.get("side", ""))
            leg_price = float(leg.get("price", 0.0) or 0.0)
            leg_notional = float(leg.get("size_usd", 0.0) or 0.0)
            leg_notes = str(leg.get("notes", ""))
            leg_market = leg.get("market") or market

            tid = await self.execute(
                strategy=strategy,
                market=leg_market,
                side=leg_side,
                price=leg_price,
                size_usd=leg_notional,
                notes=leg_notes,
                expected_fee_usd=float(leg.get("expected_fee_usd", 0.0) or 0.0),
                expected_slippage_usd=float(leg.get("expected_slippage_usd", 0.0) or 0.0),
                expected_net_usd=float(leg.get("expected_net_usd", 0.0) or 0.0),
                expected_net_pct=float(leg.get("expected_net_pct", 0.0) or 0.0),
                predicted_edge=float(leg.get("predicted_edge", 0.0) or 0.0),
                model_version=str(leg.get("model_version", "") or ""),
                decision_reason=str(leg.get("decision_reason", "") or ""),
                expected_gas_usd=float(leg.get("expected_gas_usd", 0.0) or 0.0),
                entry_latency_ms=est_latency_ms,
                fill_ratio=actual_fill_ratio,
                reject_reason="",
                execution_id=execution_id,
            )
            trade_ids.append(tid)

        latency_ms = max(0.0, (time.time() - start) * 1000.0)
        self.db.log_execution_audit(
            strategy=strategy,
            execution_id=execution_id,
            market_id=market.get("id", ""),
            side="+".join(str(l.get("side", "")) for l in legs),
            symbol=symbol,
            status="filled",
            requested_notional=requested_notional,
            filled_notional=requested_notional,
            fill_ratio=actual_fill_ratio,
            estimated_latency_ms=est_latency_ms,
            actual_latency_ms=latency_ms,
            estimated_fee_usd=float(meta.get("expected_fee_usd", 0.0) or 0.0),
            estimated_slippage_usd=float(meta.get("expected_slippage_usd", 0.0) or 0.0),
            estimated_gas_usd=float(meta.get("expected_gas_usd", 0.0) or 0.0),
            reject_reason="",
            meta={**meta, "fill_prob": fill_prob, "legs": len(legs)},
        )

        return {
            "ok": True,
            "execution_id": execution_id,
            "trade_ids": trade_ids,
            "fill_ratio": actual_fill_ratio,
            "latency_ms": latency_ms,
            "reject_reason": "",
        }
