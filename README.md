# Polymarket Trading Bot

## Mimari

```
polymarket-bot/
├── bot.py                  # Entry point (sadece başlat/durdur)
├── config.py               # Tüm ayarlar
├── requirements.txt
│
├── core/                   # Altyapı katmanı
│   ├── polymarket_client.py   # Polymarket API (market keşfi)
│   ├── binance_feed.py        # Binance WebSocket (fiyat verisi)
│   ├── database.py            # SQLite trade log
│   ├── risk_manager.py        # Kelly sizing, limitler
│   └── executor.py            # Order execution (paper/live)
│
├── strategies/             # Strateji katmanı (her biri bağımsız)
│   ├── base.py                # Base strategy sınıfı
│   ├── sum_to_one.py          # Sum-to-one arbitraj
│   ├── temporal_arb.py        # Crypto temporal arbitraj
│   └── weather_arb.py         # Weather oracle arbitraj
│
├── signals/                # Sinyal/indikatör katmanı
│   ├── technical.py           # RSI, MACD, VWAP
│   └── orderbook.py           # OBI, spread analysis
│
└── utils/                  # Yardımcı araçlar
    └── logger.py              # Logging setup
```

## Çalıştırma
```bash
pip install -r requirements.txt
python bot.py
```
