"""Base strategy interface"""

from abc import ABC, abstractmethod


class BaseStrategy(ABC):
    """Tüm stratejiler bu sınıftan türer"""

    name: str = "base"

    def __init__(self, config, polymarket, binance, db, risk, executor):
        self.cfg = config
        self.poly = polymarket
        self.binance = binance
        self.db = db
        self.risk = risk
        self.executor = executor
        self._dt = None  # DecisionTracker, subclass'ta set edilir

    def decision_snapshot(self):
        return self._dt.snapshot() if self._dt else None

    @abstractmethod
    async def scan(self):
        """Bir scan döngüsü çalıştır — fırsat bul, gerekiyorsa trade aç"""
        pass

    @property
    @abstractmethod
    def interval(self) -> float:
        """Scan aralığı (saniye)"""
        pass
