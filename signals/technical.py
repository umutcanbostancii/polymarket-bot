"""
Technical indicators — RSI, MACD, VWAP, ADX, EMA Trend,
Squeeze Momentum, Heikin-Ashi, EMA Cross, Multi-TF Momentum

v3: PDF strateji harmonizasyonu
- Squeeze Momentum (BB + KC): volatilite sikismasi → breakout tespiti
- Heikin-Ashi Streak: smoothed mum sayaci, trend dogrulama
- EMA 5/20 Cross: hizli trend degisim sinyali (@SolStine terminal)
- Modified Kelly: f = (b×p - q) / b × sqrt(p) (Roan makale)
"""

import math
from typing import List, Optional, Tuple


def rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """Relative Strength Index (Wilder's EMA smoothing)"""
    if len(prices) < period + 1:
        return None

    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]

    # Seed with SMA over first `period` deltas
    first_gains = [max(d, 0) for d in deltas[:period]]
    first_losses = [-min(d, 0) for d in deltas[:period]]
    avg_gain = sum(first_gains) / period
    avg_loss = sum(first_losses) / period

    # Wilder's smoothing for remaining deltas
    for d in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + (-min(d, 0))) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD line, signal line, histogram"""
    if len(prices) < slow + signal:
        return None, None, None

    def ema(data, n):
        k = 2 / (n + 1)
        result = [data[0]]
        for i in range(1, len(data)):
            result.append(data[i] * k + result[-1] * (1 - k))
        return result

    ema_fast = ema(prices, fast)
    ema_slow = ema(prices, slow)

    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]

    return macd_line[-1], signal_line[-1], histogram[-1]


def adx(prices: List[float], period: int = 14) -> Optional[float]:
    """
    Average Directional Index - trend gucu olcer.
    ADX < 20: Yatay/testere piyasa (trade ACMA)
    ADX 20-40: Trend basliyor
    ADX > 40: Guclu trend

    @evrenkececi onerisi: "Testere piyasasinda en iyi botlar bile patlar"
    """
    if len(prices) < period * 2 + 1:
        return None

    highs = prices  # Tek fiyat serisiyle calisiyoruz, high/low simule
    lows = prices

    # True Range ve Directional Movement hesapla
    plus_dm_list = []
    minus_dm_list = []
    tr_list = []

    for i in range(1, len(prices)):
        high_diff = prices[i] - prices[i-1]
        low_diff = prices[i-1] - prices[i]

        # +DM ve -DM
        plus_dm = max(high_diff, 0) if high_diff > low_diff else 0
        minus_dm = max(low_diff, 0) if low_diff > high_diff else 0
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

        # True Range (basitlestirilmis - sadece fiyat farki)
        tr = abs(prices[i] - prices[i-1])
        tr_list.append(max(tr, 0.0001))  # Sifir bolunme korunmasi

    if len(tr_list) < period * 2:
        return None

    # Wilder's smoothing
    def wilder_smooth(data, n):
        result = [sum(data[:n])]
        for i in range(n, len(data)):
            result.append(result[-1] - result[-1] / n + data[i])
        return result

    smooth_tr = wilder_smooth(tr_list, period)
    smooth_plus_dm = wilder_smooth(plus_dm_list, period)
    smooth_minus_dm = wilder_smooth(minus_dm_list, period)

    # +DI ve -DI hesapla
    dx_list = []
    min_len = min(len(smooth_tr), len(smooth_plus_dm), len(smooth_minus_dm))
    for i in range(min_len):
        if smooth_tr[i] == 0:
            continue
        plus_di = 100 * smooth_plus_dm[i] / smooth_tr[i]
        minus_di = 100 * smooth_minus_dm[i] / smooth_tr[i]

        di_sum = plus_di + minus_di
        if di_sum == 0:
            dx_list.append(0)
        else:
            dx = 100 * abs(plus_di - minus_di) / di_sum
            dx_list.append(dx)

    if len(dx_list) < period:
        return None

    # ADX = Wilder's smoothed DX
    adx_val = sum(dx_list[:period]) / period
    for dx_val in dx_list[period:]:
        adx_val = (adx_val * (period - 1) + dx_val) / period

    return adx_val


def ema_trend(prices: List[float], short_period: int = 20, long_period: int = 50) -> Optional[int]:
    """
    EMA Trend filtresi.
    Returns:
        +1: Yukselis trendi (short EMA > long EMA)
        -1: Dusus trendi (short EMA < long EMA)
        0: Trend belirsiz (EMA'lar cok yakin)
    """
    if len(prices) < long_period + 5:
        return None

    def calc_ema(data, n):
        k = 2 / (n + 1)
        val = data[0]
        for p in data[1:]:
            val = p * k + val * (1 - k)
        return val

    short_ema = calc_ema(prices, short_period)
    long_ema = calc_ema(prices, long_period)

    # EMA farki yuzde olarak
    diff_pct = (short_ema - long_ema) / long_ema

    if diff_pct > 0.001:    # %0.1 yukari
        return 1
    elif diff_pct < -0.001:  # %0.1 asagi
        return -1
    else:
        return 0


def vwap(prices: List[float], volumes: List[float]) -> Optional[float]:
    """Volume Weighted Average Price"""
    if not prices or not volumes or len(prices) != len(volumes):
        return None

    total_pv = sum(p * v for p, v in zip(prices, volumes))
    total_v = sum(volumes)

    return total_pv / total_v if total_v > 0 else None


# ======================================================================
# v3 PDF HARMONIZATION - Yeni indikatorler
# ======================================================================


def squeeze_momentum(prices: List[float], bb_period: int = 20,
                     bb_mult: float = 2.0, kc_period: int = 20,
                     kc_mult: float = 1.5) -> Tuple[Optional[bool], Optional[float]]:
    """
    Squeeze Momentum Indicator (John Carter / LazyBear).
    Bollinger Bands Keltner Channel icine girdiginde "squeeze" olusur.
    Squeeze bittiginde buyuk breakout beklenir.

    PDF kaynak: "squeeze momentum indicator with smart money concepts...
    massive expectancy when combined with ADX filter"

    Returns:
        (squeeze_on, momentum_value)
        squeeze_on: True = BB, KC icinde (volatilite sikismis, breakout yaklasıyor)
        momentum_value: pozitif = yukselis momentumu, negatif = dusus
    """
    n = max(bb_period, kc_period)
    if len(prices) < n + 5:
        return None, None

    recent = prices[-(n + 5):]

    # --- Bollinger Bands ---
    bb_data = recent[-bb_period:]
    bb_mean = sum(bb_data) / bb_period
    bb_std = (sum((p - bb_mean) ** 2 for p in bb_data) / bb_period) ** 0.5
    bb_upper = bb_mean + bb_mult * bb_std
    bb_lower = bb_mean - bb_mult * bb_std

    # --- Keltner Channels (ATR-based) ---
    # ATR: ortalama True Range (basitlestirilmis - sadece fiyat serisi)
    tr_list = [abs(recent[i] - recent[i - 1]) for i in range(1, len(recent))]
    if len(tr_list) < kc_period:
        return None, None

    atr = sum(tr_list[-kc_period:]) / kc_period
    kc_data = recent[-kc_period:]
    kc_mean = sum(kc_data) / kc_period
    kc_upper = kc_mean + kc_mult * atr
    kc_lower = kc_mean - kc_mult * atr

    # Squeeze: BB, KC icinde mi?
    squeeze_on = bb_lower > kc_lower and bb_upper < kc_upper

    # Momentum: lineer regresyon kalaninin momentum degeri
    # Basitlestirilmis: son fiyatin ortalamadan sapmasi / std
    current = prices[-1]
    if bb_std > 0:
        momentum_val = (current - bb_mean) / bb_std
    else:
        momentum_val = 0.0

    return squeeze_on, momentum_val


def heikin_ashi_streak(prices: List[float], lookback: int = 10) -> Optional[int]:
    """
    Heikin-Ashi mum serisi uzerinde ardisik bullish/bearish streak sayisi.

    PDF kaynak: "@SolStine terminal - Heikin-Ashi candle streak count"
    Trend dogrulama icin kullanilir: streak >= 3 = guclu trend.

    HA Close = (Open + High + Low + Close) / 4
    Tek fiyat serisiyle calisiyoruz (OHLC yok), bu yuzden
    HA yaklasimi: 2-bar HA close karsilastirmasi.

    Returns:
        Pozitif int: ardisik bullish mum sayisi (yukselis)
        Negatif int: ardisik bearish mum sayisi (dusus)
        0: karisik / belirsiz
    """
    if len(prices) < lookback + 2:
        return None

    recent = prices[-(lookback + 2):]

    # Heikin-Ashi close hesapla (basitlestirilmis: 2-bar ortalama)
    ha_close = []
    for i in range(1, len(recent)):
        ha_c = (recent[i - 1] + recent[i]) / 2.0
        ha_close.append(ha_c)

    # HA open hesapla (onceki HA open + onceki HA close) / 2
    ha_open = [ha_close[0]]
    for i in range(1, len(ha_close)):
        ha_open.append((ha_open[-1] + ha_close[i - 1]) / 2.0)

    # Streak say: HA close > HA open = bullish, tersi bearish
    streak = 0
    for i in range(len(ha_close) - 1, -1, -1):
        if ha_close[i] > ha_open[i]:
            if streak >= 0:
                streak += 1
            else:
                break
        elif ha_close[i] < ha_open[i]:
            if streak <= 0:
                streak -= 1
            else:
                break
        else:
            break

    return streak


def ema_cross(prices: List[float], fast: int = 5, slow: int = 20) -> Optional[int]:
    """
    Hizli EMA / yavas EMA cross sinyali.

    PDF kaynak: "@SolStine terminal - EMA 5 / EMA 20 cross"
    Bu bizim mevcut EMA 20/50 trend filtresine ek olarak daha hizli bir sinyal.

    Returns:
        +1: Fast EMA > Slow EMA (bullish cross)
        -1: Fast EMA < Slow EMA (bearish cross)
         0: Cok yakin (neutral)
    """
    if len(prices) < slow + 3:
        return None

    def calc_ema(data, n):
        k = 2 / (n + 1)
        val = data[0]
        for p in data[1:]:
            val = p * k + val * (1 - k)
        return val

    fast_ema = calc_ema(prices, fast)
    slow_ema = calc_ema(prices, slow)

    diff_pct = (fast_ema - slow_ema) / slow_ema if slow_ema else 0

    if diff_pct > 0.0005:  # %0.05 - daha hassas esik (hizli EMA)
        return 1
    elif diff_pct < -0.0005:
        return -1
    return 0


def modified_kelly(edge: float, win_prob: float, loss_ratio: float = 1.0) -> float:
    """
    Modified Kelly Criterion - PDF'deki Roan makalesi.

    Standart Kelly: f = (b*p - q) / b
    Modified:       f = (b*p - q) / b * sqrt(p)

    sqrt(p) carpani execution riski icin konservatif ayar.
    Dusuk win probability'de cok daha kucuk pozisyon acar.

    PDF kaynak: "Modified Kelly criterion accounting for execution risk:
    f = (b×p - q) / b × sqrt(p)  --  Cap at 50% of order book depth"

    Args:
        edge: beklenen kar orani (ornegin 0.02 = %2)
        win_prob: kazanma olasiligi (0-1 arasi)
        loss_ratio: kazanc/kayip orani (default 1:1)

    Returns:
        Optimal pozisyon yuzdesi (0-1 arasi, 0 = trade acma)
    """
    if win_prob <= 0 or win_prob >= 1 or edge <= 0:
        return 0.0

    b = loss_ratio
    p = win_prob
    q = 1 - p

    standard_kelly = (b * p - q) / b
    if standard_kelly <= 0:
        return 0.0

    # Modified: sqrt(p) ile scale (execution risk discount)
    modified = standard_kelly * math.sqrt(p)

    # Cap at reasonable maximum (PDF: 50% of order book depth)
    return min(max(modified, 0.0), 0.5)
