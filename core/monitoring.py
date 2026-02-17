"""PDF s.36: Real-time monitoring dashboard metrikleri."""

import logging
import time
from collections import deque

log = logging.getLogger(__name__)

# Keep 10 minutes of data
MAX_RECORDS = 600


class MonitoringEngine:

    def __init__(self, config=None):
        self.cfg = config
        self._opportunities = deque(maxlen=MAX_RECORDS)
        self._latencies = deque(maxlen=MAX_RECORDS)
        self._fill_qualities = deque(maxlen=MAX_RECORDS)
        self._pnl_history = deque(maxlen=MAX_RECORDS)
        self._total_profit = 0.0
        self._peak_balance = 0.0

    def record_opportunity(
        self,
        executed: bool,
        latency_ms: float = 0.0,
        fill_quality: float = 0.0,
        reason: str = "",
    ):
        """Her firsat/trade icin metrik kaydet."""
        ts = time.time()
        self._opportunities.append({
            "ts": ts,
            "executed": executed,
            "reason": reason,
        })
        if executed:
            if latency_ms > 0:
                self._latencies.append({"ts": ts, "ms": latency_ms})
            if fill_quality != 0:
                self._fill_qualities.append({"ts": ts, "quality": fill_quality})

    def record_pnl(self, pnl: float):
        """Trade sonucu kaydet, drawdown hesapla."""
        ts = time.time()
        self._total_profit += pnl
        self._pnl_history.append({"ts": ts, "pnl": pnl})
        if self._total_profit > self._peak_balance:
            self._peak_balance = self._total_profit

    @property
    def metrics(self) -> dict:
        """Son 1 dakikadaki metrikleri hesapla."""
        now = time.time()
        cutoff_1m = now - 60

        recent_opps = [o for o in self._opportunities if o["ts"] >= cutoff_1m]
        detected = len(recent_opps)
        executed = sum(1 for o in recent_opps if o["executed"])

        recent_lat = [l for l in self._latencies if l["ts"] >= cutoff_1m]
        avg_latency = sum(l["ms"] for l in recent_lat) / len(recent_lat) if recent_lat else 0.0

        recent_fq = [f for f in self._fill_qualities if f["ts"] >= cutoff_1m]
        avg_fq = sum(f["quality"] for f in recent_fq) / len(recent_fq) if recent_fq else 0.0

        # Drawdown
        drawdown_usd = self._peak_balance - self._total_profit
        drawdown_pct = (drawdown_usd / self._peak_balance * 100) if self._peak_balance > 0 else 0.0

        return {
            "opportunities_per_min": detected,
            "executions_per_min": executed,
            "execution_rate_pct": (executed / detected * 100) if detected > 0 else 0.0,
            "total_profit": round(self._total_profit, 2),
            "drawdown_pct": round(drawdown_pct, 2),
            "drawdown_usd": round(drawdown_usd, 2),
            "avg_latency_ms": round(avg_latency, 1),
            "avg_fill_quality": round(avg_fq, 3),
            "peak_balance": round(self._peak_balance, 2),
        }

    def check_alarms(self) -> list:
        """PDF alarmlari kontrol et."""
        alarms = []
        m = self.metrics

        max_dd = self.cfg.MAX_DRAWDOWN_PCT if self.cfg else 15.0
        min_exec = self.cfg.MIN_EXECUTION_RATE_PCT if self.cfg else 30.0

        if m["drawdown_pct"] > max_dd:
            alarms.append(f"DRAWDOWN: {m['drawdown_pct']:.1f}% > {max_dd}%")

        if m["opportunities_per_min"] > 0 and m["execution_rate_pct"] < min_exec:
            alarms.append(f"LOW_EXEC_RATE: {m['execution_rate_pct']:.1f}% < {min_exec}%")

        # Fill failure spike: son 5dk'da >20% basarisiz
        now = time.time()
        cutoff_5m = now - 300
        recent_5m = [o for o in self._opportunities if o["ts"] >= cutoff_5m]
        if len(recent_5m) >= 5:
            failures = sum(1 for o in recent_5m if not o["executed"])
            failure_rate = failures / len(recent_5m) * 100
            if failure_rate > 80:
                alarms.append(f"FILL_FAILURE_SPIKE: {failure_rate:.0f}% failed in 5min")

        return alarms
