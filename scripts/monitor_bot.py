#!/usr/bin/env python3
"""
Bot Monitor - Her 10 dakikada bot durumunu kontrol eder.
Eğer bot çalışmıyorsa veya hata varsa düzeltir.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time

BOT_DIR = "/Users/umutcanbostanci/Desktop/polymarket-bot"
STATUS_FILE = "/tmp/polymarket_bot_status.json"
DB_PATH = f"{BOT_DIR}/trades.db"

def check_bot():
    """Bot durumunu kontrol et."""
    print("=== Bot Kontrolü ===")
    
    # Status dosyasını oku
    try:
        with open(STATUS_FILE, 'r') as f:
            status = json.load(f)
    except:
        status = {}
    
    running = status.get('running', False)
    mode = status.get('mode', 'UNKNOWN')
    today_trades = status.get('today_trades', 0)
    today_pnl = status.get('today_pnl', 0)
    btc_price = status.get('btc_price', 0)
    
    print(f"Running: {running}")
    print(f"Mode: {mode}")
    print(f"Trades: {today_trades}")
    print(f"PnL: ${today_pnl}")
    print(f"BTC: ${btc_price}")
    
    # Son trade'leri kontrol et
    try:
        conn = sqlite3.connect(DB_PATH)
        last_trades = conn.execute(
            "SELECT id, side, price, cost, is_paper, pnl FROM trades ORDER BY id DESC LIMIT 3"
        ).fetchall()
        conn.close()
        
        print("\nSon 3 trade:")
        for t in last_trades:
            print(f"  #{t[0]}: {t[1]} @ {t[2]} | cost=${t[3]} | paper={t[4]} | pnl=${t[5]}")
    except Exception as e:
        print(f"DB error: {e}")
    
    # Bot çalışmıyorsa başlat
    if not running or mode != 'LIVE':
        print("\n⚠️ Bot çalışmıyor! Başlatılıyor...")
        start_bot()
    else:
        print("\n✅ Bot çalışıyor")
    
    print("\n=== Kontrol Bitti ===")

def start_bot():
    """Botu başlat."""
    print("Bot başlatılıyor...")
    try:
        result = subprocess.run(
            [f"{BOT_DIR}/.venv/bin/python3.13", f"{BOT_DIR}/scripts/start_trading.py", "--live", "--size", "1"],
            cwd=BOT_DIR,
            capture_output=True,
            text=True,
            timeout=30
        )
        print(f"Start result: {result.returncode}")
        if result.stdout:
            print(result.stdout[-500:])
        if result.stderr:
            print("Errors:", result.stderr[-500:])
    except Exception as e:
        print(f"Start error: {e}")

if __name__ == "__main__":
    check_bot()
