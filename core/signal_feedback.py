"""Signal feedback and weight learning system."""

import json
import logging
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class SignalFeedback:

    def __init__(self, db_path: str = "trades.db"):
        self.db_path = db_path
        self.conn = None
        self._ensure_table()

    def _ensure_table(self):
        """Create signal_scores table if not exists."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=10.0)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signal_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id INTEGER,
                    signal_name TEXT,
                    signal_direction TEXT,
                    signal_score REAL,
                    signal_confidence REAL,
                    trade_direction TEXT,
                    trade_won INTEGER,
                    pnl REAL,
                    created_at TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_signal_scores_trade
                ON signal_scores(trade_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_signal_scores_name
                ON signal_scores(signal_name)
            """)
            conn.commit()
            conn.close()
            log.info("SignalFeedback table ready")
        except Exception as e:
            log.warning(f"SignalFeedback table init error: {e}")

    def _get_conn(self) -> sqlite3.Connection:
        if self.conn is None or self.conn is not None:
            # Always create fresh connection for thread safety
            return sqlite3.connect(self.db_path, timeout=10.0)
        return self.conn

    def record_signals(
        self,
        trade_id: int,
        signals: list[dict],
        direction: str,
    ):
        """Trade acildiginda sinyalleri kaydet."""
        try:
            conn = self._get_conn()
            now = datetime.now(timezone.utc).isoformat()
            for sig in signals:
                conn.execute(
                    """
                    INSERT INTO signal_scores (
                        trade_id, signal_name, signal_direction,
                        signal_score, signal_confidence,
                        trade_direction, trade_won, pnl, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                    """,
                    (
                        trade_id,
                        sig.get("name", "unknown"),
                        sig.get("direction", "neutral"),
                        sig.get("score", 0.0),
                        sig.get("confidence", 1.0),
                        direction,
                        now,
                    ),
                )
            conn.commit()
            conn.close()
            log.debug(f"Recorded {len(signals)} signals for trade #{trade_id}")
        except Exception as e:
            log.warning(f"Failed to record signals: {e}")

    def record_outcome(self, trade_id: int, won: bool, pnl: float):
        """Trade sonuclandiginda guncelle."""
        try:
            conn = self._get_conn()
            conn.execute(
                """
                UPDATE signal_scores
                SET trade_won = ?, pnl = ?
                WHERE trade_id = ?
                """,
                (1 if won else 0, pnl, trade_id),
            )
            conn.commit()
            conn.close()
            log.debug(f"Recorded outcome for trade #{trade_id}: won={won} pnl={pnl:.2f}")
        except Exception as e:
            log.warning(f"Failed to record outcome: {e}")

    def get_signal_accuracy(self, window: int = 50) -> dict:
        """Son N trade'de her sinyalin dogruluk orani."""
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row

            # Get distinct trade IDs with outcomes
            trade_ids = conn.execute(
                """
                SELECT DISTINCT trade_id FROM signal_scores
                WHERE trade_won IS NOT NULL
                ORDER BY trade_id DESC
                LIMIT ?
                """,
                (window,),
            ).fetchall()

            if not trade_ids:
                conn.close()
                return {}

            ids = [r[0] for r in trade_ids]
            placeholders = ",".join("?" for _ in ids)

            rows = conn.execute(
                f"""
                SELECT signal_name,
                       COUNT(*) as total,
                       SUM(CASE WHEN (
                           (signal_direction = trade_direction AND trade_won = 1)
                           OR (signal_direction != trade_direction AND trade_won = 0)
                       ) THEN 1 ELSE 0 END) as correct,
                       SUM(CASE WHEN signal_direction = trade_direction AND trade_won = 1
                           THEN 1 ELSE 0 END) as aligned_wins,
                       AVG(ABS(signal_score)) as avg_score
                FROM signal_scores
                WHERE trade_id IN ({placeholders}) AND trade_won IS NOT NULL
                GROUP BY signal_name
                """,
                ids,
            ).fetchall()

            result = {}
            for r in rows:
                row = dict(r)
                total = row["total"]
                correct = row["correct"]
                accuracy = correct / total if total > 0 else 0.0
                result[row["signal_name"]] = {
                    "total": total,
                    "correct": correct,
                    "accuracy": round(accuracy, 4),
                    "aligned_wins": row["aligned_wins"],
                    "avg_score": round(row["avg_score"], 4),
                }

            conn.close()
            return result
        except Exception as e:
            log.warning(f"Failed to get signal accuracy: {e}")
            return {}

    def suggest_weights(
        self,
        current_weights: dict,
        min_trades: int = 20,
        smoothing: float = 0.3,
    ) -> dict | None:
        """
        Accuracy'ye gore agirlik onerisi.
        - accuracy > 0.60 -> agirlik artir
        - accuracy < 0.45 -> agirlik azalt
        - Smoothing: new = old*0.7 + target*0.3
        - Min 20 trade data gerekli
        """
        accuracy = self.get_signal_accuracy(window=50)
        if not accuracy:
            return None

        # Check if enough data
        total_trades = set()
        for info in accuracy.values():
            # Can't know exact trade count per signal, use total
            if info["total"] < min_trades:
                return None

        new_weights = dict(current_weights)
        base_weight = 1.0 / max(len(current_weights), 1)

        for signal_name, info in accuracy.items():
            if signal_name not in current_weights:
                continue

            acc = info["accuracy"]
            old_w = current_weights[signal_name]

            if acc > 0.60:
                # Good signal, increase weight
                target = old_w * 1.3
            elif acc < 0.45:
                # Bad signal, decrease weight
                target = old_w * 0.7
            else:
                # Neutral, keep similar
                target = old_w

            # Clamp target
            target = max(0.02, min(0.50, target))

            # Smooth update
            new_w = old_w * (1 - smoothing) + target * smoothing
            new_weights[signal_name] = new_w

        # Normalize
        total = sum(new_weights.values())
        if total > 0:
            for k in new_weights:
                new_weights[k] = round(new_weights[k] / total, 4)

        # Log changes
        changes = []
        for k in new_weights:
            if k in current_weights:
                diff = new_weights[k] - current_weights[k]
                if abs(diff) > 0.005:
                    changes.append(f"{k}: {current_weights[k]:.3f}->{new_weights[k]:.3f}")
        if changes:
            log.info(f"Weight suggestions: {', '.join(changes)}")

        return new_weights

    def get_summary(self) -> dict:
        """Dashboard icin ozet bilgi."""
        accuracy = self.get_signal_accuracy(window=50)
        try:
            conn = self._get_conn()
            total = conn.execute(
                "SELECT COUNT(DISTINCT trade_id) FROM signal_scores WHERE trade_won IS NOT NULL"
            ).fetchone()[0]
            conn.close()
        except Exception:
            total = 0

        return {
            "total_trades_with_signals": total,
            "signal_accuracy": accuracy,
        }
