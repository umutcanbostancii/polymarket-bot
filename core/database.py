"""SQLite-backed trade, opportunity, and decision audit storage."""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

log = logging.getLogger(__name__)


StrategyRef = Union[str, Sequence[str]]


class TradeDB:

    def __init__(self, path: str = "trades.db"):
        self.path = path
        self.conn = None

    def _ensure_conn(self):
        if self.conn is None:
            raise RuntimeError("Database not initialized")

    def start(self):
        self.conn = sqlite3.connect(self.path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                strategy TEXT,
                market_id TEXT,
                question TEXT,
                side TEXT,
                price REAL,
                size REAL,
                cost REAL,
                is_paper BOOLEAN DEFAULT 1,
                status TEXT DEFAULT 'open',
                pnl REAL DEFAULT 0,
                resolved_at TEXT,
                notes TEXT,
                expected_fee_usd REAL DEFAULT 0,
                expected_slippage_usd REAL DEFAULT 0,
                expected_net_usd REAL DEFAULT 0,
                expected_net_pct REAL DEFAULT 0,
                predicted_edge REAL DEFAULT 0,
                model_version TEXT DEFAULT '',
                decision_reason TEXT DEFAULT '',
                exit_reason TEXT DEFAULT '',
                realized_fee_usd REAL DEFAULT 0,
                realized_net_pct REAL DEFAULT 0,
                borrow_fee_usd REAL DEFAULT 0,
                realized_slippage_usd REAL DEFAULT 0,
                gross_pnl REAL DEFAULT 0,
                expected_gas_usd REAL DEFAULT 0,
                realized_gas_usd REAL DEFAULT 0,
                entry_latency_ms REAL DEFAULT 0,
                fill_ratio REAL DEFAULT 0,
                reject_reason TEXT DEFAULT '',
                execution_id TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                strategy TEXT,
                market_id TEXT,
                question TEXT,
                edge REAL,
                yes_price REAL,
                no_price REAL,
                price_sum REAL,
                binance_price REAL,
                action TEXT DEFAULT 'logged',
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS decision_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                strategy TEXT,
                market_id TEXT,
                question TEXT,
                symbol TEXT,
                side TEXT,
                action TEXT,
                decision_reason TEXT,
                predicted_edge REAL DEFAULT 0,
                expected_fee_usd REAL DEFAULT 0,
                expected_slippage_usd REAL DEFAULT 0,
                expected_net_usd REAL DEFAULT 0,
                expected_net_pct REAL DEFAULT 0,
                model_version TEXT DEFAULT '',
                confidence REAL DEFAULT 0,
                p_exec REAL DEFAULT 0,
                win_prob REAL DEFAULT 0,
                notional REAL DEFAULT 0,
                data_quality REAL DEFAULT 0,
                meta_json TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS execution_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                strategy TEXT,
                execution_id TEXT,
                market_id TEXT,
                side TEXT,
                symbol TEXT,
                status TEXT,
                requested_notional REAL DEFAULT 0,
                filled_notional REAL DEFAULT 0,
                fill_ratio REAL DEFAULT 0,
                estimated_latency_ms REAL DEFAULT 0,
                actual_latency_ms REAL DEFAULT 0,
                estimated_fee_usd REAL DEFAULT 0,
                estimated_slippage_usd REAL DEFAULT 0,
                estimated_gas_usd REAL DEFAULT 0,
                reject_reason TEXT DEFAULT '',
                meta_json TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS validation_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                window_days INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_trades_status_ts ON trades(status, ts);
            CREATE INDEX IF NOT EXISTS idx_trades_strategy_ts ON trades(strategy, ts);
            CREATE INDEX IF NOT EXISTS idx_decision_strategy_ts ON decision_audit(strategy, ts);
            CREATE INDEX IF NOT EXISTS idx_decision_action_ts ON decision_audit(action, ts);
            CREATE INDEX IF NOT EXISTS idx_execution_strategy_ts ON execution_audit(strategy, ts);
            CREATE INDEX IF NOT EXISTS idx_execution_status_ts ON execution_audit(status, ts);
            CREATE INDEX IF NOT EXISTS idx_validation_ts ON validation_audit(ts);

            CREATE TABLE IF NOT EXISTS monitoring_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                opp_detected INTEGER DEFAULT 0,
                opp_executed INTEGER DEFAULT 0,
                exec_rate REAL DEFAULT 0,
                avg_latency_ms REAL DEFAULT 0,
                avg_fill_quality REAL DEFAULT 0,
                drawdown_pct REAL DEFAULT 0,
                total_profit REAL DEFAULT 0,
                alarms_json TEXT DEFAULT '[]'
            );

            CREATE INDEX IF NOT EXISTS idx_monitoring_ts ON monitoring_metrics(ts);

            CREATE TABLE IF NOT EXISTS arb_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_open TEXT NOT NULL,
                ts_close TEXT DEFAULT '',
                symbol TEXT NOT NULL,
                timeframe TEXT DEFAULT '15m',
                market_id TEXT,
                question TEXT,
                mode TEXT DEFAULT 'paper',
                status TEXT DEFAULT 'waiting_fill',
                entry_price_limit REAL DEFAULT 0.45,
                bailout_price REAL DEFAULT 0.72,
                cycle_budget_usd REAL DEFAULT 0,
                shares_target REAL DEFAULT 0,
                up_token_id TEXT DEFAULT '',
                down_token_id TEXT DEFAULT '',
                up_order_id TEXT DEFAULT '',
                down_order_id TEXT DEFAULT '',
                up_fill_shares REAL DEFAULT 0,
                down_fill_shares REAL DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                payout_usd REAL DEFAULT 0,
                fees_usd REAL DEFAULT 0,
                pnl_usd REAL DEFAULT 0,
                lock_skipped INTEGER DEFAULT 0,
                lock_skip_reason TEXT DEFAULT '',
                liquidity_reject INTEGER DEFAULT 0,
                spread_reject INTEGER DEFAULT 0,
                competition_score REAL DEFAULT 0,
                dynamic_price_suggested REAL DEFAULT 0,
                exit_reason TEXT DEFAULT '',
                notes TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS arb_order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                cycle_id INTEGER DEFAULT 0,
                symbol TEXT DEFAULT '',
                market_id TEXT DEFAULT '',
                side TEXT DEFAULT '',
                action TEXT DEFAULT '',
                order_id TEXT DEFAULT '',
                price REAL DEFAULT 0,
                size REAL DEFAULT 0,
                filled_size REAL DEFAULT 0,
                remaining_size REAL DEFAULT 0,
                state TEXT DEFAULT '',
                message TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS arb_runtime_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_arb_cycles_ts ON arb_cycles(ts_open);
            CREATE INDEX IF NOT EXISTS idx_arb_cycles_symbol_status ON arb_cycles(symbol, status);
            CREATE INDEX IF NOT EXISTS idx_arb_events_cycle_ts ON arb_order_events(cycle_id, ts);
            """
        )

        # Migration-safe for older DB files.
        self._migrate_trades_schema()
        self._migrate_arb_schema()
        self.conn.commit()
        log.info(f"TradeDB ready: {self.path}")

    def stop(self):
        if self.conn:
            self.conn.close()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _table_columns(self, table: str) -> set:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}

    def _table_exists(self, table: str) -> bool:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None

    def _ensure_column(self, table: str, column: str, ddl: str):
        cols = self._table_columns(table)
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _migrate_trades_schema(self):
        required = {
            "expected_fee_usd": "REAL DEFAULT 0",
            "expected_slippage_usd": "REAL DEFAULT 0",
            "expected_net_usd": "REAL DEFAULT 0",
            "expected_net_pct": "REAL DEFAULT 0",
            "predicted_edge": "REAL DEFAULT 0",
            "model_version": "TEXT DEFAULT ''",
            "decision_reason": "TEXT DEFAULT ''",
            "exit_reason": "TEXT DEFAULT ''",
            "realized_fee_usd": "REAL DEFAULT 0",
            "realized_net_pct": "REAL DEFAULT 0",
            "borrow_fee_usd": "REAL DEFAULT 0",
            "realized_slippage_usd": "REAL DEFAULT 0",
            "gross_pnl": "REAL DEFAULT 0",
            "expected_gas_usd": "REAL DEFAULT 0",
            "realized_gas_usd": "REAL DEFAULT 0",
            "entry_latency_ms": "REAL DEFAULT 0",
            "fill_ratio": "REAL DEFAULT 0",
            "reject_reason": "TEXT DEFAULT ''",
            "execution_id": "TEXT DEFAULT ''",
            "redeemed": "INTEGER DEFAULT 0",
            "condition_id": "TEXT DEFAULT ''",
        }
        for name, ddl in required.items():
            self._ensure_column("trades", name, ddl)

    def _migrate_arb_schema(self):
        if self._table_exists("arb_cycles"):
            cycle_required = {
                "timeframe": "TEXT DEFAULT '15m'",
                "mode": "TEXT DEFAULT 'paper'",
                "entry_price_limit": "REAL DEFAULT 0.45",
                "bailout_price": "REAL DEFAULT 0.72",
                "cycle_budget_usd": "REAL DEFAULT 0",
                "shares_target": "REAL DEFAULT 0",
                "up_token_id": "TEXT DEFAULT ''",
                "down_token_id": "TEXT DEFAULT ''",
                "up_order_id": "TEXT DEFAULT ''",
                "down_order_id": "TEXT DEFAULT ''",
                "up_fill_shares": "REAL DEFAULT 0",
                "down_fill_shares": "REAL DEFAULT 0",
                "cost_usd": "REAL DEFAULT 0",
                "payout_usd": "REAL DEFAULT 0",
                "fees_usd": "REAL DEFAULT 0",
                "pnl_usd": "REAL DEFAULT 0",
                "lock_skipped": "INTEGER DEFAULT 0",
                "lock_skip_reason": "TEXT DEFAULT ''",
                "liquidity_reject": "INTEGER DEFAULT 0",
                "spread_reject": "INTEGER DEFAULT 0",
                "competition_score": "REAL DEFAULT 0",
                "dynamic_price_suggested": "REAL DEFAULT 0",
                "exit_reason": "TEXT DEFAULT ''",
                "notes": "TEXT DEFAULT ''",
            }
            for name, ddl in cycle_required.items():
                self._ensure_column("arb_cycles", name, ddl)

        if self._table_exists("arb_order_events"):
            event_required = {
                "cycle_id": "INTEGER DEFAULT 0",
                "symbol": "TEXT DEFAULT ''",
                "market_id": "TEXT DEFAULT ''",
                "side": "TEXT DEFAULT ''",
                "action": "TEXT DEFAULT ''",
                "order_id": "TEXT DEFAULT ''",
                "price": "REAL DEFAULT 0",
                "size": "REAL DEFAULT 0",
                "filled_size": "REAL DEFAULT 0",
                "remaining_size": "REAL DEFAULT 0",
                "state": "TEXT DEFAULT ''",
                "message": "TEXT DEFAULT ''",
            }
            for name, ddl in event_required.items():
                self._ensure_column("arb_order_events", name, ddl)

    def _strategy_clause(self, strategy: StrategyRef) -> Tuple[str, Tuple]:
        if isinstance(strategy, str):
            return "= ?", (strategy,)

        values = tuple(strategy)
        if not values:
            return "IN ('')", tuple()

        placeholders = ",".join("?" for _ in values)
        return f"IN ({placeholders})", values

    # ------------------------------------------------------------------
    # Single trade lookup
    # ------------------------------------------------------------------

    def get_trade(self, trade_id: int) -> Optional[dict]:
        """Tek bir trade'in tum bilgilerini dondur. Paper PnL fix icin."""
        self._ensure_conn()
        row = self.conn.execute(
            """
            SELECT id, ts as time, strategy, market_id, question,
                   side, price, size, cost,
                   is_paper, status, pnl, resolved_at, notes,
                   expected_fee_usd, expected_slippage_usd,
                   expected_net_usd, expected_net_pct,
                   predicted_edge, model_version,
                   decision_reason, exit_reason,
                   realized_fee_usd, realized_net_pct,
                   borrow_fee_usd, realized_slippage_usd,
                   gross_pnl, expected_gas_usd, realized_gas_usd,
                   entry_latency_ms, fill_ratio, reject_reason, execution_id
            FROM trades
            WHERE id=?
            """,
            (trade_id,),
        ).fetchone()
        if row:
            return dict(row)
        return None

    def get_open_trades(self) -> list:
        """Açık (resolved olmamış) tüm trade'leri getir."""
        self._ensure_conn()
        rows = self.conn.execute(
            """
            SELECT id, ts as time, strategy, market_id, question,
                   side, price, size, cost,
                   is_paper, status, pnl, resolved_at, notes
            FROM trades
            WHERE status = 'open'
            ORDER BY id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def log_monitoring_snapshot(self, metrics: dict, alarms: list):
        """Monitoring metriklerini kaydet."""
        self._ensure_conn()
        import json as _json
        self.conn.execute(
            """
            INSERT INTO monitoring_metrics (
                ts, opp_detected, opp_executed, exec_rate,
                avg_latency_ms, avg_fill_quality, drawdown_pct,
                total_profit, alarms_json
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                self._now(),
                metrics.get("opportunities_per_min", 0),
                metrics.get("executions_per_min", 0),
                metrics.get("execution_rate_pct", 0),
                metrics.get("avg_latency_ms", 0),
                metrics.get("avg_fill_quality", 0),
                metrics.get("drawdown_pct", 0),
                metrics.get("total_profit", 0),
                _json.dumps(alarms, separators=(",", ":")),
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Arbitrage
    # ------------------------------------------------------------------

    def create_arb_cycle(
        self,
        *,
        symbol: str,
        market_id: str,
        question: str = "",
        mode: str = "paper",
        status: str = "waiting_fill",
        timeframe: str = "15m",
        entry_price_limit: float = 0.45,
        bailout_price: float = 0.72,
        cycle_budget_usd: float = 0.0,
        shares_target: float = 0.0,
        up_token_id: str = "",
        down_token_id: str = "",
        up_order_id: str = "",
        down_order_id: str = "",
        up_fill_shares: float = 0.0,
        down_fill_shares: float = 0.0,
        cost_usd: float = 0.0,
        payout_usd: float = 0.0,
        fees_usd: float = 0.0,
        pnl_usd: float = 0.0,
        lock_skipped: bool = False,
        lock_skip_reason: str = "",
        liquidity_reject: bool = False,
        spread_reject: bool = False,
        competition_score: float = 0.0,
        dynamic_price_suggested: float = 0.0,
        exit_reason: str = "",
        notes: str = "",
    ) -> int:
        self._ensure_conn()
        ts_open = self._now()
        ts_close = ts_open if status in {"resolved", "bailed", "expired"} else ""
        cur = self.conn.execute(
            """
            INSERT INTO arb_cycles (
                ts_open, ts_close, symbol, timeframe, market_id, question, mode, status,
                entry_price_limit, bailout_price, cycle_budget_usd, shares_target,
                up_token_id, down_token_id, up_order_id, down_order_id,
                up_fill_shares, down_fill_shares, cost_usd, payout_usd, fees_usd, pnl_usd,
                lock_skipped, lock_skip_reason, liquidity_reject, spread_reject,
                competition_score, dynamic_price_suggested, exit_reason, notes
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ts_open,
                ts_close,
                symbol,
                timeframe,
                market_id,
                question[:180],
                mode,
                status,
                float(entry_price_limit),
                float(bailout_price),
                float(cycle_budget_usd),
                float(shares_target),
                up_token_id,
                down_token_id,
                up_order_id,
                down_order_id,
                float(up_fill_shares),
                float(down_fill_shares),
                float(cost_usd),
                float(payout_usd),
                float(fees_usd),
                float(pnl_usd),
                1 if lock_skipped else 0,
                lock_skip_reason,
                1 if liquidity_reject else 0,
                1 if spread_reject else 0,
                float(competition_score),
                float(dynamic_price_suggested),
                exit_reason,
                notes,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def log_arb_cycle_reject(
        self,
        *,
        symbol: str,
        market_id: str,
        question: str,
        mode: str,
        reason: str,
        lock_skipped: bool = False,
        liquidity_reject: bool = False,
        spread_reject: bool = False,
        dynamic_price_suggested: float = 0.0,
        competition_score: float = 0.0,
    ) -> int:
        return self.create_arb_cycle(
            symbol=symbol,
            market_id=market_id,
            question=question,
            mode=mode,
            status="resolved",
            cycle_budget_usd=0.0,
            exit_reason=reason,
            notes=f"reject:{reason}",
            lock_skipped=lock_skipped,
            lock_skip_reason=reason if lock_skipped else "",
            liquidity_reject=liquidity_reject,
            spread_reject=spread_reject,
            dynamic_price_suggested=dynamic_price_suggested,
            competition_score=competition_score,
        )

    def get_arb_cycle(self, cycle_id: int) -> Optional[dict]:
        self._ensure_conn()
        row = self.conn.execute(
            "SELECT * FROM arb_cycles WHERE id=?",
            (cycle_id,),
        ).fetchone()
        return dict(row) if row else None

    def update_arb_cycle(self, cycle_id: int, **fields: Any):
        self._ensure_conn()
        if not fields:
            return

        allowed = {
            "ts_close",
            "status",
            "entry_price_limit",
            "bailout_price",
            "cycle_budget_usd",
            "shares_target",
            "up_order_id",
            "down_order_id",
            "up_fill_shares",
            "down_fill_shares",
            "cost_usd",
            "payout_usd",
            "fees_usd",
            "pnl_usd",
            "lock_skipped",
            "lock_skip_reason",
            "liquidity_reject",
            "spread_reject",
            "competition_score",
            "dynamic_price_suggested",
            "exit_reason",
            "notes",
        }

        updates: Dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in {"lock_skipped", "liquidity_reject", "spread_reject"}:
                updates[key] = 1 if bool(value) else 0
            else:
                updates[key] = value

        if not updates:
            return

        clause = ", ".join(f"{k}=?" for k in updates)
        params = tuple(updates.values()) + (cycle_id,)
        self.conn.execute(f"UPDATE arb_cycles SET {clause} WHERE id=?", params)
        self.conn.commit()

    def close_arb_cycle(
        self,
        cycle_id: int,
        *,
        status: str = "resolved",
        exit_reason: str = "",
        pnl_usd: Optional[float] = None,
        cost_usd: Optional[float] = None,
        payout_usd: Optional[float] = None,
        fees_usd: Optional[float] = None,
        notes_append: str = "",
    ):
        self._ensure_conn()
        row = self.conn.execute(
            "SELECT notes FROM arb_cycles WHERE id=?",
            (cycle_id,),
        ).fetchone()

        notes = ""
        if row:
            current = row[0] or ""
            notes = f"{current} | {notes_append}" if current and notes_append else (notes_append or current)

        fields: Dict[str, Any] = {
            "status": status,
            "ts_close": self._now(),
        }
        if exit_reason:
            fields["exit_reason"] = exit_reason
        if pnl_usd is not None:
            fields["pnl_usd"] = float(pnl_usd)
        if cost_usd is not None:
            fields["cost_usd"] = float(cost_usd)
        if payout_usd is not None:
            fields["payout_usd"] = float(payout_usd)
        if fees_usd is not None:
            fields["fees_usd"] = float(fees_usd)
        if row is not None:
            fields["notes"] = notes
        self.update_arb_cycle(cycle_id, **fields)

    def open_arb_cycles(self) -> list:
        self._ensure_conn()
        rows = self.conn.execute(
            """
            SELECT *
            FROM arb_cycles
            WHERE status IN ('waiting_fill', 'paired', 'partial_unhedged')
            ORDER BY id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_arb_cycles(self, limit: int = 100) -> list:
        self._ensure_conn()
        rows = self.conn.execute(
            """
            SELECT *
            FROM arb_cycles
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def arb_open_cycles_count(self) -> int:
        self._ensure_conn()
        row = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM arb_cycles
            WHERE status IN ('waiting_fill', 'paired', 'partial_unhedged')
            """
        ).fetchone()
        return int(row[0] if row else 0)

    def arb_active_capital_usd(self) -> float:
        self._ensure_conn()
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(cycle_budget_usd),0)
            FROM arb_cycles
            WHERE status IN ('waiting_fill', 'paired', 'partial_unhedged')
            """
        ).fetchone()
        return float(row[0] if row else 0.0)

    def log_arb_order_event(
        self,
        *,
        cycle_id: int,
        symbol: str = "",
        market_id: str = "",
        side: str = "",
        action: str = "",
        order_id: str = "",
        price: float = 0.0,
        size: float = 0.0,
        filled_size: float = 0.0,
        remaining_size: float = 0.0,
        state: str = "",
        message: str = "",
    ) -> int:
        self._ensure_conn()
        cur = self.conn.execute(
            """
            INSERT INTO arb_order_events (
                ts, cycle_id, symbol, market_id, side, action, order_id,
                price, size, filled_size, remaining_size, state, message
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                self._now(),
                int(cycle_id),
                symbol,
                market_id,
                side,
                action,
                order_id,
                float(price),
                float(size),
                float(filled_size),
                float(remaining_size),
                state,
                message[:300],
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_arb_events(self, limit: int = 200, cycle_id: Optional[int] = None) -> list:
        self._ensure_conn()
        if cycle_id:
            rows = self.conn.execute(
                """
                SELECT *
                FROM arb_order_events
                WHERE cycle_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(cycle_id), int(limit)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT *
                FROM arb_order_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def log_arb_runtime_snapshot(self, payload: dict):
        self._ensure_conn()
        self.conn.execute(
            """
            INSERT INTO arb_runtime_snapshots (ts, payload_json)
            VALUES (?,?)
            """,
            (self._now(), json.dumps(payload or {}, separators=(",", ":"))),
        )
        self.conn.commit()

    def latest_arb_runtime_snapshot(self) -> dict:
        self._ensure_conn()
        row = self.conn.execute(
            """
            SELECT ts, payload_json
            FROM arb_runtime_snapshots
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return {}
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}
        payload["ts"] = row["ts"]
        return payload

    def recent_arb_runtime_snapshots(self, limit: int = 60) -> list:
        self._ensure_conn()
        rows = self.conn.execute(
            """
            SELECT ts, payload_json
            FROM arb_runtime_snapshots
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except Exception:
                payload = {}
            payload["ts"] = r["ts"]
            out.append(payload)
        out.reverse()
        return out

    def has_open_directional_position_for_market(self, market_id: str) -> bool:
        self._ensure_conn()
        if not market_id:
            return False
        row = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM trades
            WHERE status='open'
              AND market_id=?
              AND strategy NOT LIKE 'arb_%'
            """,
            (market_id,),
        ).fetchone()
        return bool(row and row[0] > 0)

    def arb_summary(self, window_days: int = 7) -> dict:
        self._ensure_conn()
        since = f"-{int(window_days)} days"
        row = self.conn.execute(
            """
            SELECT
                COUNT(*) AS total_cycles,
                COALESCE(SUM(CASE WHEN status='resolved' THEN 1 ELSE 0 END),0) AS resolved_cycles,
                COALESCE(SUM(CASE WHEN up_fill_shares > 0 AND down_fill_shares > 0 THEN 1 ELSE 0 END),0) AS paired_cycles,
                COALESCE(SUM(CASE WHEN status='bailed' THEN 1 ELSE 0 END),0) AS bailed_cycles,
                COALESCE(SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END),0) AS expired_cycles,
                COALESCE(SUM(CASE WHEN lock_skipped=1 THEN 1 ELSE 0 END),0) AS lock_skips,
                COALESCE(SUM(CASE WHEN liquidity_reject=1 THEN 1 ELSE 0 END),0) AS liquidity_rejects,
                COALESCE(SUM(CASE WHEN spread_reject=1 THEN 1 ELSE 0 END),0) AS spread_rejects,
                COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END),0) AS winning_cycles,
                COALESCE(SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END),0) AS losing_cycles,
                COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN pnl_usd ELSE 0 END),0) AS total_profit_usd,
                COALESCE(SUM(CASE WHEN pnl_usd < 0 THEN pnl_usd ELSE 0 END),0) AS total_loss_usd,
                COALESCE(SUM(pnl_usd),0) AS net_pnl_usd
            FROM arb_cycles
            WHERE julianday(ts_open) >= julianday('now', ?)
            """,
            (since,),
        ).fetchone()
        data = dict(row) if row else {}

        attempts = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM arb_cycles
            WHERE julianday(ts_open) >= julianday('now', ?)
              AND lock_skipped=0
              AND liquidity_reject=0
              AND spread_reject=0
              AND cycle_budget_usd > 0
            """,
            (since,),
        ).fetchone()
        attempts_count = float(attempts[0] if attempts else 0.0)
        paired = float(data.get("paired_cycles", 0) or 0)
        bailed = float(data.get("bailed_cycles", 0) or 0)

        open_cycles = self.arb_open_cycles_count()
        active_capital = self.arb_active_capital_usd()

        data["window_days"] = int(window_days)
        data["attempted_cycles"] = int(attempts_count)
        data["fill_rate"] = (paired / attempts_count) if attempts_count > 0 else 0.0
        data["bailout_rate"] = (bailed / attempts_count) if attempts_count > 0 else 0.0
        data["open_cycles"] = open_cycles
        data["active_capital_usd"] = round(active_capital, 4)
        return data

    def arb_telemetry(self, window_cycles: int = 200) -> dict:
        self._ensure_conn()
        rows = self.conn.execute(
            """
            SELECT *
            FROM arb_cycles
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(window_cycles),),
        ).fetchall()
        cycles = [dict(r) for r in rows]

        total = len(cycles)
        attempts = [
            c
            for c in cycles
            if not c.get("lock_skipped") and not c.get("liquidity_reject") and not c.get("spread_reject")
            and (c.get("cycle_budget_usd") or 0) > 0
        ]
        paired = [c for c in attempts if c.get("status") in {"paired", "resolved"} and c.get("up_fill_shares", 0) > 0 and c.get("down_fill_shares", 0) > 0]
        bailed = [c for c in attempts if c.get("status") == "bailed"]

        fill_durations = []
        for c in paired:
            ts_open = c.get("ts_open") or ""
            ts_close = c.get("ts_close") or ""
            if not ts_open or not ts_close:
                continue
            try:
                start = datetime.fromisoformat(ts_open.replace("Z", "+00:00")).timestamp()
                end = datetime.fromisoformat(ts_close.replace("Z", "+00:00")).timestamp()
                if end >= start:
                    fill_durations.append(end - start)
            except Exception:
                continue

        suggested_prices = [float(c.get("dynamic_price_suggested") or 0) for c in cycles if float(c.get("dynamic_price_suggested") or 0) > 0]
        trend = []
        for snap in self.recent_arb_runtime_snapshots(limit=48):
            trend.append(
                {
                    "ts": snap.get("ts"),
                    "fill_rate": float(snap.get("fill_rate", 0) or 0),
                    "bailout_rate": float(snap.get("bailout_rate", 0) or 0),
                    "suggested_price": float(snap.get("suggested_price", 0) or 0),
                }
            )

        attempts_count = len(attempts)
        return {
            "window_cycles": int(window_cycles),
            "total_cycles": total,
            "attempted_cycles": attempts_count,
            "fill_rate": (len(paired) / attempts_count) if attempts_count else 0.0,
            "bailout_rate": (len(bailed) / attempts_count) if attempts_count else 0.0,
            "liquidity_reject_rate": (sum(1 for c in cycles if c.get("liquidity_reject")) / total) if total else 0.0,
            "spread_reject_rate": (sum(1 for c in cycles if c.get("spread_reject")) / total) if total else 0.0,
            "lock_skip_rate": (sum(1 for c in cycles if c.get("lock_skipped")) / total) if total else 0.0,
            "avg_fill_time_sec": (sum(fill_durations) / len(fill_durations)) if fill_durations else 0.0,
            "competition_score_avg": (
                sum(float(c.get("competition_score") or 0) for c in cycles) / total
            ) if total else 0.0,
            "suggested_price": (sum(suggested_prices) / len(suggested_prices)) if suggested_prices else 0.0,
            "trend": trend,
        }

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    def log_trade(
        self,
        strategy: str,
        market_id: str,
        question: str,
        side: str,
        price: float,
        size: float,
        is_paper: bool = True,
        notes: str = "",
        expected_fee_usd: float = 0.0,
        expected_slippage_usd: float = 0.0,
        expected_net_usd: float = 0.0,
        expected_net_pct: float = 0.0,
        predicted_edge: float = 0.0,
        model_version: str = "",
        decision_reason: str = "",
        expected_gas_usd: float = 0.0,
        entry_latency_ms: float = 0.0,
        fill_ratio: float = 1.0,
        reject_reason: str = "",
        execution_id: str = "",
        condition_id: str = "",
    ) -> int:
        self._ensure_conn()
        cost = price * size
        cur = self.conn.execute(
            """
            INSERT INTO trades (
                ts, strategy, market_id, question, side, price, size, cost,
                is_paper, notes,
                expected_fee_usd, expected_slippage_usd,
                expected_net_usd, expected_net_pct,
                predicted_edge, model_version, decision_reason,
                expected_gas_usd, entry_latency_ms, fill_ratio,
                reject_reason, execution_id, condition_id
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                self._now(), strategy, market_id, question, side, price, size, cost,
                is_paper, notes,
                expected_fee_usd, expected_slippage_usd,
                expected_net_usd, expected_net_pct,
                predicted_edge, model_version, decision_reason,
                expected_gas_usd, entry_latency_ms, fill_ratio,
                reject_reason, execution_id, condition_id,
            ),
        )
        self.conn.commit()
        tid = cur.lastrowid
        mode = "PAPER" if is_paper else "LIVE"
        log.info(
            f"[{mode}] Trade #{tid}: {strategy} {side} {size:.6f}@{price:.6f} "
            f"notional=${cost:.2f} expected_net=${expected_net_usd:.2f}"
        )
        return tid

    def resolve_trade(
        self,
        trade_id: int,
        pnl: float,
        exit_reason: str = "",
        realized_fee_usd: float = None,
        realized_net_pct: float = None,
        borrow_fee_usd: float = None,
        realized_slippage_usd: float = None,
        realized_gas_usd: float = None,
        fill_ratio: float = None,
        reject_reason: str = "",
        gross_pnl: float = None,
        notes_append: str = "",
    ):
        self._ensure_conn()
        row = self.conn.execute(
            "SELECT notes FROM trades WHERE id=?", (trade_id,)
        ).fetchone()

        updated_notes = ""
        if row:
            current_notes = row[0] or ""
            if notes_append:
                updated_notes = f"{current_notes} | {notes_append}" if current_notes else notes_append
            else:
                updated_notes = current_notes

        fields = {
            "status": "resolved",
            "pnl": pnl,
            "resolved_at": self._now(),
        }

        if exit_reason:
            fields["exit_reason"] = exit_reason
        if realized_fee_usd is not None:
            fields["realized_fee_usd"] = realized_fee_usd
        if realized_net_pct is not None:
            fields["realized_net_pct"] = realized_net_pct
        if borrow_fee_usd is not None:
            fields["borrow_fee_usd"] = borrow_fee_usd
        if realized_slippage_usd is not None:
            fields["realized_slippage_usd"] = realized_slippage_usd
        if realized_gas_usd is not None:
            fields["realized_gas_usd"] = realized_gas_usd
        if fill_ratio is not None:
            fields["fill_ratio"] = fill_ratio
        if reject_reason:
            fields["reject_reason"] = reject_reason
        if gross_pnl is not None:
            fields["gross_pnl"] = gross_pnl
        if row is not None:
            fields["notes"] = updated_notes

        set_clause = ", ".join(f"{k}=?" for k in fields)
        params = tuple(fields.values()) + (trade_id,)
        self.conn.execute(f"UPDATE trades SET {set_clause} WHERE id=?", params)
        self.conn.commit()

    def update_trade_status(self, trade_id: int, status: str, sold_price: float = 0.0):
        """Trade'i sold olarak isaretle (profit taking / early exit).
        PnL = (sold_price - entry_price) * shares seklinde hesaplanir.
        """
        self._ensure_conn()
        row = self.conn.execute(
            "SELECT price, size FROM trades WHERE id=?", (trade_id,)
        ).fetchone()
        if not row:
            log.warning(f"update_trade_status: trade #{trade_id} not found")
            return

        entry_price = row[0]
        shares = row[1]

        # Early exit PnL: (sell_price - buy_price) * shares
        pnl = round((sold_price - entry_price) * shares, 2) if sold_price > 0 else 0.0

        self.conn.execute(
            """UPDATE trades
               SET status=?, pnl=?, resolved_at=?, exit_reason=?
               WHERE id=?""",
            (status, pnl, self._now(), "profit_take", trade_id),
        )
        self.conn.commit()
        log.info(
            f"Trade #{trade_id} → {status}: entry={entry_price:.4f} "
            f"sold={sold_price:.4f} PnL=${pnl:+.2f}"
        )

    # ------------------------------------------------------------------
    # Decision Audit
    # ------------------------------------------------------------------

    def log_decision_audit(
        self,
        strategy: str,
        market_id: str = "",
        question: str = "",
        symbol: str = "",
        side: str = "",
        action: str = "scanned",
        decision_reason: str = "",
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
        data_quality: float = 0.0,
        meta: dict = None,
    ):
        self._ensure_conn()
        meta_json = json.dumps(meta or {}, separators=(",", ":"))
        self.conn.execute(
            """
            INSERT INTO decision_audit (
                ts, strategy, market_id, question, symbol, side, action,
                decision_reason, predicted_edge,
                expected_fee_usd, expected_slippage_usd,
                expected_net_usd, expected_net_pct,
                model_version, confidence, p_exec,
                win_prob, notional, data_quality, meta_json
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                self._now(), strategy, market_id, question, symbol, side, action,
                decision_reason, predicted_edge,
                expected_fee_usd, expected_slippage_usd,
                expected_net_usd, expected_net_pct,
                model_version, confidence, p_exec,
                win_prob, notional, data_quality, meta_json,
            ),
        )
        self.conn.commit()

    def recent_decisions(self, limit: int = 100) -> list:
        self._ensure_conn()
        rows = self.conn.execute(
            """
            SELECT id, ts AS time, strategy, market_id, question, symbol, side,
                   action, decision_reason, predicted_edge,
                   expected_fee_usd, expected_slippage_usd,
                   expected_net_usd, expected_net_pct,
                   model_version, confidence, p_exec, win_prob,
                   notional, data_quality, meta_json
            FROM decision_audit
            ORDER BY ts DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def log_execution_audit(
        self,
        strategy: str,
        execution_id: str = "",
        market_id: str = "",
        side: str = "",
        symbol: str = "",
        status: str = "",
        requested_notional: float = 0.0,
        filled_notional: float = 0.0,
        fill_ratio: float = 0.0,
        estimated_latency_ms: float = 0.0,
        actual_latency_ms: float = 0.0,
        estimated_fee_usd: float = 0.0,
        estimated_slippage_usd: float = 0.0,
        estimated_gas_usd: float = 0.0,
        reject_reason: str = "",
        meta: dict = None,
    ):
        self._ensure_conn()
        self.conn.execute(
            """
            INSERT INTO execution_audit (
                ts, strategy, execution_id, market_id, side, symbol, status,
                requested_notional, filled_notional, fill_ratio,
                estimated_latency_ms, actual_latency_ms,
                estimated_fee_usd, estimated_slippage_usd, estimated_gas_usd,
                reject_reason, meta_json
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                self._now(),
                strategy,
                execution_id,
                market_id,
                side,
                symbol,
                status,
                requested_notional,
                filled_notional,
                fill_ratio,
                estimated_latency_ms,
                actual_latency_ms,
                estimated_fee_usd,
                estimated_slippage_usd,
                estimated_gas_usd,
                reject_reason,
                json.dumps(meta or {}, separators=(",", ":")),
            ),
        )
        self.conn.commit()

    def recent_execution_audit(self, limit: int = 200) -> list:
        self._ensure_conn()
        rows = self.conn.execute(
            """
            SELECT id, ts AS time, strategy, execution_id, market_id, side, symbol,
                   status, requested_notional, filled_notional, fill_ratio,
                   estimated_latency_ms, actual_latency_ms,
                   estimated_fee_usd, estimated_slippage_usd, estimated_gas_usd,
                   reject_reason, meta_json
            FROM execution_audit
            ORDER BY ts DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def execution_quality_summary(self, days: int = 30) -> dict:
        self._ensure_conn()
        since = f"-{int(days)} days"

        overall = self.conn.execute(
            """
            SELECT COUNT(*) AS attempts,
                   COALESCE(SUM(CASE WHEN status='filled' THEN 1 ELSE 0 END),0) AS filled_count,
                   COALESCE(AVG(fill_ratio),0) AS avg_fill_ratio,
                   COALESCE(AVG(actual_latency_ms),0) AS avg_latency_ms,
                   COALESCE(AVG(estimated_latency_ms),0) AS avg_estimated_latency_ms,
                   COALESCE(SUM(estimated_fee_usd),0) AS est_fee_usd,
                   COALESCE(SUM(estimated_slippage_usd),0) AS est_slippage_usd,
                   COALESCE(SUM(estimated_gas_usd),0) AS est_gas_usd
            FROM execution_audit
            WHERE julianday(ts) >= julianday('now', ?)
            """,
            (since,),
        ).fetchone()

        by_status = self.conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM execution_audit
            WHERE julianday(ts) >= julianday('now', ?)
            GROUP BY status
            ORDER BY count DESC
            """,
            (since,),
        ).fetchall()

        by_strategy = self.conn.execute(
            """
            SELECT strategy,
                   COUNT(*) AS attempts,
                   COALESCE(SUM(CASE WHEN status='filled' THEN 1 ELSE 0 END),0) AS filled_count,
                   COALESCE(AVG(fill_ratio),0) AS avg_fill_ratio,
                   COALESCE(AVG(actual_latency_ms),0) AS avg_latency_ms
            FROM execution_audit
            WHERE julianday(ts) >= julianday('now', ?)
            GROUP BY strategy
            ORDER BY attempts DESC
            """,
            (since,),
        ).fetchall()

        o = dict(overall) if overall else {}
        attempts = float(o.get("attempts", 0) or 0)
        filled = float(o.get("filled_count", 0) or 0)
        fill_rate = (filled / attempts) if attempts > 0 else 0.0

        return {
            "window_days": int(days),
            "overall": {**o, "fill_rate": fill_rate},
            "by_status": [dict(r) for r in by_status],
            "by_strategy": [dict(r) for r in by_strategy],
        }

    # ------------------------------------------------------------------
    # Opportunities
    # ------------------------------------------------------------------

    def log_opportunity(
        self,
        strategy: str,
        market_id: str = "",
        question: str = "",
        edge: float = 0,
        yes_price: float = 0,
        no_price: float = 0,
        price_sum: float = 0,
        binance_price: float = 0,
        action: str = "logged",
        notes: str = "",
    ):
        self._ensure_conn()
        self.conn.execute(
            """
            INSERT INTO opportunities (
                ts, strategy, market_id, question,
                edge, yes_price, no_price, price_sum,
                binance_price, action, notes
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                self._now(), strategy, market_id, question,
                edge, yes_price, no_price, price_sum,
                binance_price, action, notes,
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def diagnostics_cost_breakdown(self, days: int = 30) -> dict:
        self._ensure_conn()
        since = f"-{int(days)} days"

        totals = self.conn.execute(
            """
            SELECT COUNT(*) AS trades,
                   COALESCE(SUM(expected_fee_usd),0) AS expected_fee_usd,
                   COALESCE(SUM(realized_fee_usd),0) AS realized_fee_usd,
                   COALESCE(SUM(expected_slippage_usd),0) AS expected_slippage_usd,
                   COALESCE(SUM(realized_slippage_usd),0) AS realized_slippage_usd,
                   COALESCE(SUM(borrow_fee_usd),0) AS borrow_fee_usd,
                   COALESCE(SUM(expected_gas_usd),0) AS expected_gas_usd,
                   COALESCE(SUM(realized_gas_usd),0) AS realized_gas_usd,
                   COALESCE(SUM(gross_pnl),0) AS gross_pnl,
                   COALESCE(SUM(pnl),0) AS net_pnl
            FROM trades
            WHERE status='resolved'
              AND julianday(ts) >= julianday('now', ?)
            """,
            (since,),
        ).fetchone()

        by_strategy = self.conn.execute(
            """
            SELECT strategy,
                   COUNT(*) AS trades,
                   COALESCE(SUM(expected_fee_usd),0) AS expected_fee_usd,
                   COALESCE(SUM(realized_fee_usd),0) AS realized_fee_usd,
                   COALESCE(SUM(expected_slippage_usd),0) AS expected_slippage_usd,
                   COALESCE(SUM(realized_slippage_usd),0) AS realized_slippage_usd,
                   COALESCE(SUM(borrow_fee_usd),0) AS borrow_fee_usd,
                   COALESCE(SUM(expected_gas_usd),0) AS expected_gas_usd,
                   COALESCE(SUM(realized_gas_usd),0) AS realized_gas_usd,
                   COALESCE(SUM(gross_pnl),0) AS gross_pnl,
                   COALESCE(SUM(pnl),0) AS net_pnl
            FROM trades
            WHERE status='resolved'
              AND julianday(ts) >= julianday('now', ?)
            GROUP BY strategy
            ORDER BY net_pnl DESC
            """,
            (since,),
        ).fetchall()

        return {
            "window_days": int(days),
            "totals": dict(totals) if totals else {},
            "by_strategy": [dict(r) for r in by_strategy],
        }

    def diagnostics_edge_drift(self, days: int = 30) -> dict:
        self._ensure_conn()
        since = f"-{int(days)} days"
        rows = self.conn.execute(
            """
            SELECT strategy,
                   COUNT(*) AS trades,
                   COALESCE(AVG(predicted_edge),0) AS avg_predicted_edge,
                   COALESCE(AVG(expected_net_pct),0) AS avg_expected_net_pct,
                   COALESCE(AVG(realized_net_pct),0) AS avg_realized_net_pct,
                   COALESCE(AVG(expected_net_usd),0) AS avg_expected_net_usd,
                   COALESCE(AVG(pnl),0) AS avg_realized_net_usd,
                   COALESCE(AVG(realized_net_pct - expected_net_pct),0) AS avg_drift_pct,
                   COALESCE(AVG(pnl - expected_net_usd),0) AS avg_drift_usd,
                   COALESCE(AVG(ABS(pnl - expected_net_usd)),0) AS mae_usd
            FROM trades
            WHERE status='resolved'
              AND julianday(ts) >= julianday('now', ?)
            GROUP BY strategy
            ORDER BY trades DESC
            """,
            (since,),
        ).fetchall()

        overall = self.conn.execute(
            """
            SELECT COUNT(*) AS trades,
                   COALESCE(AVG(expected_net_pct),0) AS avg_expected_net_pct,
                   COALESCE(AVG(realized_net_pct),0) AS avg_realized_net_pct,
                   COALESCE(AVG(expected_net_usd),0) AS avg_expected_net_usd,
                   COALESCE(AVG(pnl),0) AS avg_realized_net_usd,
                   COALESCE(AVG(realized_net_pct - expected_net_pct),0) AS avg_drift_pct,
                   COALESCE(AVG(pnl - expected_net_usd),0) AS avg_drift_usd,
                   COALESCE(AVG(ABS(pnl - expected_net_usd)),0) AS mae_usd
            FROM trades
            WHERE status='resolved'
              AND julianday(ts) >= julianday('now', ?)
            """,
            (since,),
        ).fetchone()

        return {
            "window_days": int(days),
            "overall": dict(overall) if overall else {},
            "by_strategy": [dict(r) for r in rows],
        }

    def validation_gates(
        self,
        window_days: int = 30,
        bankroll: float = 10_000.0,
        min_trades: int = 100,
        min_win_rate_pct: float = 80.0,
        min_return_pct: float = 30.0,
        max_drawdown_pct: float = 10.0,
        min_profit_factor: float = 1.8,
        min_median_net_trade_usd: float = 3.0,
    ) -> dict:
        self._ensure_conn()
        since = f"-{int(window_days)} days"

        strategies = self.conn.execute(
            """
            SELECT DISTINCT strategy
            FROM trades
            WHERE status='resolved'
              AND julianday(ts) >= julianday('now', ?)
            ORDER BY strategy
            """,
            (since,),
        ).fetchall()

        result = []
        for row in strategies:
            strategy = row[0]
            trades_rows = self.conn.execute(
                """
                SELECT pnl
                FROM trades
                WHERE status='resolved' AND strategy=?
                  AND julianday(ts) >= julianday('now', ?)
                ORDER BY ts ASC
                """,
                (strategy, since),
            ).fetchall()
            pnls = [r[0] for r in trades_rows]
            if not pnls:
                continue

            trades = len(pnls)
            wins = sum(1 for x in pnls if x > 0)
            losses = sum(1 for x in pnls if x < 0)
            gross_win = sum(x for x in pnls if x > 0)
            gross_loss = abs(sum(x for x in pnls if x < 0))
            total_pnl = sum(pnls)
            win_rate = (wins / trades) * 100 if trades else 0.0
            ret_pct = (total_pnl / bankroll) * 100 if bankroll else 0.0
            profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
            median_trade = median(pnls)

            peak = 0.0
            cum = 0.0
            max_dd = 0.0
            for x in pnls:
                cum += x
                if cum > peak:
                    peak = cum
                dd = peak - cum
                if dd > max_dd:
                    max_dd = dd
            max_dd_pct = (max_dd / bankroll) * 100 if bankroll else 0.0

            gates = {
                "min_trades": trades >= min_trades,
                "win_rate": win_rate >= min_win_rate_pct,
                "return": ret_pct >= min_return_pct,
                "max_drawdown": max_dd_pct <= max_drawdown_pct,
                "profit_factor": profit_factor >= min_profit_factor,
                "median_net_trade": median_trade >= min_median_net_trade_usd,
            }

            result.append(
                {
                    "strategy": strategy,
                    "window_days": int(window_days),
                    "trades": trades,
                    "wins": wins,
                    "losses": losses,
                    "win_rate_pct": round(win_rate, 2),
                    "return_pct": round(ret_pct, 2),
                    "max_drawdown_pct": round(max_dd_pct, 2),
                    "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else None,
                    "median_net_trade_usd": round(float(median_trade), 2),
                    "pass": all(gates.values()),
                    "gates": gates,
                }
            )

        return {
            "window_days": int(window_days),
            "targets": {
                "min_trades": min_trades,
                "min_win_rate_pct": min_win_rate_pct,
                "min_return_pct": min_return_pct,
                "max_drawdown_pct": max_drawdown_pct,
                "min_profit_factor": min_profit_factor,
                "min_median_net_trade_usd": min_median_net_trade_usd,
            },
            "strategies": result,
        }

    def log_validation_snapshot(self, snapshot: dict, window_days: int = 30):
        self._ensure_conn()
        self.conn.execute(
            """
            INSERT INTO validation_audit (ts, window_days, payload_json)
            VALUES (?,?,?)
            """,
            (
                self._now(),
                int(window_days),
                json.dumps(snapshot, separators=(",", ":")),
            ),
        )
        self.conn.commit()

    def latest_validation_snapshots(self, limit: int = 20) -> list:
        self._ensure_conn()
        rows = self.conn.execute(
            """
            SELECT id, ts AS time, window_days, payload_json
            FROM validation_audit
            ORDER BY ts DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload_json") or "{}")
            except Exception:
                d["payload"] = {}
            out.append(d)
        return out

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def daily_pnl(self, date: str = None) -> float:
        self._ensure_conn()
        if not date:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        r = self.conn.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE DATE(ts)=? AND status='resolved'",
            (date,),
        ).fetchone()
        return r[0]

    def consecutive_losses(self) -> int:
        self._ensure_conn()
        rows = self.conn.execute(
            "SELECT pnl FROM trades WHERE status='resolved' ORDER BY ts DESC LIMIT 50"
        ).fetchall()
        count = 0
        for r in rows:
            if r[0] < 0:
                count += 1
            else:
                break
        return count

    def today_stats(self) -> dict:
        self._ensure_conn()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        total_row = self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE DATE(ts)=?", (today,)
        ).fetchone()
        total = total_row[0] or 0

        r = self.conn.execute(
            """
            SELECT SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as w,
                   SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END) as l,
                   COALESCE(SUM(pnl),0) as pnl,
                   COALESCE(MAX(pnl),0) as best,
                   COALESCE(MIN(pnl),0) as worst
            FROM trades
            WHERE DATE(ts)=? AND status='resolved'
            """,
            (today,),
        ).fetchone()

        wins = r[0] or 0
        return {
            "trades": total,
            "wins": wins,
            "losses": r[1] or 0,
            "win_rate": (wins / total * 100) if total else 0,
            "pnl": r[2],
            "best": r[3],
            "worst": r[4],
        }

    def recent_trades(self, limit: int = 50) -> list:
        self._ensure_conn()
        rows = self.conn.execute(
            """
            SELECT id, ts as time, strategy, market_id, question,
                   side, price, size, cost,
                   is_paper, status, pnl, resolved_at, notes,
                   expected_fee_usd, expected_slippage_usd,
                   expected_net_usd, expected_net_pct,
                   predicted_edge, model_version,
                   decision_reason, exit_reason,
                   realized_fee_usd, realized_net_pct,
                   borrow_fee_usd, realized_slippage_usd,
                   gross_pnl, expected_gas_usd, realized_gas_usd,
                   entry_latency_ms, fill_ratio, reject_reason, execution_id
            FROM trades
            ORDER BY ts DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_opportunities(self, limit: int = 50) -> list:
        self._ensure_conn()
        rows = self.conn.execute(
            """
            SELECT id, ts as time, strategy, market_id, question,
                   edge, yes_price, no_price, price_sum,
                   binance_price, action, notes
            FROM opportunities
            ORDER BY ts DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def strategy_stats(self) -> list:
        self._ensure_conn()
        rows = self.conn.execute(
            """
            SELECT strategy,
                   COUNT(*) as trades,
                   SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END) as losses,
                   COALESCE(SUM(pnl),0) as pnl,
                   COALESCE(AVG(pnl),0) as avg_pnl
            FROM trades
            WHERE status='resolved'
            GROUP BY strategy
            ORDER BY pnl DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def total_trades_count(self) -> int:
        self._ensure_conn()
        r = self.conn.execute("SELECT COUNT(*) FROM trades").fetchone()
        return r[0] if r else 0

    # ------------------------------------------------------------------
    # Redeem
    # ------------------------------------------------------------------

    def unredeemed_winning_trades(self) -> list:
        """Resolved, winning, unredeemed trade'leri getir."""
        self._ensure_conn()
        rows = self.conn.execute(
            """
            SELECT id, market_id, condition_id, side, size, pnl, strategy
            FROM trades
            WHERE status = 'resolved'
              AND pnl > 0
              AND redeemed = 0
              AND is_paper = 0
              AND condition_id != ''
            ORDER BY id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_redeemed(self, trade_id: int, tx_hash: str = ""):
        """Trade'i redeem edildi olarak işaretle."""
        self._ensure_conn()
        self.conn.execute(
            "UPDATE trades SET redeemed = 1, notes = notes || ? WHERE id = ?",
            (f" | redeemed tx={tx_hash}" if tx_hash else " | redeemed", trade_id),
        )
        self.conn.commit()

    def pending_redeem_count(self) -> int:
        """Henüz redeem edilmemiş kazanan trade sayısı."""
        self._ensure_conn()
        r = self.conn.execute(
            """
            SELECT COUNT(*) FROM trades
            WHERE status = 'resolved' AND pnl > 0 AND redeemed = 0
              AND is_paper = 0 AND condition_id != ''
            """
        ).fetchone()
        return r[0] if r else 0

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def active_positions(self) -> list:
        self._ensure_conn()
        rows = self.conn.execute(
            """
            SELECT id, ts as time, strategy, market_id, question,
                   side, price, size, cost, notes,
                   expected_fee_usd, expected_slippage_usd,
                   expected_net_usd, expected_net_pct,
                   predicted_edge, model_version, decision_reason,
                   expected_gas_usd, entry_latency_ms, fill_ratio,
                   reject_reason, execution_id
            FROM trades
            WHERE status='open'
            ORDER BY ts DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def pnl_history(self, days: int = 30) -> list:
        self._ensure_conn()
        rows = self.conn.execute(
            """
            SELECT DATE(ts) as date, COALESCE(SUM(pnl),0) as pnl
            FROM trades
            WHERE status='resolved'
              AND ts >= date('now', ? || ' days')
            GROUP BY DATE(ts)
            ORDER BY date
            """,
            (f"-{days}",),
        ).fetchall()

        result = []
        cumulative = 0
        for r in rows:
            d = dict(r)
            cumulative += d["pnl"]
            d["cumulative"] = round(cumulative, 2)
            result.append(d)
        return result

    def open_positions_count(self) -> int:
        self._ensure_conn()
        r = self.conn.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()
        return r[0] if r else 0

    # ------------------------------------------------------------------
    # Strategy-filtered views
    # ------------------------------------------------------------------

    def today_stats_by_strategy(self, strategy: StrategyRef) -> dict:
        self._ensure_conn()
        op, values = self._strategy_clause(strategy)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        total_row = self.conn.execute(
            f"SELECT COUNT(*) FROM trades WHERE DATE(ts)=? AND strategy {op}",
            (today,) + values,
        ).fetchone()
        total = total_row[0] or 0

        row = self.conn.execute(
            f"""
            SELECT SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as w,
                   SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END) as l,
                   COALESCE(SUM(pnl),0) as pnl,
                   COALESCE(MAX(pnl),0) as best,
                   COALESCE(MIN(pnl),0) as worst
            FROM trades
            WHERE DATE(ts)=? AND status='resolved' AND strategy {op}
            """,
            (today,) + values,
        ).fetchone()

        wins = row[0] or 0
        return {
            "trades": total,
            "wins": wins,
            "losses": row[1] or 0,
            "win_rate": (wins / total * 100) if total else 0,
            "pnl": row[2],
            "best": row[3],
            "worst": row[4],
        }

    def recent_trades_by_strategy(self, strategy: StrategyRef, limit: int = 50) -> list:
        self._ensure_conn()
        op, values = self._strategy_clause(strategy)
        rows = self.conn.execute(
            f"""
            SELECT id, ts as time, strategy, market_id, question,
                   side, price, size, cost, is_paper, status,
                   pnl, resolved_at, notes,
                   expected_fee_usd, expected_slippage_usd,
                   expected_net_usd, expected_net_pct,
                   predicted_edge, model_version,
                   decision_reason, exit_reason,
                   realized_fee_usd, realized_net_pct,
                   borrow_fee_usd, realized_slippage_usd,
                   gross_pnl, expected_gas_usd, realized_gas_usd,
                   entry_latency_ms, fill_ratio, reject_reason, execution_id
            FROM trades
            WHERE strategy {op}
            ORDER BY ts DESC
            LIMIT ?
            """,
            values + (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def active_positions_by_strategy(self, strategy: StrategyRef) -> list:
        self._ensure_conn()
        op, values = self._strategy_clause(strategy)
        rows = self.conn.execute(
            f"""
            SELECT id, ts as time, strategy, market_id, question,
                   side, price, size, cost, notes,
                   expected_fee_usd, expected_slippage_usd,
                   expected_net_usd, expected_net_pct,
                   predicted_edge, model_version, decision_reason,
                   expected_gas_usd, entry_latency_ms, fill_ratio,
                   reject_reason, execution_id
            FROM trades
            WHERE status='open' AND strategy {op}
            ORDER BY ts DESC
            """,
            values,
        ).fetchall()
        return [dict(r) for r in rows]

    def pnl_history_by_strategy(self, strategy: StrategyRef, days: int = 30) -> list:
        self._ensure_conn()
        op, values = self._strategy_clause(strategy)
        rows = self.conn.execute(
            f"""
            SELECT DATE(ts) as date, COALESCE(SUM(pnl),0) as pnl
            FROM trades
            WHERE status='resolved' AND strategy {op}
              AND ts >= date('now', ? || ' days')
            GROUP BY DATE(ts)
            ORDER BY date
            """,
            values + (f"-{days}",),
        ).fetchall()

        result = []
        cumulative = 0
        for r in rows:
            d = dict(r)
            cumulative += d["pnl"]
            d["cumulative"] = round(cumulative, 2)
            result.append(d)
        return result

    def total_trades_by_strategy(self, strategy: StrategyRef) -> int:
        self._ensure_conn()
        op, values = self._strategy_clause(strategy)
        r = self.conn.execute(
            f"SELECT COUNT(*) FROM trades WHERE strategy {op}", values
        ).fetchone()
        return r[0] if r else 0

    def daily_pnl_by_strategy(self, strategy: StrategyRef, date: str = None) -> float:
        self._ensure_conn()
        op, values = self._strategy_clause(strategy)
        if not date:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        r = self.conn.execute(
            f"""
            SELECT COALESCE(SUM(pnl),0)
            FROM trades
            WHERE DATE(ts)=?
              AND status='resolved'
              AND strategy {op}
            """,
            (date,) + values,
        ).fetchone()
        return r[0]

    def consecutive_losses_by_strategy(self, strategy: StrategyRef) -> int:
        self._ensure_conn()
        op, values = self._strategy_clause(strategy)
        rows = self.conn.execute(
            f"""
            SELECT pnl
            FROM trades
            WHERE status='resolved' AND strategy {op}
            ORDER BY ts DESC
            LIMIT 50
            """,
            values,
        ).fetchall()

        count = 0
        for r in rows:
            if r[0] < 0:
                count += 1
            else:
                break
        return count
