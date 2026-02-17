#!/usr/bin/env python3
"""Generate a daily P&L report for the BTC 5-min trading bot."""

import os
import sys
from datetime import datetime, timezone

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import config
from core.database import TradeDB


def main():
    db = TradeDB(config.DB_PATH)
    db.start()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stats = db.today_stats()
    trades = db.recent_trades(limit=200)

    # Filter today's trades
    today_trades = [t for t in trades if t.get("time", "").startswith(today)]
    resolved = [t for t in today_trades if t["status"] == "resolved"]

    print("=" * 60)
    print(f"DAILY REPORT - {today}")
    print(f"Strategy: btc_5min_fast")
    print(f"Mode: {'PAPER' if config.PAPER_TRADING else 'LIVE'}")
    print("=" * 60)

    print(f"\nTotal Trades: {stats['trades']}")
    print(f"Resolved: {len(resolved)}")
    print(f"Open: {stats['trades'] - len(resolved)}")

    print(f"\nWins: {stats['wins']}")
    print(f"Losses: {stats['losses']}")
    print(f"Win Rate: {stats['win_rate']:.1f}%")

    print(f"\nP&L: ${stats['pnl']:+.2f}")
    print(f"Best Trade: ${stats['best']:+.2f}")
    print(f"Worst Trade: ${stats['worst']:+.2f}")

    if resolved:
        pnls = [t["pnl"] for t in resolved if t["pnl"] is not None]
        if pnls:
            avg_pnl = sum(pnls) / len(pnls)
            print(f"Average P&L: ${avg_pnl:+.2f}")

            winning = [p for p in pnls if p > 0]
            losing = [p for p in pnls if p < 0]
            if winning:
                print(f"Average Win: ${sum(winning)/len(winning):+.2f}")
            if losing:
                print(f"Average Loss: ${sum(losing)/len(losing):+.2f}")

            # Profit factor
            gross_profit = sum(winning) if winning else 0
            gross_loss = abs(sum(losing)) if losing else 0
            if gross_loss > 0:
                pf = gross_profit / gross_loss
                print(f"Profit Factor: {pf:.2f}")

    # Trade-by-trade breakdown
    if today_trades:
        print(f"\n{'='*60}")
        print("TRADE BREAKDOWN")
        print(f"{'='*60}")
        print(f"{'#':<5} {'Time':<20} {'Side':<10} {'Price':<8} {'Cost':<10} {'P&L':<10} {'Status'}")
        print("-" * 75)
        for t in today_trades:
            pnl_str = f"${t['pnl']:+.2f}" if t["status"] == "resolved" else "-"
            time_str = t.get("time", "")[:19]
            print(
                f"{t['id']:<5} {time_str:<20} {t['side']:<10} "
                f"{t['price']:<8.4f} ${t['cost']:<9.2f} {pnl_str:<10} {t['status']}"
            )

    # 7-day history
    history = db.pnl_history(days=7)
    if history:
        print(f"\n{'='*60}")
        print("7-DAY P&L HISTORY")
        print(f"{'='*60}")
        for h in history:
            print(f"  {h['date']}: ${h['pnl']:+.2f} (cumulative: ${h['cumulative']:+.2f})")

    db.stop()


if __name__ == "__main__":
    main()
