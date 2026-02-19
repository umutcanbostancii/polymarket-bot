"""Decision step tracker for dashboard pipeline visualization."""

import time


class DecisionTracker:
    """Tracks decision steps during a strategy scan() iteration.

    Usage in strategy scan():
        self._dt.begin(self._scan_count)
        ok, reason = self.risk.can_trade()
        if not self._dt.step("risk_check", ok, reason if not ok else "OK"):
            return
        ...
    """

    STEPS_MAIN = [
        ("risk_check", "Risk Kontrolu"),
        ("market_discovery", "Market Bulma"),
        ("preparation", "Hazirlik"),
        ("observation", "Gozlem Fazi"),
        ("delta_filter", "Delta Filtre"),
        ("momentum_cap", "Momentum Cap"),
        ("candle_signal", "Mum Analizi"),
        ("direction", "Yon Belirleme"),
        ("trend", "Trend Uyumu"),
        ("signal_strength", "Sinyal Gucu"),
        ("orderbook", "CLOB Orderbook"),
        ("edge_calc", "Edge Hesaplama"),
        ("price_risk", "Fiyat/Risk"),
        ("validation", "Validation"),
        ("execution", "Islem"),
    ]

    STEPS_TEST = [
        ("risk_check", "Risk Kontrolu"),
        ("market_discovery", "Market Bulma"),
        ("preparation", "Hazirlik"),
        ("warmup", "Warmup Fazi"),
        ("signals", "Sinyal Toplama"),
        ("aggregate", "Sinyal Agregasyon"),
        ("timing", "Giris Zamanlama"),
        ("orderbook", "CLOB Orderbook"),
        ("price_check", "Fiyat Kontrol"),
        ("execution", "Islem"),
    ]

    def __init__(self, step_defs: list[tuple[str, str]]):
        self._step_defs = step_defs
        self._steps: list[dict] = []
        self._ts: float = 0.0
        self._interesting: bool = False
        self._scan_count: int = 0
        self._market_question: str = ""
        self._remaining: float = 0.0

    def begin(self, scan_count: int):
        """Call at the start of scan(). Resets all steps to pending."""
        self._ts = time.time()
        self._scan_count = scan_count
        self._interesting = False
        self._market_question = ""
        self._remaining = 0.0
        self._steps = [
            {"id": sid, "label": label, "status": "pending", "detail": ""}
            for sid, label in self._step_defs
        ]

    def step(self, step_id: str, passed: bool, detail: str = "") -> bool:
        """Record the outcome of a decision step.

        Returns the passed value for one-liner usage:
            if not self._dt.step("risk_check", ok, detail):
                return
        """
        for s in self._steps:
            if s["id"] == step_id:
                s["status"] = "pass" if passed else "fail"
                s["detail"] = detail
                break
        if step_id == "market_discovery" and passed:
            self._interesting = True
        return passed

    def set_market_info(self, question: str, remaining: float):
        """Store current market info for display."""
        self._market_question = question
        self._remaining = remaining

    def snapshot(self) -> dict:
        """Return current state for JSON serialization."""
        return {
            "scan_count": self._scan_count,
            "ts": self._ts,
            "interesting": self._interesting,
            "market_question": self._market_question,
            "remaining": round(self._remaining),
            "steps": list(self._steps),
        }
