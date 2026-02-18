import os
from dataclasses import dataclass

from dotenv import load_dotenv
load_dotenv()


@dataclass
class Config:
    # Mode
    PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "true").lower() not in ("false", "0", "no")
    PID_LOCK_PATH: str = "/tmp/polymarket_bot.pid"
    STATUS_FILE: str = "/tmp/polymarket_bot_status.json"
    DECISIONS_FILE: str = "/tmp/polymarket_bot_decisions.json"
    TRADE_SIZE_FILE: str = "/tmp/polymarket_bot_trade_size"
    ARB_PID_LOCK_PATH: str = "/tmp/poly_arb_bot.pid"
    ARB_STATUS_FILE: str = "/tmp/poly_arb_bot_status.json"
    ARB_RUNTIME_CONFIG_FILE: str = "/tmp/poly_arb_bot_runtime.json"
    ARB_LOG_FILE: str = "/tmp/poly_arb_bot.log"

    # Polymarket
    GAMMA_API: str = "https://gamma-api.polymarket.com"
    CLOB_API: str = "https://clob.polymarket.com"
    POLY_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/"

    # Polymarket CLOB API Credentials
    POLY_API_KEY: str = os.getenv("POLY_API_KEY", "")
    POLY_API_SECRET: str = os.getenv("POLY_API_SECRET", "")
    POLY_API_PASSPHRASE: str = os.getenv("POLY_API_PASSPHRASE", "")
    POLY_PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")
    POLY_FUNDER: str = os.getenv("POLY_FUNDER", "")  # Gnosis Safe proxy wallet address
    # Arbitrage bot isolated credentials
    ARB_POLY_API_KEY: str = os.getenv("ARB_POLY_API_KEY", "")
    ARB_POLY_API_SECRET: str = os.getenv("ARB_POLY_API_SECRET", "")
    ARB_POLY_API_PASSPHRASE: str = os.getenv("ARB_POLY_API_PASSPHRASE", "")
    ARB_POLY_PRIVATE_KEY: str = os.getenv("ARB_POLY_PRIVATE_KEY", "")
    ARB_POLY_FUNDER: str = os.getenv("ARB_POLY_FUNDER", "")

    # Binance (public, no account needed)
    BINANCE_WS: str = "wss://stream.binance.com:9443/ws"
    BINANCE_REST: str = "https://api.binance.com/api/v3"

    # ── BTC 5-min Strategy Parameters ──
    TRADE_SIZE_USD: float = float(os.getenv("TRADE_SIZE_USD", "1"))  # min 5 share = ~$4 efektif
    MAX_LIVE_TEST_TRADES: int = 0  # 0 = limitsiz (eski: 3)
    EDGE_THRESHOLD: float = 0.0095          # minimum net edge to enter (0.95%) - gevşetildi %5
    DELTA_THRESHOLD: float = 0.00005      # minimum |delta| to filter noise (0.005%)
    MIN_SECONDS_REMAINING: int = 30        # son 30s'de girme (çok geç)
    MAX_SECONDS_REMAINING: int = 180       # ilk 120s giriş yok (gözlem fazı)
    LOOP_INTERVAL: float = 2.0            # fast-loop interval (seconds)
    MIN_DELTA_FOR_ENTRY: float = 0.000285    # %0.0285 minimum sinyal gücü (gürültü filtresi) - gevşetildi %5
    OBSERVATION_CONSISTENCY_MIN: float = 0.45    # gözlem trend tutarlılığı alt sınırı (eski: 0.5035)
    MAX_MOMENTUM_FOR_ENTRY: float = 0.0010     # 5dk: momentum cap (eski: 0.0006)
    MAX_ENTRY_PRICE: float = 0.75              # fiyat > 0.75 ise girme (eski: 0.92) - risk/reward düzeltme
    MIN_SIGNAL_STRENGTH_5M: float = 0.35       # zayif sinyalde trade acma
    MIN_REWARD_RISK_RATIO_5M: float = 0.35     # (1-p)/p; tek kaybin mini karlari silmesini azaltir

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

    # ── BTC 5-min Strategy Toggle ──
    MARKET_5M_ENABLED: bool = False           # 5-min strategy OFF (veriler: -$10.35 PnL)
    MARKET_5M_FILE: str = "/tmp/polymarket_bot_5m_enabled"

    # ── BTC 15-min Strategy Parameters ──
    MARKET_15M_ENABLED: bool = True          # 15-min strategy ON (veriler: +$10.73 PnL)
    MARKET_15M_FILE: str = "/tmp/polymarket_bot_15m_enabled"
    MIN_SECONDS_REMAINING_15M: int = 60
    MAX_SECONDS_REMAINING_15M: int = 420       # ilk 480s gozlem (900-420=480), son 7dk giris
    MIN_DELTA_FOR_ENTRY_15M: float = 0.000475  # gevşetildi %5
    OBSERVATION_CONSISTENCY_MIN_15M: float = 0.456  # 15dk consistency - gevşetildi %5
    MAX_MOMENTUM_FOR_ENTRY_15M: float = 0.0012    # 15dk momentum cap (eski: 0.0007)
    MAX_ENTRY_PRICE_15M: float = 0.80             # 15dk max entry (eski: 0.92) - risk/reward düzeltme
    MIN_SIGNAL_STRENGTH_15M: float = 0.35         # zayif sinyalde trade acma
    MIN_REWARD_RISK_RATIO_15M: float = 0.30       # 15dk icin asgari odul/risk

    # Execution Validation (PDF s.35-36)
    MIN_NET_PROFIT_USD: float = 0.001        # mutlak alt limit
    MIN_NET_PROFIT_PCT: float = 0.015        # pozisyonun en az %1.5'i kadar beklenen net kar
    MAX_SLIPPAGE_PCT: float = 0.10           # 5-dk marketler ince, %10 tolerans
    MAX_SPREAD_PCT: float = 0.035            # spread cok aciksa islem acma
    MIN_LIQUIDITY_USD: float = 0.5           # $1 test: minimum $0.50 likidite
    ALLOW_PAPER_VALIDATION_BYPASS: bool = False  # paper/live parity: default bypass yok

    # Monitoring Alarms (PDF s.36)
    MAX_DRAWDOWN_PCT: float = 15.0           # drawdown > 15% alarm
    MIN_EXECUTION_RATE_PCT: float = 30.0     # execution rate < 30% alarm

    # Risk Management
    BANKROLL: float = float(os.getenv("BANKROLL", "10000"))
    MAX_DAILY_LOSS_USD: float = float(os.getenv("MAX_DAILY_LOSS_USD", "1000"))
    MAX_CONSECUTIVE_LOSSES: int = 3       # PDF s.90: 3 ardisik kayip
    HALT_ON_CONSECUTIVE_LOSSES: bool = True
    MAX_SIMULTANEOUS_POSITIONS: int = 4   # Patron istedi: max 4 es zamanli
    KELLY_FRACTION: float = 0.25          # quarter-Kelly sizing
    RISK_POSITION_SIZING_ENABLED: bool = True

    # ── Profit Taking (EXIT) ──
    EXIT_PROFIT_TAKE_ENABLED: bool = True      # profit taking açık
    EXIT_PROFIT_TARGET: float = 0.10           # %10 kar'da sat (konservatif)
    EXIT_CHECK_INTERVAL: float = 5.0           # 5 saniyede bir kontrol
    EXIT_MIN_TIME_BEFORE_END: int = 30         # market bitmeden 30s önce durur
    # NOT: Stop loss YOK — zarar'da resolution'a bırakılır

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

    # ── Test Mode (mevcut parametreler DEGISMEZ) ──
    TEST_MODE_ENABLED: bool = False
    TEST_MODE_FILE: str = "/tmp/polymarket_bot_test_enabled"

    # Rapid signal warmup (artırıldı: daha fazla gözlem)
    TEST_MAX_SECONDS_REMAINING_5M: int = 210    # 90s warmup (eski 270→30s, şimdi 90s)
    TEST_MAX_SECONDS_REMAINING_15M: int = 600   # 300s warmup (eski 720→180s, şimdi 5dk)

    # Test-specific loosened filters (~10% gevşek)
    # Geri almak icin bu degerleri mevcut ana degerlerle degistir
    TEST_SIGNAL_SCORE_THRESHOLD: float = 0.18   # WEAK sinyalleri filtrele (eski: 0.12)
    TEST_SIGNAL_MIN_AGREEMENT: float = 0.65     # sinyal uyumu daha yuksek (eski: 0.50)
    TEST_MAX_ENTRY_PRICE_5M: float = 0.90       # ana ile ayni
    TEST_MAX_ENTRY_PRICE_15M: float = 0.90      # ana ile ayni
    TEST_COOLDOWN_SECONDS: int = 55             # daha uzun bekleme (eski: 35)

    # Sinyal agirliklari
    SIGNAL_SCORE_THRESHOLD: float = 0.15
    SIGNAL_MIN_AGREEMENT: float = 0.60
    SIGNAL_WEIGHT_CANDLE: float = 0.30
    SIGNAL_WEIGHT_SENTIMENT: float = 0.25
    SIGNAL_WEIGHT_DELTA: float = 0.20
    SIGNAL_WEIGHT_MOMENTUM: float = 0.10
    SIGNAL_WEIGHT_TECHNICAL: float = 0.10
    SIGNAL_WEIGHT_AI: float = 0.05

    # Skor tabanli yon belirleme
    MIN_DIRECTION_SCORE: float = 0.5       # 5dk: min net skor (yon esigi)
    MIN_DIRECTION_SCORE_15M: float = 0.5   # 15dk: min net skor (yon esigi)
    CANDLE_SIGNAL_WEIGHT: float = 0.6      # mum sinyali agirligi

    # Mum analizi
    CANDLE_1S_LIMIT_5M: int = 60     # 5dk: son 60 saniye (1s mumlar)
    CANDLE_1S_LIMIT_15M: int = 300   # 15dk: son 300 saniye (1s mumlar)

    # Feedback
    FEEDBACK_ENABLED: bool = True
    FEEDBACK_MIN_TRADES: int = 20
    FEEDBACK_SMOOTHING: float = 0.3

    # ── Arbitrage Bot (45c limit) ──
    ARB_ENABLED: bool = True
    ARB_DEFAULT_MODE: str = "paper"  # paper|live
    ARB_SYMBOLS: str = "BTC,ETH"
    ARB_ENTRY_PRICE_LIMIT: float = 0.45
    ARB_BAILOUT_PRICE: float = 0.72
    ARB_ENTRY_DELAY_SECONDS: int = 15
    ARB_ENTRY_CUTOFF_SECONDS: int = 180
    ARB_BAILOUT_CUTOFF_SECONDS: int = 120
    ARB_SCAN_INTERVAL_SECONDS: float = 5.0
    ARB_ORDER_POLL_SECONDS: float = 4.0
    ARB_ORDER_TIMEOUT_SECONDS: int = 90
    ARB_CYCLE_BUDGET_USD: float = 20.0
    ARB_MAX_ACTIVE_CAPITAL_USD: float = 120.0
    ARB_MAX_OPEN_CYCLES: int = 4
    ARB_MAX_SPREAD_PCT: float = 0.04
    ARB_MIN_LIQUIDITY_USD: float = 100.0
    ARB_ENABLE_DYNAMIC_PRICING: bool = False
    ARB_DYNAMIC_PRICE_MIN: float = 0.44
    ARB_DYNAMIC_PRICE_MAX: float = 0.46
    ARB_DYNAMIC_MIN_CYCLES: int = 120
    ARB_DYNAMIC_MIN_FILL_RATE: float = 0.20
    ARB_DYNAMIC_MAX_BAIL_RATE: float = 0.35
    ARB_MAX_CONSECUTIVE_BAILS: int = 5
    ARB_MAX_CONSECUTIVE_ERRORS: int = 8
    ARB_LOCK_MAIN_STRATEGY: bool = True

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
