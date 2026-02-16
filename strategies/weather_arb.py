"""
Weather Oracle Arbitrage
=========================
Hava durumu API'sinden tahmin cek, Polymarket fiyatiyla karsilastir.
Resolution source: Wunderground (market bazli kontrol).
Faz 0: sadece market tara ve logla.
Faz 1: API entegrasyonu + otomatik trade.
"""

import logging
from strategies.base import BaseStrategy

log = logging.getLogger(__name__)


class WeatherArbStrategy(BaseStrategy):

    name = "weather_arb"

    @property
    def interval(self) -> float:
        return self.cfg.WEATHER_INTERVAL  # 15 dakika

    async def scan(self):
        markets = await self.poly.get_weather_markets()

        if not markets:
            log.debug("No weather markets found")
            return

        log.info(f"🌤️ {len(markets)} weather markets found")

        # Faz 0: only log opportunities when edge is found
        for m in markets:
            y = m.get("yes_price", 0)
            n = m.get("no_price", 0)
            s = y + n

            # Sum-to-one trading is handled by SumToOneStrategy (includes weather markets)
            net_edge = (1.0 - s) - 0.05
            if net_edge > 0 and s < self.cfg.SUM_THRESHOLD:
                self.db.log_opportunity(
                    strategy=self.name,
                    market_id=m["id"],
                    question=m["question"],
                    edge=net_edge,
                    yes_price=y, no_price=n, price_sum=s,
                    action="scanned",
                    notes=f"res_source={m.get('resolution_source', 'unknown')}"
                )
                log.info(
                    f"🔍 WEATHER SUM<1: {m['question'][:60]} | "
                    f"Y={y:.4f} N={n:.4f} edge={net_edge:.2%} (scan only)"
                )

        # TODO Faz 1: Wunderground/OWM API entegrasyonu
        # 1. Her market icin resolution source'u parse et
        # 2. Dogru hava durumu API'sinden tahmin cek
        # 3. Tahmin vs market fiyat karsilastir
        # 4. Edge > threshold ise pozisyon ac
