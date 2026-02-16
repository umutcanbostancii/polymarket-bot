import os
from dataclasses import dataclass, field
from typing import Dict, List

from dotenv import load_dotenv
load_dotenv()


@dataclass
class Config:
    # Mode
    PAPER_TRADING: bool = True
    CORE_STRATEGIES_ONLY: bool = True
    PID_LOCK_PATH: str = "/tmp/polymarket_bot.pid"

    # Polymarket
    GAMMA_API: str = "https://gamma-api.polymarket.com"
    CLOB_API: str = "https://clob.polymarket.com"
    POLY_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/"
    GAMMA_API_FALLBACKS: List[str] = field(default_factory=list)
    CLOB_API_FALLBACKS: List[str] = field(default_factory=list)

    # Polymarket CLOB API Credentials
    POLY_API_KEY: str = os.getenv("POLY_API_KEY", "")
    POLY_API_SECRET: str = os.getenv("POLY_API_SECRET", "")
    POLY_API_PASSPHRASE: str = os.getenv("POLY_API_PASSPHRASE", "")

    # Binance (ucretsiz, hesap gerektirmez)
    BINANCE_WS: str = "wss://stream.binance.com:9443/ws"
    BINANCE_REST: str = "https://api.binance.com/api/v3"

    # Binance Spot Simulation (ayri bakiye ile multi-coin trading)
    SPOT_BANKROLL: float = 10_000.0
    SPOT_MAX_POSITION_PCT: float = 0.06  # %6 per coin ($600 hard cap)
    # Kademeli cap'ler: {min_expected_net_usd: max_notional_usd}
    NOTIONAL_TIERS: Dict[float, float] = field(default_factory=lambda: {
        3.0: 200,   # exp_net < $3 → max $200
        6.0: 400,   # exp_net $3-6 → max $400
        999: 600,   # exp_net > $6 → max $600
    })
    SPOT_MAX_DAILY_LOSS_PCT: float = 0.05
    SPOT_SYMBOLS: List[str] = field(default_factory=lambda: [
        "BTC", "ETH", "SOL", "XRP", "DOGE", "AVAX", "ADA", "LINK", "DOT", "MATIC",
    ])

    # Weather
    OWM_API_KEY: str = os.getenv("OWM_API_KEY", "")

    # Wallet (Faz 1)
    PRIVATE_KEY: str = os.getenv("BOT_PRIVATE_KEY", "")
    WALLET_ADDRESS: str = os.getenv("BOT_WALLET_ADDRESS", "")

    # Risk
    BANKROLL: float = 10_000.0
    MAX_POSITION_PCT: float = 0.05
    MAX_DAILY_LOSS_PCT: float = 0.05
    KELLY_FRACTION: float = 0.25
    MIN_EDGE: float = 0.015
    COOLDOWN_AFTER_LOSSES: int = 50

    # Sum-to-one
    SUM_THRESHOLD: float = 0.98
    MIN_LIQUIDITY: int = 50

    # Temporal
    MOMENTUM_THRESHOLD: float = 0.003  # %0.3 - yukseldi, daha net sinyal gerekli
    MIN_TIME_REMAINING: int = 60

    # Weather
    WEATHER_CITIES: List[str] = field(default_factory=lambda: [
        "New York", "Chicago", "Atlanta", "Toronto", "London"
    ])
    WEATHER_EDGE_THRESHOLD: float = 0.10
    WEATHER_INTERVAL: int = 900

    # Scanning
    SCAN_INTERVAL: float = 1.0
    CRYPTO_MARKETS: List[str] = field(default_factory=lambda: [
        "Bitcoin Up or Down", "Ethereum Up or Down", "Solana Up or Down"
    ])

    # Indicators
    RSI_PERIOD: int = 14
    RSI_OVERBOUGHT: float = 70.0
    RSI_OVERSOLD: float = 30.0
    FUSION_THRESHOLD: float = 0.45
    ADX_PERIOD: int = 14
    ADX_TREND_THRESHOLD: float = 20.0  # ADX < 20 = yatay piyasa, trade acma

    # Signal confidence
    MIN_SIGNAL_CONFIDENCE: float = 0.40  # 0.30'dan yukseldi - daha az ama daha kaliteli trade
    STRONG_SIGNAL_CONFIDENCE: float = 0.65  # Guclu sinyal icin bonus pozisyon

    # === STRATEJI IYILESTIRMELERI (v2) ===

    # Fix 1: Strict Trend Filter
    # EMA50 ustunde = sadece LONG, altinda = sadece SHORT. Istisna yok.
    STRICT_TREND_FILTER: bool = True

    # Fix 2: Minimum Momentum Hard Gate
    # |momentum| < bu deger ise trade ACILMAZ. Gurultu filtreleme.
    MIN_MOMENTUM_GATE: float = 0.0005  # %0.05 - paper trade icin daha dusuk esik

    # Fix 3: RSI Dead Zone (notr bolge = trade yok)
    RSI_NO_TRADE_LOW: float = 40.0    # RSI < 40 = SHORT bolge
    RSI_NO_TRADE_HIGH: float = 60.0   # RSI > 60 = LONG bolge (40-60 arasi = trade YOK)

    # Fix 5: Trade Cooldown
    # Bir trade kapandiktan sonra ayni coin icin bekleme suresi
    TRADE_COOLDOWN_SECONDS: int = 180  # 3 dakika - noise trading'i keser

    # Trade resolution (paper mode simulation)
    # ONEMLI: Eski degerler cok kisaydi (BTC 5m, diger 15m)
    # Kisa hold time + fee = kar marji yok
    # Yeni degerler: fiyatin anlamli hareket etmesi icin yeterli sure
    TEMPORAL_HOLD_MINUTES: int = 60   # Eskiden 15 idi
    BTC_HOLD_MINUTES: int = 30        # Eskiden 5 idi (5 dakikada fee'yi bile cikaramiyordu)
    ARB_HOLD_MINUTES: int = 45

    # Stop Loss / Take Profit (yuzde olarak, negatif = zarar)
    SL_PCT: Dict[str, float] = field(default_factory=lambda: {
        "BTC": -0.015,    # %1.5 stop loss
        "ETH": -0.020,    # %2.0 stop loss
        "SOL": -0.025,    # %2.5 stop loss (daha volatil)
        "XRP": -0.025,
        "DOGE": -0.030,   # %3.0 - meme coin, daha volatil
        "AVAX": -0.025,
        "ADA": -0.025,
        "LINK": -0.025,
        "DOT": -0.025,
        "MATIC": -0.025,
        "DEFAULT": -0.020,
    })
    TP_PCT: Dict[str, float] = field(default_factory=lambda: {
        "BTC": 0.025,     # %2.5 take profit
        "ETH": 0.035,     # %3.5 take profit
        "SOL": 0.045,     # %4.5 take profit (daha volatil = daha yuksek TP)
        "XRP": 0.040,
        "DOGE": 0.050,    # %5.0 - meme coin, buyuk hareket bekle
        "AVAX": 0.040,
        "ADA": 0.040,
        "LINK": 0.040,
        "DOT": 0.040,
        "MATIC": 0.040,
        "DEFAULT": 0.030,
    })

    # Binance fee (configurable, VIP tier'a gore degisir)
    # BNB ile odeme: 0.075% giris + 0.075% cikis = %0.15 round-trip
    # BNB'siz: 0.1% + 0.1% = 0.2%. Biz BNB varsayiyoruz (standart).
    BINANCE_FEE_RATE: float = 0.0015
    BINANCE_BASE_SLIPPAGE_PCT: float = 0.0005
    BINANCE_MAX_SLIPPAGE_PCT: float = 0.003
    MARGIN_BORROW_APR: float = 0.18

    # Minimum 24h quote volume (USDT) - dusuk hacimli coinlerde trade ACMA
    # Binance likit borsa, $100-800 paper trade icin $3M bile yeterli
    MIN_VOLUME_USD: Dict[str, float] = field(default_factory=lambda: {
        "BTC": 200_000_000,
        "ETH": 50_000_000,
        "SOL": 10_000_000,
        "XRP": 5_000_000,
        "DOGE": 3_000_000,
        "AVAX": 2_000_000,
        "ADA": 2_000_000,
        "LINK": 2_000_000,
        "DOT": 2_000_000,
        "MATIC": 2_000_000,
        "DEFAULT": 2_000_000,
    })

    # Minimum beklenen dolar kar - fee'lerden fazla olmali
    # $300 pozisyon x 0.2% fee = $0.60 fee → en az $1.50 kar hedefle
    MIN_DOLLAR_PROFIT: float = 1.50

    # === EDGE MODELI ===
    EXPECTED_HOLD_MINUTES: float = 10.0       # Gercekci hold suresi (60 → 10)
    EDGE_SHRINK_FACTOR: float = 0.50          # Model iyimserlik duzeltmesi

    # === PORTFOLIO LIMITI ===
    MAX_SIMULTANEOUS_POSITIONS: int = 3
    MAX_POSITIONS_PER_DIRECTION: int = 2

    # === LONG FILTRESI ===
    LONG_MIN_ALIGNMENT: int = 5               # LONG icin 5/6 sinyal gerekli (SHORT 4/6 ok)

    # Spot icin ayri seans filtresi - kripto 7/24, seans filtresi gereksiz
    SPOT_USE_SESSION_FILTER: bool = False

    # Trading session filter (@ecocantravel + @evrenkececi onerisi)
    # Kripto 7/24 - seans filtresi sadece geleneksel piyasalar icin
    # Binance zaten 24 saat likit, gece spread farki minimal
    USE_SESSION_FILTER: bool = False
    # UTC saatleri - London + NY overlap = en yuksek hacim
    TRADING_SESSIONS: List[Dict] = field(default_factory=lambda: [
        {"name": "London", "start_utc": 8, "end_utc": 16},
        {"name": "NewYork", "start_utc": 13, "end_utc": 21},
    ])

    # Minimum kar/zarar esigi - bu kadar kucuk PnL icin trade acilmasin
    MIN_PROFIT_TARGET_PCT: float = 0.005  # En az %0.5 potansiyel kar hedefi

    # Hard entry gate (fee-aware) - sertlestirildi: sadece guclu sinyaller gecmeli
    DIRECTIONAL_MIN_EXPECTED_NET_USD: float = 3.00   # 0.50 → 3.00
    DIRECTIONAL_MIN_EXPECTED_NET_PCT: float = 0.003  # 0.001 → 0.003 (%0.3)
    DIRECTIONAL_MIN_NET_FEE_MULT: float = 2.0        # 0.5 → 2.0 (kar > 2x fee)
    DIRECTIONAL_MIN_NOTIONAL_EDGE_PCT: float = 0.002 # 0.001 → 0.002 of notional
    MIN_TRADE_NOTIONAL_USD: float = 50.0
    MAX_LIQUIDITY_SHARE: float = 0.0005  # binance quote volume share cap

    # Wallet split for spot/margin simulation
    SPOT_LONG_WALLET_SHARE: float = 0.60
    SPOT_SHORT_WALLET_SHARE: float = 0.40

    # Polymarket complete-set / rebalancing costs
    POLY_TAKER_FEE: float = 0.025
    POLY_SLIPPAGE_PCT: float = 0.003
    POLY_EXEC_BUFFER_PCT: float = 0.001
    POLY_GAS_EST_USD: float = 0.60
    POLY_MAX_GAS_EST_USD: float = 4.00
    POLY_LATENCY_PENALTY_BPS_PER_100MS: float = 1.5
    POLY_MIN_FILL_PROB: float = 0.75
    POLY_ORDERBOOK_STALE_MS: int = 1200
    POLY_REST_TIMEOUT_SECONDS: int = 10
    POLY_MAX_REQUESTS_PER_SEC: float = 25.0
    POLY_RETRY_MAX_ATTEMPTS: int = 3
    POLY_CIRCUIT_BREAKER_ERRORS: int = 5
    POLY_CIRCUIT_BREAKER_COOLDOWN_SECONDS: int = 25
    POLY_MAX_DEPTH_SHARE: float = 0.50
    POLY_REBALANCE_SPREAD_THRESHOLD: float = 0.07
    POLY_MIN_MARKET_LIQUIDITY: float = 100.0
    POLY_ENABLE_REBALANCE: bool = False  # Disabled by default: requires stricter cross-market math.
    POLY_MAX_ORDERBOOK_SPREAD_PCT: float = 0.35
    POLY_MAX_VWAP_DEVIATION_PCT: float = 0.40

    # Resolver settings (triple barrier)
    RESOLVER_INTERVAL_SECONDS: int = 20
    MAX_HOLD_MINUTES_SPOT: int = 120
    MAX_HOLD_MINUTES_POLY: int = 180
    SIGNAL_FLIP_MOMENTUM_PCT: float = 0.0012
    DYNAMIC_TP_VOL_MULT: float = 1.0        # 1.8 → 1.0 (daha erken TP)
    DYNAMIC_SL_VOL_MULT: float = 1.5        # 1.2 → 1.5 (daha genis SL)
    MIN_DYNAMIC_TP_PCT: float = 0.004       # 0.008 → 0.004
    MIN_DYNAMIC_SL_PCT: float = 0.006
    MAX_DYNAMIC_SL_PCT: float = 0.020       # 0.040 → 0.020
    MACD_CROSS_LOOKBACK_BARS: int = 12
    SIGNAL_FLIP_MIN_AGE_MINUTES: float = 2.0

    # EMA cross driven entry/exit (yellow/blue line discipline)
    EMA_CROSS_FAST_PERIOD: int = 20
    EMA_CROSS_SLOW_PERIOD: int = 50
    EMA_CROSS_PRIMARY_TF: str = "15m"
    EMA_CROSS_TIMEFRAMES: List[str] = field(default_factory=lambda: ["1m", "5m", "15m"])
    EMA_CROSS_MAX_AGE_BARS: int = 1
    EMA_CROSS_MIN_CONFIRMATIONS: int = 2
    CROSS_HOLD_UNTIL_FLIP: bool = True
    CROSS_HOLD_MAX_ADVERSE_PCT: float = 0.015  # 0.025 → 0.015
    CROSS_HOLD_MIN_FLIP_AGE_MINUTES: float = 8.0  # zararda signal flip icin 8dk bekleme
    SIGNAL_FLIP_MIN_MOVE_PCT: float = 0.002        # signal flip icin min %0.2 kar
    EMA_CROSS_EXIT_LOOKBACK_BARS: int = 2

    # Validation gates runner
    VALIDATION_WINDOW_DAYS: int = 30
    VALIDATION_RUN_INTERVAL_SECONDS: int = 900
    VALIDATION_MIN_TRADES: int = 100
    VALIDATION_MIN_WIN_RATE_PCT: float = 80.0
    VALIDATION_MIN_RETURN_PCT: float = 30.0
    VALIDATION_MAX_DRAWDOWN_PCT: float = 10.0
    VALIDATION_MIN_PROFIT_FACTOR: float = 1.8
    VALIDATION_MIN_MEDIAN_NET_TRADE_USD: float = 3.0

    # === v3 PDF HARMONIZASYON - Yeni Parametreler ===

    # Squeeze Momentum (John Carter / LazyBear)
    # BB, KC icine girdiginde volatilite sikisir → breakout beklenir
    USE_SQUEEZE_MOMENTUM: bool = True
    SQUEEZE_BB_PERIOD: int = 20
    SQUEEZE_BB_MULT: float = 2.0
    SQUEEZE_KC_MULT: float = 1.5

    # Heikin-Ashi Streak: ardisik HA mum sayisi, trend dogrulama
    USE_HEIKIN_ASHI: bool = True
    MIN_HA_STREAK: int = 2  # En az 2 ardisik mum ayni yonde olmali

    # EMA 5/20 Cross: hizli trend sinyali (@SolStine terminal)
    USE_EMA_CROSS: bool = True

    # Multi-Timeframe Momentum: 1m, 5m, 10m hepsi ayni yonde olmali
    USE_MULTI_TF_MOMENTUM: bool = True

    # Modified Kelly Criterion (Roan makale)
    # f = (b*p - q) / b * sqrt(p) - execution risk icin konservatif
    USE_MODIFIED_KELLY: bool = True
    KELLY_WIN_PROB_BASE: float = 0.55  # Baz kazanma olasiligi (confidence ile ayarlanir)

    # Infra
    DB_PATH: str = "trades.db"
    LOG_LEVEL: str = "INFO"
    TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Auto-resume after halt
    HALT_RESUME_SECONDS: int = 3600

    # Dashboard
    DASHBOARD_PORT: int = 8080

    # Position monitoring
    POSITION_CHECK_INTERVAL: int = 60

    def __post_init__(self):
        assert self.BANKROLL > 0, f"BANKROLL must be > 0, got {self.BANKROLL}"
        assert 0 < self.MAX_DAILY_LOSS_PCT < 1, (
            f"MAX_DAILY_LOSS_PCT must be in (0, 1), got {self.MAX_DAILY_LOSS_PCT}"
        )
        assert 0 < self.KELLY_FRACTION <= 1, (
            f"KELLY_FRACTION must be in (0, 1], got {self.KELLY_FRACTION}"
        )
        assert 0 < self.MAX_POSITION_PCT < 1, (
            f"MAX_POSITION_PCT must be in (0, 1), got {self.MAX_POSITION_PCT}"
        )
        assert 0 < self.SPOT_LONG_WALLET_SHARE <= 1, (
            f"SPOT_LONG_WALLET_SHARE must be in (0, 1], got {self.SPOT_LONG_WALLET_SHARE}"
        )
        assert 0 < self.SPOT_SHORT_WALLET_SHARE <= 1, (
            f"SPOT_SHORT_WALLET_SHARE must be in (0, 1], got {self.SPOT_SHORT_WALLET_SHARE}"
        )
        assert self.SPOT_LONG_WALLET_SHARE + self.SPOT_SHORT_WALLET_SHARE <= 1.0 + 1e-9, (
            "SPOT_LONG_WALLET_SHARE + SPOT_SHORT_WALLET_SHARE must be <= 1.0"
        )
        assert self.POLY_RETRY_MAX_ATTEMPTS >= 1, (
            f"POLY_RETRY_MAX_ATTEMPTS must be >= 1, got {self.POLY_RETRY_MAX_ATTEMPTS}"
        )
        assert self.VALIDATION_WINDOW_DAYS >= 1, (
            f"VALIDATION_WINDOW_DAYS must be >= 1, got {self.VALIDATION_WINDOW_DAYS}"
        )


config = Config()
