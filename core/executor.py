"""Trade execution for BTC 5-min directional bets. Paper + live mode."""

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)


class Executor:

    def __init__(self, config, db):
        self.cfg = config
        self.db = db
        self._clob_client = None

    def _get_clob_client(self):
        """Lazy-init py-clob-client for live trading."""
        if self._clob_client is not None:
            return self._clob_client

        if not self.cfg.POLY_PRIVATE_KEY:
            return None

        try:
            from py_clob_client.client import ClobClient
            self._clob_client = ClobClient(
                self.cfg.CLOB_API,
                key=self.cfg.POLY_PRIVATE_KEY,
                chain_id=137,
            )
            # Derive or use existing API creds
            if self.cfg.POLY_API_KEY:
                from py_clob_client.clob_types import ApiCreds
                self._clob_client.set_api_creds(ApiCreds(
                    api_key=self.cfg.POLY_API_KEY,
                    api_secret=self.cfg.POLY_API_SECRET,
                    api_passphrase=self.cfg.POLY_API_PASSPHRASE,
                ))
            else:
                self._clob_client.create_or_derive_api_creds()
            log.info("CLOB client initialized for live trading")
            return self._clob_client
        except Exception as e:
            log.error(f"Failed to init CLOB client: {e}")
            return None

    async def execute_directional_bet(
        self,
        market: dict,
        direction: str,
        size_usd: float,
        edge: float = 0.0,
        notes: str = "",
        fill_quality: float = 0.0,
        latency_ms: float = 0.0,
        strategy_name: str = "btc_5min_fast",
    ) -> Optional[int]:
        """
        Place a directional bet on Up or Down.

        Args:
            market: parsed market dict with up_token, down_token, up_price, down_price
            direction: "up" or "down"
            size_usd: amount in USDC
            edge: calculated edge for logging
            notes: extra notes

        Returns:
            trade_id from DB, or None on failure
        """
        if direction == "up":
            token_id = market.get("up_token")
            price = market.get("up_price", 0.5)
            side = "BUY_UP"
        elif direction == "down":
            token_id = market.get("down_token")
            price = market.get("down_price", 0.5)
            side = "BUY_DOWN"
        else:
            log.error(f"Invalid direction: {direction}")
            return None

        if not token_id:
            log.error(f"No token_id for direction={direction}")
            return None

        if price <= 0 or price >= 1:
            log.error(f"Invalid price={price} for {direction}")
            return None

        import math
        shares = math.ceil(size_usd / price * 100) / 100  # yukarı yuvarla, min $1 notional
        question = market.get("question", "")[:120]

        if self.cfg.PAPER_TRADING:
            # Paper mode: just log to DB
            tid = self.db.log_trade(
                strategy=strategy_name,
                market_id=market.get("id", ""),
                question=question,
                side=side,
                price=price,
                size=shares,
                is_paper=True,
                notes=notes,
                predicted_edge=edge,
                decision_reason=f"direction={direction} edge={edge:.4f}",
                entry_latency_ms=latency_ms,
            )
            # Log execution audit with fill quality
            self.db.log_execution_audit(
                strategy=strategy_name,
                market_id=market.get("id", ""),
                side=side,
                status="filled",
                requested_notional=size_usd,
                filled_notional=size_usd,
                fill_ratio=1.0,
                actual_latency_ms=latency_ms,
                estimated_fee_usd=0.0,
                meta={"fill_quality": fill_quality, "paper": True},
            )
            log.info(
                f"PAPER BET: {side} ${size_usd:.2f} @ {price:.4f} "
                f"edge={edge:.4f} fq={fill_quality:.2f} lat={latency_ms:.0f}ms"
            )
            return tid
        else:
            # Live mode: use py-clob-client
            return await self._execute_live(
                market=market,
                token_id=token_id,
                side=side,
                price=price,
                size_usd=size_usd,
                shares=shares,
                edge=edge,
                question=question,
                direction=direction,
                notes=notes,
            )

    async def _execute_live(
        self,
        market: dict,
        token_id: str,
        side: str,
        price: float,
        size_usd: float,
        shares: float,
        edge: float,
        question: str,
        direction: str,
        notes: str,
    ) -> Optional[int]:
        """Execute a live order via py-clob-client."""
        client = self._get_clob_client()
        if not client:
            log.error("Live trading requested but CLOB client not available")
            return None

        try:
            from py_clob_client.order_builder.constants import BUY
            from py_clob_client.clob_types import OrderArgs

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=shares,
                side=BUY,
            )
            order = client.create_order(order_args)
            result = client.post_order(order)

            success = bool(result and result.get("success"))
            order_id = result.get("orderID", "") if result else ""

            tid = self.db.log_trade(
                strategy="btc_5min_fast",
                market_id=market.get("id", ""),
                question=question,
                side=side,
                price=price,
                size=shares,
                is_paper=False,
                notes=f"{notes} order_id={order_id}",
                predicted_edge=edge,
                decision_reason=f"LIVE direction={direction} edge={edge:.4f}",
                execution_id=order_id,
            )

            if success:
                log.info(
                    f"LIVE BET: {side} ${size_usd:.2f} @ {price:.4f} "
                    f"edge={edge:.4f} order={order_id}"
                )
            else:
                log.warning(f"LIVE BET may have failed: {result}")

            return tid

        except Exception as e:
            log.error(f"Live execution error: {e}", exc_info=True)
            return None
