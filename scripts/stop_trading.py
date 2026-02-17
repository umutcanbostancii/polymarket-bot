#!/usr/bin/env python3
"""Stop the running BTC 5-min trading bot."""

import os
import signal
import sys
import time


def main():
    pid_path = "/tmp/polymarket_bot.pid"

    if not os.path.exists(pid_path):
        print("No bot process found (no PID file).")
        sys.exit(0)

    try:
        with open(pid_path, "r") as f:
            pid = int(f.read().strip())
    except (ValueError, FileNotFoundError):
        print("Invalid PID file.")
        sys.exit(1)

    try:
        os.kill(pid, 0)  # Check if process exists
    except ProcessLookupError:
        print(f"Bot process (pid={pid}) not running. Cleaning up PID file.")
        os.remove(pid_path)
        sys.exit(0)
    except PermissionError:
        pass

    print(f"Sending SIGTERM to bot (pid={pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("Process already terminated.")
        return

    # Wait for graceful shutdown
    for i in range(10):
        time.sleep(1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print("Bot stopped successfully.")
            return

    print("Bot did not stop gracefully. Sending SIGKILL...")
    try:
        os.kill(pid, signal.SIGKILL)
        print("Bot killed.")
    except ProcessLookupError:
        print("Bot already stopped.")

    # Clean up PID file
    if os.path.exists(pid_path):
        os.remove(pid_path)


if __name__ == "__main__":
    main()
