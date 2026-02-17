#!/usr/bin/env python3
"""Check the current status of the BTC 5-min trading bot."""

import json
import os
import sys
import time

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import config
from core.database import TradeDB


def main():
    # 1. Check if bot is running
    status_file = config.STATUS_FILE
    pid_path = config.PID_LOCK_PATH

    bot_running = False
    status = {}

    if os.path.exists(status_file):
        try:
            with open(status_file, "r") as f:
                status = json.load(f)
            bot_running = status.get("running", False)
        except Exception:
            pass

    if os.path.exists(pid_path):
        try:
            with open(pid_path, "r") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            bot_running = True
        except (ProcessLookupError, ValueError):
            bot_running = False
        except PermissionError:
            bot_running = True

    print("=" * 50)
    print("BTC 5-MIN TRADING BOT STATUS")
    print("=" * 50)

    if bot_running:
        mode = status.get("mode", "UNKNOWN")
        print(f"Status: RUNNING ({mode})")
        print(f"PID: {status.get('pid', 'N/A')}")
        uptime = status.get("uptime_seconds", 0)
        hours = uptime // 3600
        mins = (uptime % 3600) // 60
        print(f"Uptime: {hours}h {mins}m")
        print(f"BTC Price: ${status.get('btc_price', 'N/A'):,.2f}" if status.get("btc_price") else "BTC Price: N/A")
        print(f"Binance WS: {'Connected' if status.get('btc_connected') else 'Disconnected'}")
        print(f"Polymarket: {'Healthy' if status.get('poly_healthy') else 'Unhealthy'}")
    else:
        print("Status: STOPPED")

    print()

    # 2. Today's stats from DB
    db = TradeDB(config.DB_PATH)
    try:
        db.start()
        stats = db.today_stats()
        print("TODAY'S PERFORMANCE")
        print("-" * 30)
        print(f"Trades: {stats['trades']}")
        print(f"Wins: {stats['wins']} | Losses: {stats['losses']}")
        print(f"Win Rate: {stats['win_rate']:.1f}%")
        print(f"P&L: ${stats['pnl']:+.2f}")
        print(f"Best: ${stats['best']:+.2f}")
        print(f"Worst: ${stats['worst']:+.2f}")

        # Recent trades
        recent = db.recent_trades(limit=5)
        if recent:
            print()
            print("LAST 5 TRADES")
            print("-" * 30)
            for t in recent:
                pnl_str = f"${t['pnl']:+.2f}" if t["status"] == "resolved" else "open"
                print(
                    f"  #{t['id']} {t['side']} @ {t['price']:.4f} "
                    f"${t['cost']:.2f} -> {pnl_str} [{t['time'][:19]}]"
                )

        # Risk status
        if bot_running:
            print()
            print("RISK STATUS")
            print("-" * 30)
            halted = status.get("risk_halted", False)
            print(f"Halted: {'YES - ' + status.get('risk_reason', '') if halted else 'No'}")
            print(f"Daily P&L: ${status.get('daily_pnl', 0):+.2f}")
            print(f"Consecutive Losses: {status.get('consecutive_losses', 0)}")

        db.stop()
    except Exception as e:
        print(f"Error reading database: {e}")

    print()
    print(f"Updated: {status.get('updated_at', 'N/A')}")


if __name__ == "__main__":
    main()
