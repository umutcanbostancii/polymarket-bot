#!/usr/bin/env python3
"""Scan for active BTC 5-min markets on Polymarket."""

import asyncio
import os
import sys
import time

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import config
from core.polymarket_client import PolymarketClient


async def main():
    poly = PolymarketClient(config)
    await poly.start()

    print("Scanning for active BTC 5-min markets...\n")

    market = await poly.find_active_btc_5min_market()

    if market:
        end_ts = market.get("end_date_ts", 0)
        remaining = end_ts - time.time() if end_ts else 0

        print("ACTIVE MARKET FOUND")
        print("=" * 50)
        print(f"Question: {market['question']}")
        print(f"Market ID: {market['id']}")
        print(f"Ends: {market['end_date']}")
        print(f"Remaining: {remaining:.0f}s ({remaining/60:.1f}m)")
        print()
        print(f"Up Price: {market.get('up_price', 'N/A'):.4f}")
        print(f"Down Price: {market.get('down_price', 'N/A'):.4f}")
        print(f"Volume: ${market.get('volume', 0):,.0f}")
        print(f"Liquidity: ${market.get('liquidity', 0):,.0f}")
        print()
        print(f"Up Token: {market.get('up_token', 'N/A')}")
        print(f"Down Token: {market.get('down_token', 'N/A')}")
        print(f"Outcomes: {market.get('outcomes', [])}")

        # Fetch orderbooks
        for direction in ["up", "down"]:
            token = market.get(f"{direction}_token")
            if token:
                book = await poly.get_orderbook(token)
                if book:
                    bids = book.get("bids", [])
                    asks = book.get("asks", [])
                    print(f"\n{direction.upper()} Orderbook:")
                    print(f"  Bids: {len(bids)} levels")
                    print(f"  Asks: {len(asks)} levels")
                    if bids:
                        best_bid = max(float(b.get("price", 0)) for b in bids)
                        print(f"  Best Bid: {best_bid:.4f}")
                    if asks:
                        best_ask = min(float(a.get("price", 1)) for a in asks)
                        print(f"  Best Ask: {best_ask:.4f}")
    else:
        print("No active BTC 5-min market found right now.")
        print("This can happen if:")
        print("  - It's outside trading hours (markets run ~9AM-7PM ET)")
        print("  - You're between 5-min windows (new one starts soon)")
        print()
        print("Checking latest available events for reference...")
        from datetime import datetime, timezone
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://gamma-api.polymarket.com/events",
                params={"closed": "false", "order": "startDate",
                        "ascending": "false", "limit": 5}
            ) as r:
                if r.status == 200:
                    events = await r.json()
                    btc = [e for e in events
                           if e.get("slug", "").startswith("btc-updown-5m-")]
                    if btc:
                        print(f"\nLatest BTC 5-min event: {btc[0].get('title', '')}")
                        markets = btc[0].get("markets", [])
                        if markets:
                            end = markets[0].get("endDate", "")
                            print(f"  Ended at: {end}")
                    else:
                        print("  No recent BTC 5-min events found in latest 5.")

    await poly.stop()


if __name__ == "__main__":
    asyncio.run(main())
