#!/usr/bin/env python3
"""Start the BTC 5-min fast loop trading bot."""

import argparse
import os
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="Start BTC 5-min Polymarket trading bot")
    parser.add_argument("--paper", action="store_true", default=True, help="Paper trading mode (default)")
    parser.add_argument("--live", action="store_true", help="Live trading mode")
    parser.add_argument("--size", type=float, default=None, help="Trade size in USDC")
    parser.add_argument("--edge-min", type=float, default=None, help="Minimum edge threshold")
    args = parser.parse_args()

    # Set environment overrides
    env = os.environ.copy()

    if args.live:
        env["PAPER_TRADING"] = "false"
        print("MODE: LIVE TRADING")
    else:
        env["PAPER_TRADING"] = "true"
        print("MODE: PAPER TRADING")

    if args.size:
        env["TRADE_SIZE_USD"] = str(args.size)
        print(f"Trade size: ${args.size}")

    if args.edge_min:
        env["EDGE_THRESHOLD"] = str(args.edge_min)
        print(f"Edge threshold: {args.edge_min}")

    # Check for PID lock
    pid_path = "/tmp/polymarket_bot.pid"
    if os.path.exists(pid_path):
        try:
            with open(pid_path, "r") as f:
                existing_pid = int(f.read().strip())
            # Check if process is running
            os.kill(existing_pid, 0)
            print(f"Bot is already running (pid={existing_pid})")
            print("Use stop_trading.py to stop it first.")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            # Stale PID file, remove it
            os.remove(pid_path)
        except PermissionError:
            print(f"Bot is already running (pid={existing_pid})")
            sys.exit(1)

    # Start bot
    bot_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bot_py = os.path.join(bot_dir, "bot.py")

    print(f"Starting bot from {bot_py}...")
    print("Press Ctrl+C to stop.")
    print()

    try:
        proc = subprocess.run(
            [sys.executable, bot_py],
            env=env,
            cwd=bot_dir,
        )
        sys.exit(proc.returncode)
    except KeyboardInterrupt:
        print("\nBot stopped by user.")


if __name__ == "__main__":
    main()
