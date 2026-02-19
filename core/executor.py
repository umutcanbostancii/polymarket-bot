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
        """Lazy-init py-clob-client for live trading (Gnosis Safe proxy)."""
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
                signature_type=2,  # Gnosis Safe proxy
                funder=self.cfg.POLY_FUNDER,
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
        # Polymarket requires 0.01 tick size
        price = math.floor(price * 100) / 100
        if price <= 0 or price >= 1:
            log.error(f"Price invalid after tick rounding: {price}")
            return None

        shares = math.ceil(size_usd / price * 100) / 100  # yukarı yuvarla
        # Polymarket minimum 5 share zorunlulugu
        if shares < 5:
            shares = 5.0
            size_usd = round(shares * price, 2)
            log.info(f"Adjusted to min 5 shares: size_usd=${size_usd:.2f}")
        question = market.get("question", "")[:120]

        # Test stratejileri ASLA live order vermez
        _is_test = strategy_name in ("btc_5min_test", "btc_15min_test")
        _force_paper = self.cfg.PAPER_TRADING or _is_test

        if _force_paper:
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
                condition_id=market.get("condition_id", ""),
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
                strategy_name=strategy_name,
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
        strategy_name: str = "btc_5min_fast",
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
                strategy=strategy_name,
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
                condition_id=market.get("condition_id", ""),
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

    async def sell_position(
        self,
        token_id: str,
        shares: float,
        price: float,
        trade_id: Optional[int] = None,
        strategy_name: str = "btc_5min_fast",
    ) -> bool:
        """
        Profit taking: pozisyonu SELL order ile sat.
        
        Args:
            token_id: Polymarket token ID
            shares: number of shares to sell
            price: sell price (best bid)
            trade_id: optional trade ID for logging
            strategy_name: strategy name for logging
            
        Returns:
            True if sell successful (or paper mode simulated), False otherwise
        """
        if self.cfg.PAPER_TRADING:
            # Paper mode: simulate sell, log to DB
            log.info(
                f"PAPER SELL: {token_id[:10]}... {shares:.2f} shares @ {price:.4f} "
                f"(trade_id={trade_id})"
            )
            # Update trade status in DB if trade_id provided
            if trade_id:
                self.db.update_trade_status(trade_id, "sold", sold_price=price)
            return True
        else:
            # Live mode: execute SELL order with fill verification
            client = self._get_clob_client()
            if not client:
                log.error("Live trading requested but CLOB client not available")
                return False

            try:
                from py_clob_client.order_builder.constants import SELL
                from py_clob_client.clob_types import OrderArgs

                order_args = OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=shares,
                    side=SELL,
                )
                order = client.create_order(order_args)
                result = client.post_order(order)

                success = bool(result and result.get("success"))
                order_id = result.get("orderID", "") if result else ""

                if not success:
                    log.warning(f"LIVE SELL post failed: {result}")
                    return False

                log.info(
                    f"LIVE SELL posted: {token_id[:10]}... {shares:.2f} shares @ {price:.4f} "
                    f"order={order_id} (trade_id={trade_id})"
                )

                # Fill verification: poll for 15s (5 attempts, 3s apart)
                import asyncio
                filled = False
                for attempt in range(5):
                    await asyncio.sleep(3)
                    try:
                        order_state = client.get_order(order_id)
                        if not isinstance(order_state, dict):
                            continue
                        status = str(order_state.get("status") or order_state.get("state") or "").lower()
                        filled_size = float(order_state.get("filled_size") or order_state.get("sizeMatched") or 0.0)

                        if status in ("cancelled", "canceled", "expired"):
                            log.warning(f"LIVE SELL order {status}: {order_id}")
                            return False

                        fill_ratio = filled_size / shares if shares > 0 else 0
                        if fill_ratio >= 0.95:
                            log.info(f"LIVE SELL filled: {filled_size:.2f}/{shares:.2f} shares order={order_id}")
                            filled = True
                            break

                        log.info(f"LIVE SELL polling ({attempt+1}/5): filled={filled_size:.2f}/{shares:.2f} status={status}")
                    except Exception as poll_err:
                        log.warning(f"LIVE SELL poll error: {poll_err}")

                if filled:
                    if trade_id:
                        self.db.update_trade_status(trade_id, "sold", sold_price=price)
                    return True
                else:
                    # Timeout: cancel order, leave for resolution
                    log.warning(f"LIVE SELL timeout, cancelling order {order_id}")
                    try:
                        client.cancel(order_id)
                    except Exception:
                        pass
                    return False

            except Exception as e:
                log.error(f"Live sell error: {e}", exc_info=True)
                return False
