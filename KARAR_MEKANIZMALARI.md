# Polymarket Bot - Tüm Karar Mekanizmaları

---

## 1. VERİ TOPLAMA

| Kaynak | Yöntem | Veri | Detay |
|--------|--------|------|-------|
| **Binance WebSocket** | `miniTicker` stream | Canlı fiyat + 24h hacim | 10 sembol, anlık |
| **Binance REST** | Kline endpoint | **500 adet 1dk mum** | Başlangıçta warm-up |
| **Binance REST** | 24h ticker | Son fiyat + 24h volume | Başlangıçta |

Her sembol için `PriceHistory` objesi tutuluyor (max 2000 veri noktası):
- Fiyat serisi (prices)
- Volume akışı (signed flow + quote delta)
- `volume_24h` (USDT)
- `volume_base_24h` (coin cinsinden)

**Evet, mum çekiyoruz** - 500 adet 1dk kline REST ile başlangıçta indirilir, sonra WebSocket ile canlı güncellenir. Ayrıca `export_chart()` ile farklı timeframe'lerde (1m, 5m, 15m) mum verileri çekilir.

---

## 2. İNDİKATÖRLER

| İndikatör | Parametreler | Hesaplama | Sinyal |
|-----------|-------------|-----------|--------|
| **RSI** | Period=14, Wilder's EMA | `100 - 100/(1+RS)` | >55: +1, <45: -1, arası: 0 |
| **MACD** | Fast=5, Slow=13, Signal=4 | `EMA5 - EMA13`, Signal=`EMA4(MACD)` | Histogram>0: +1, <0: -1 |
| **EMA 20/50** | Fast=20, Slow=50 | Trend yönü: `(EMA20-EMA50)/EMA50` | >%0.02: +1, <%−0.02: -1 |
| **Squeeze Momentum** | BB(20,2.0), KC(20,1.5) | BB < KC = sıkışma | sq_val>0: +1, <0: -1 |
| **VWAP** | 180s pencere | `sum(price*vol)/sum(vol)` | Fiyat>VWAP: +1, <VWAP: -1 |
| **CVD** | 180s pencere | Alım-satım fark oranı | CVD>0: +1, <0: -1 |
| **Momentum 5m** | 300 saniye | Fiyat değişim % | >0: +1, <0: -1 |
| **Momentum 15m** | 900 saniye | Fiyat değişim % | Edge hesabında kullanılır |

---

## 3. ENTRY KARAR AKIŞI (Tam Sıra)

```
┌──────────────────────────────────────┐
│  WARMUP COMPLETE?                    │ 500 kline + 24h ticker
├──────────────────────────────────────┤
│  PORTFOLIO LİMİTİ                    │ Max 3 pozisyon, max 2 per yön
├──────────────────────────────────────┤
│  COOLDOWN                            │ 180s bekleme (son kapanıştan)
├──────────────────────────────────────┤
│  DEGRADED DATA?                      │ Eksik kline → skip
├──────────────────────────────────────┤
│  STALE PRICE?                        │ Fiyat yaşı > 10s → skip
├──────────────────────────────────────┤
│  DATA QUALITY                        │ Score < 0.55 → skip
├──────────────────────────────────────┤
│  VOLUME FİLTRE                       │ BTC<$200M, ETH<$50M → skip
├──────────────────────────────────────┤
│  HISTORY                             │ < 120 fiyat → skip
├──────────────────────────────────────┤
│  EMA 20/50 CROSS (Multi-TF)         │ 15m primary, 1m+5m confirm
│  - Trend yönü belirlenir             │ +1=LONG, -1=SHORT
│  - Cross yaşı ≤ 1 bar               │
│  - Min 2 TF onay                     │
├──────────────────────────────────────┤
│  6 İNDİKATÖR OYLAMA                 │
│  mom5m + MACD + RSI + VWAP + CVD    │
│  + Squeeze → min 4/6 hepsi trend    │
│  yönünde olmalı                      │
├──────────────────────────────────────┤
│  LONG EK FİLTRE                      │ LONG → 5/6 gerekli (SHORT 4/6)
├──────────────────────────────────────┤
│  EDGE MODELİ                         │
│  time_scale = √(10/5) = 1.41x       │
│  mom_edge = (|mom5|+|mom15|*0.6)     │
│           * 1.41 * 0.5              │
│  vol_edge = vol * 1.41 * 0.45       │
│  predicted = max(mom,vol) * conf     │
│           * 0.50 (shrink)           │
├──────────────────────────────────────┤
│  KELLY SIZING                        │
│  p_exec = quality + liquidity        │
│  win_prob = (0.52+0.30*conf)*p_exec  │
│  kelly = (b*p-q)/b * √p             │
│  notional = $10k * 0.25 * kelly      │
├──────────────────────────────────────┤
│  KADEMELİ POZİSYON CAP              │
│  exp_net < $3 → max $200            │
│  exp_net $3-6 → max $400            │
│  exp_net > $6 → max $600            │
├──────────────────────────────────────┤
│  HARD ENTRY GATE (Son Veto)          │
│  ✗ exp_net < $3.00                   │
│  ✗ exp_net_pct < %0.3               │
│  ✗ exp_net < 2x fee                 │
├──────────────────────────────────────┤
│  ✓ TRADE AÇ                         │
└──────────────────────────────────────┘
```

---

## 4. EXIT KARAR AKIŞI (Trade Resolver - 20s arayla kontrol)

### Cross-Hold Mode (binance_spot_margin_core stratejisi):

| Öncelik | Koşul | Exit Sebebi | Detay |
|---------|-------|-------------|-------|
| **1** | `move_pct ≥ tp_pct` | **tp** | Dinamik TP (vol*1.0 + cost*1.2, min %0.4) |
| **2** | `move_pct ≤ -sl_pct` | **sl** | Dinamik SL (vol*1.5 + cost, max %2.0) |
| **3** | `move_pct ≤ -%1.5` | **risk_exit** | Emergency stop |
| **4** | Karda (%0.2+) + ≥2dk + signal flip | **signal_flip** | EMA cross ters dönmüş |
| **5** | Zararda + ≥8dk + signal flip | **signal_flip** | Uzun bekleme sonrası |
| **6** | Stale data + yaş ≥ 60dk | **risk_exit** | Veri kalitesi düşmüş |
| **7** | Yaş ≥ 180dk | **time_expiry** | Zaman aşımı |

### Signal Flip Nasıl Tespit Edilir?

| Seviye | Yöntem | Koşul |
|--------|--------|-------|
| 1 | **EMA 20/50 Cross** | LONG ise bearish cross, SHORT ise bullish cross |
| 2 | **MACD Crossover** | MACD line signal line'ı ters yönde kesiyor |
| 3 | **Momentum** (fallback) | LONG ise mom < -%0.12, SHORT ise mom > %0.12 |

### Dinamik TP/SL Hesaplama:

```
vol = 5dk volatilite
cost_pct = (fee + slippage + borrow) / pozisyon maliyeti

TP = max(%0.4, vol × 1.0 + cost_pct × 1.2)
SL = min(%2.0, max(%0.6, vol × 1.5 + cost_pct))
```

---

## 5. RİSK YÖNETİMİ

| Mekanizma | Değer | Açıklama |
|-----------|-------|----------|
| Max toplam pozisyon | **3** | Aynı anda en fazla 3 trade |
| Max per yön | **2** | LONG max 2, SHORT max 2 |
| Kelly fraction | **%25** | Kelly'nin sadece 1/4'ü |
| Edge shrink | **%50** | Model iyimserliğini yarıya indir |
| Min expected_net | **$3.00** | Net kar $3'dan az → trade yok |
| Net > fee mult | **2x** | Kar, fee'nin 2 katından fazla olmalı |
| Cooldown | **180s** | Aynı coin'de 3dk bekleme |
| Emergency stop | **%1.5** | Acil çıkış |
| Max hold | **120dk** | Zaman aşımı |
| Wallet split | **%60 LONG / %40 SHORT** | $6000 / $4000 |

---

## 6. KRİTİK PARAMETRELERİN ETKİ HARİTASI

| Parametre | Etki Alanı | Eski → Yeni |
|-----------|------------|-------------|
| `EXPECTED_HOLD_MINUTES` | Edge büyüklüğü | 60dk → **10dk** (time_scale 3.46→1.41) |
| `EDGE_SHRINK_FACTOR` | Edge büyüklüğü | yok → **0.50** |
| `SPOT_MAX_POSITION_PCT` | Pozisyon boyutu | %8 → **%6** |
| `NOTIONAL_TIERS` | Pozisyon boyutu | yok → **$200/$400/$600** |
| `LONG_MIN_ALIGNMENT` | LONG giriş kalitesi | 4/6 → **5/6** |
| `DIRECTIONAL_MIN_EXPECTED_NET_USD` | Entry gate | $0.50 → **$3.00** |
| `CROSS_HOLD_MIN_FLIP_AGE_MINUTES` | Signal flip bekleme | 2dk → **8dk** |
| `DYNAMIC_TP_VOL_MULT` | TP seviyesi | 1.8 → **1.0** (daha erken TP) |
| `MAX_DYNAMIC_SL_PCT` | SL limiti | %4 → **%2** (daha sıkı) |

---

## 7. ÖRNEK SENARYO: BTC LONG TRADE

```
1. Warmup OK, 3'ten az pozisyon var, cooldown yok
2. BTC degraded değil, fiyat taze, quality=0.72, volume=$250M > $200M
3. EMA20 > EMA50 (15m'de bullish cross, 0 bar önce)
   1m ve 5m de trend=+1 → 3/3 confirm ✓

4. Oylama:
   mom_5m = +0.008  → +1 ✓
   MACD hist = +0.003 → +1 ✓
   RSI = 62          → +1 ✓ (>55)
   VWAP = +0.001     → +1 ✓
   CVD = +0.25       → +1 ✓
   Squeeze = -0.5    → -1 ✗
   Aligned = 5/6 ✓ (LONG min 5/6 geçti)

5. Edge:
   vol = 0.012, time_scale = 1.41
   mom_edge = (0.008 + 0.005*0.6) * 1.41 * 0.5 = 0.00776
   vol_edge = 0.012 * 1.41 * 0.45 = 0.00762
   raw_edge = 0.00776 * 1.13 (conf 5/6) = 0.00877
   predicted_edge = 0.00877 * 0.50 = 0.00438 (%0.44)

6. Kelly:
   p_exec = 0.86, win_prob = 0.70
   kelly = 0.38 → notional = $10k * 0.25 * 0.38 = $950
   preliminary_net = $950 * 0.0044 - $950 * 0.0015 = $2.75
   tier_cap = $200 (< $3)
   notional = $200

7. Hard Gate:
   exp_net = $200 * 0.0044 - $200 * 0.0015 - $200 * 0.0005 = $0.48
   $0.48 < $3.00 → REJECTED ✗

   → Sadece çok güçlü sinyaller geçebilir
```

---

Özet: Veri → 12 filtrelik entry pipeline → 7 seviyeli exit pipeline. Her katman AND mantığıyla çalışır - tek bir filtre bile reddederse trade açılmaz.
