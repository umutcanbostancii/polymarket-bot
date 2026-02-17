import os
from dataclasses import dataclass

from dotenv import load_dotenv
load_dotenv()


@dataclass
class Config:
    # Mode
    PAPER_TRADING: bool = True
    PID_LOCK_PATH: str = "/tmp/polymarket_bot.pid"
    STATUS_FILE: str = "/tmp/polymarket_bot_status.json"
    TRADE_SIZE_FILE: str = "/tmp/polymarket_bot_trade_size"

    # Polymarket
    GAMMA_API: str = "https://gamma-api.polymarket.com"
    CLOB_API: str = "https://clob.polymarket.com"
    POLY_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/"

    # Polymarket CLOB API Credentials
    POLY_API_KEY: str = os.getenv("POLY_API_KEY", "")
    POLY_API_SECRET: str = os.getenv("POLY_API_SECRET", "")
    POLY_API_PASSPHRASE: str = os.getenv("POLY_API_PASSPHRASE", "")
    POLY_PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")

    # Binance (public, no account needed)
    BINANCE_WS: str = "wss://stream.binance.com:9443/ws"
    BINANCE_REST: str = "https://api.binance.com/api/v3"

    # ── BTC 5-min Strategy Parameters ──
    TRADE_SIZE_USD: float = float(os.getenv("TRADE_SIZE_USD", "1"))
    MAX_LIVE_TEST_TRADES: int = 3  # test: max 3 live trade sonra dur
    EDGE_THRESHOLD: float = 0.01          # minimum net edge to enter (1%)
    DELTA_THRESHOLD: float = 0.00005      # minimum |delta| to filter noise (0.005%)
    MIN_SECONDS_REMAINING: int = 30        # son 30s'de girme (çok geç)
    MAX_SECONDS_REMAINING: int = 180       # ilk 120s giriş yok (gözlem fazı)
    LOOP_INTERVAL: float = 2.0            # fast-loop interval (seconds)
    MIN_DELTA_FOR_ENTRY: float = 0.0003    # %0.03 minimum sinyal gücü (gürültü filtresi)
    OBSERVATION_CONSISTENCY_MIN: float = 0.55  # gözlem trend tutarlılığı alt sınırı
    MAX_MOMENTUM_FOR_ENTRY: float = 0.0005    # 5dk: momentum > %0.05 ise trend uzamis
    MAX_ENTRY_PRICE: float = 0.80             # fiyat > 0.80 ise girme (risk/odul kotu)

    # Edge model tuning
    MOMENTUM_WEIGHT: float = 50.0         # how much delta amplifies probability
    REVERSAL_RISK_WEIGHT: float = 0.5     # how much volatility penalizes
    SLIPPAGE_ESTIMATE: float = 0.0        # paper mode: no slippage
    FEE_BUFFER: float = 0.0              # paper mode: no extra buffer

    # DeepSeek AI (PDF s.35)
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_MODEL: str = "deepseek-chat"
    AI_CONFIDENCE_THRESHOLD: float = 0.55    # min confidence to use AI signal
    AI_BOOST_WEIGHT: float = 0.15            # AI contribution to edge

    # ── BTC 15-min Strategy Parameters ──
    MARKET_15M_ENABLED: bool = False
    MARKET_15M_FILE: str = "/tmp/polymarket_bot_15m_enabled"
    MIN_SECONDS_REMAINING_15M: int = 60
    MAX_SECONDS_REMAINING_15M: int = 420       # ilk 480s gozlem (900-420=480), son 7dk giris
    MIN_DELTA_FOR_ENTRY_15M: float = 0.0005
    OBSERVATION_CONSISTENCY_MIN_15M: float = 0.50
    MAX_MOMENTUM_FOR_ENTRY_15M: float = 0.0006  # 15dk: biraz daha toleransli
    MAX_ENTRY_PRICE_15M: float = 0.80            # 15dk icin de ayni

    # Execution Validation (PDF s.35-36)
    MIN_NET_PROFIT_USD: float = 0.001        # $1 test: çok düşük eşik
    MAX_SLIPPAGE_PCT: float = 0.10           # 5-dk marketler ince, %10 tolerans
    MIN_LIQUIDITY_USD: float = 0.5           # $1 test: minimum $0.50 likidite

    # Monitoring Alarms (PDF s.36)
    MAX_DRAWDOWN_PCT: float = 15.0           # drawdown > 15% alarm
    MIN_EXECUTION_RATE_PCT: float = 30.0     # execution rate < 30% alarm

    # Risk Management
    BANKROLL: float = float(os.getenv("BANKROLL", "10000"))
    MAX_DAILY_LOSS_USD: float = float(os.getenv("MAX_DAILY_LOSS_USD", "1000"))
    MAX_CONSECUTIVE_LOSSES: int = 3       # PDF s.90: 3 ardisik kayip
    MAX_SIMULTANEOUS_POSITIONS: int = 20   # Patron istedi: max 20 es zamanli
    KELLY_FRACTION: float = 0.25          # quarter-Kelly sizing

    # Polymarket REST resilience
    POLY_REST_TIMEOUT_SECONDS: int = 10
    POLY_MAX_REQUESTS_PER_SEC: float = 25.0
    POLY_RETRY_MAX_ATTEMPTS: int = 3
    POLY_CIRCUIT_BREAKER_ERRORS: int = 5
    POLY_CIRCUIT_BREAKER_COOLDOWN_SECONDS: int = 25
    POLY_ORDERBOOK_STALE_MS: int = 1200

    # Telegram
    TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Infra
    DB_PATH: str = "trades.db"
    LOG_LEVEL: str = "INFO"
    HALT_RESUME_SECONDS: int = 3600

    def __post_init__(self):
        assert self.BANKROLL > 0
        assert self.MAX_DAILY_LOSS_USD > 0
        assert self.TRADE_SIZE_USD > 0
        assert self.EDGE_THRESHOLD > 0


config = Config()
