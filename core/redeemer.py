"""Auto-redeem resolved winning positions back to USDC."""

import logging
import time

log = logging.getLogger(__name__)

# Cooldown after relay 429 (quota exceeded) — wait before retrying
RATE_LIMIT_COOLDOWN = 600  # 10 minutes


class PositionRedeemer:
    """Scans for resolved winning trades and redeems CTF shares to USDC
    via Polymarket's gasless relay (no MATIC required)."""

    def __init__(self, config, db_path: str = "trades.db"):
        self.cfg = config
        self.db_path = db_path
        self._client = None
        self._init_failed = False
        self._rate_limited_until = 0.0  # timestamp when cooldown expires

    def _get_client(self):
        """Lazy-init PolymarketGaslessWeb3Client with working RPC."""
        if self._client is not None:
            return self._client
        if self._init_failed:
            return None

        if not self.cfg.POLY_PRIVATE_KEY:
            log.warning("Redeemer: POLY_PRIVATE_KEY not set, skipping")
            self._init_failed = True
            return None

        try:
            from polymarket_apis.clients import web3_client as _wmod
            from web3 import Web3
            from web3.middleware import ExtraDataToPOAMiddleware, SignAndSendRawMiddlewareBuilder

            POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"

            # Monkey-patch BaseWeb3Client.__init__ to use working RPC
            # (default polygon-rpc.com returns 401)
            _orig_init = _wmod.BaseWeb3Client.__init__

            def _patched_init(self_inner, private_key, signature_type, chain_id=137):
                import httpx
                self_inner.client = httpx.Client(http2=True, timeout=30.0)
                self_inner.w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
                self_inner.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                self_inner.w3.middleware_onion.inject(
                    SignAndSendRawMiddlewareBuilder.build(private_key), layer=0
                )
                self_inner.account = self_inner.w3.eth.account.from_key(private_key)
                self_inner.signature_type = signature_type
                self_inner.config = _wmod.get_contract_config(chain_id, neg_risk=False)
                self_inner.neg_risk_config = _wmod.get_contract_config(chain_id, neg_risk=True)
                self_inner.chain_id = chain_id
                self_inner._setup_contracts()
                self_inner._setup_address()

            _wmod.BaseWeb3Client.__init__ = _patched_init
            try:
                self._client = _wmod.PolymarketGaslessWeb3Client(
                    private_key=self.cfg.POLY_PRIVATE_KEY,
                    signature_type=2,  # Safe wallet
                    chain_id=137,
                )
            finally:
                _wmod.BaseWeb3Client.__init__ = _orig_init

            proxy = self._client.get_poly_proxy_address()
            log.info(f"Redeemer gasless client ready (proxy={proxy[:12]}...)")
            return self._client
        except Exception as e:
            log.error(f"Redeemer client init failed: {e}")
            self._init_failed = True
            return None

    def _is_rate_limited(self) -> bool:
        """Check if we're in a cooldown period from relay rate limiting."""
        if time.time() < self._rate_limited_until:
            remaining = int(self._rate_limited_until - time.time())
            log.debug(f"Redeemer rate-limited, {remaining}s remaining")
            return True
        return False

    def redeem_position(self, condition_id: str, neg_risk: bool = False) -> str | None:
        """Redeem a resolved position. Returns tx_hash, 'already_redeemed', or None."""
        if self._is_rate_limited():
            return None

        client = self._get_client()
        if not client:
            return None

        try:
            receipt = client.redeem_position(
                condition_id=condition_id,
                amounts=[0, 0],
                neg_risk=neg_risk,
            )

            if receipt.status == 1:
                tx = receipt.tx_hash
                log.info(f"Redeem SUCCESS: condition={condition_id[:16]}... tx={tx}")
                return tx
            else:
                log.warning(f"Redeem FAILED on-chain: condition={condition_id[:16]}... status={receipt.status}")
                return None
        except Exception as e:
            err_str = str(e)

            # Rate limit from relay — enter cooldown
            if "429" in err_str or "quota exceeded" in err_str.lower():
                # Parse reset time if available
                cooldown = RATE_LIMIT_COOLDOWN
                if "resets in" in err_str:
                    try:
                        secs = int(err_str.split("resets in")[1].split("seconds")[0].strip())
                        cooldown = min(secs + 60, 43200)  # add 60s buffer, cap at 12h
                    except (ValueError, IndexError):
                        pass
                self._rate_limited_until = time.time() + cooldown
                log.warning(f"Relay rate limit hit, cooldown {cooldown}s: {err_str[:120]}")
                return None

            # Already redeemed or no balance — mark as done
            if "revert" in err_str.lower() or "execution reverted" in err_str.lower():
                log.info(f"Redeem skipped (likely already redeemed): condition={condition_id[:16]}...")
                return "already_redeemed"

            log.warning(f"Redeem error: condition={condition_id[:16]}... {e}")
            return None

    def scan_and_redeem(self) -> list:
        """Scan DB for unredeemed winning trades and redeem them.

        Opens its own SQLite connection (thread-safe for run_in_executor).
        Returns list of dicts with trade_id, condition_id, tx_hash.
        """
        if self._is_rate_limited():
            return []

        import sqlite3
        results = []

        try:
            conn = sqlite3.connect(self.db_path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT id, market_id, condition_id, side, size, pnl, strategy
                FROM trades
                WHERE status = 'resolved'
                  AND pnl > 0
                  AND redeemed = 0
                  AND is_paper = 0
                  AND condition_id != ''
                ORDER BY id ASC
            """).fetchall()
            trades = [dict(r) for r in rows]
        except Exception as e:
            log.warning(f"Redeem scan DB error: {e}")
            return results

        if not trades:
            conn.close()
            return results

        log.info(f"Redeem scan: {len(trades)} unredeemed winning trade(s) found")

        # Deduplicate by condition_id (multiple trades can share same condition)
        seen_conditions = set()

        for trade in trades:
            condition_id = trade.get("condition_id", "")
            trade_id = trade["id"]

            if not condition_id:
                continue

            # Only redeem each condition once per scan
            if condition_id in seen_conditions:
                conn.execute(
                    "UPDATE trades SET redeemed = 1, notes = notes || ' | redeemed batch' WHERE id = ?",
                    (trade_id,),
                )
                conn.commit()
                results.append({
                    "trade_id": trade_id,
                    "condition_id": condition_id,
                    "tx_hash": "batch",
                })
                continue

            tx_hash = self.redeem_position(condition_id, neg_risk=False)

            if tx_hash:
                seen_conditions.add(condition_id)
                note = f" | redeemed tx={tx_hash}" if tx_hash != "already_redeemed" else " | redeemed"
                conn.execute(
                    "UPDATE trades SET redeemed = 1, notes = notes || ? WHERE id = ?",
                    (note, trade_id),
                )
                conn.commit()
                results.append({
                    "trade_id": trade_id,
                    "condition_id": condition_id,
                    "tx_hash": tx_hash,
                })
                time.sleep(2)
            elif self._is_rate_limited():
                log.info("Stopping redeem scan due to rate limit")
                break

        conn.close()
        return results

    def get_usdc_balance(self) -> float | None:
        """Get current USDC balance from the Safe wallet."""
        client = self._get_client()
        if not client:
            return None
        try:
            return client.get_usdc_balance()
        except Exception as e:
            log.warning(f"USDC balance check failed: {e}")
            return None
