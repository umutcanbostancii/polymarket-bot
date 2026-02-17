#!/usr/bin/env python3
import sys
sys.path.insert(0, '.')
from core.polymarket_client import PolymarketClient
import asyncio

async def main():
    client = PolymarketClient()
    # Search for BTC Up or Down markets
    markets = await client.search_markets("Bitcoin Up or Down")
    active = [m for m in markets if m.get('active')]
    print(f"Found {len(markets)} BTC markets, {len(active)} active")
    for m in markets[:5]:
        print(f"  - {m.get('question')} (id={m.get('id')}) active={m.get('active')}")

if __name__ == "__main__":
    asyncio.run(main())