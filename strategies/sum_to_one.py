"""
Sum-to-One Arbitrage
=====================
YES + NO < $0.97 olan marketlerde her iki tarafı da al.
Sonuç ne olursa olsun kâr = $1.00 - (YES + NO) - fees
"""

import logging
from typing import List, Dict
from strategies.base import BaseStrategy

log = logging.getLogger(__name__)


class SumToOneStrategy(BaseStrategy):

    name = "sum_to_one"
    FEE_RATE = 0.05  # 2.5% x 2 sides (YES+NO)

    @property
    def interval(self) -> float:
        return self.cfg.SCAN_INTERVAL

    async def scan(self):
        markets = await self.poly.get_crypto_markets()
        weather = await self.poly.get_weather_markets()
        all_markets = markets + weather

        if not all_markets:
            return

        opps = self._find_opportunities(all_markets)

        for opp in opps:
            await self._handle(opp)

    def _find_opportunities(self, markets: List[Dict]) -> List[Dict]:
        opps = []
        for m in markets:
            if m.get("liquidity", 0) < self.cfg.MIN_LIQUIDITY:
                continue

            y = m.get("yes_price", 0)
            n = m.get("no_price", 0)
            if y <= 0 or n <= 0:
                continue

            s = y + n
            if s >= self.cfg.SUM_THRESHOLD:
                continue

            raw_edge = 1.0 - s
            net_edge = raw_edge - self.FEE_RATE

            if net_edge > 0:
                opps.append({
                    "market": m, "yes": y, "no": n,
                    "sum": s, "edge": net_edge
                })
                log.info(
                    f"🎯 SUM<1: {m['question'][:60]} | "
                    f"Y={y:.4f} N={n:.4f} sum={s:.4f} edge={net_edge:.2%}"
                )

        return sorted(opps, key=lambda x: x["edge"], reverse=True)

    async def _handle(self, opp: dict):
        edge = opp["edge"]
        market = opp["market"]
        size = self.risk.arb_size(edge)

        if size <= 0:
            return

        self.db.log_opportunity(
            strategy=self.name, market_id=market["id"],
            question=market["question"], edge=edge,
            yes_price=opp["yes"], no_price=opp["no"],
            price_sum=opp["sum"], action="trade"
        )

        half = size / 2
        await self.executor.execute(
            self.name, market, "YES", opp["yes"], half,
            notes=f"arb edge={edge:.2%}"
        )
        await self.executor.execute(
            self.name, market, "NO", opp["no"], half,
            notes=f"arb edge={edge:.2%}"
        )
