"""Technical signals for BTC 5-min fast loop. Momentum, volatility, RSI, MACD, BB, OBI."""

import math
from typing import Dict, List, Optional


def momentum(prices: List[float], lookback: int = 30) -> Optional[float]:
    """
    Simple momentum: price change over last N data points.
    Returns fraction (e.g., 0.001 = 0.1% move).
    """
    if len(prices) < lookback + 1:
        return None
    old = prices[-(lookback + 1)]
    new = prices[-1]
    if old == 0:
        return None
    return (new - old) / old


def volatility(prices: List[float], lookback: int = 60) -> Optional[float]:
    """
    Realized volatility: std dev of tick-to-tick returns.
    """
    if len(prices) < lookback + 1:
        return None
    recent = prices[-lookback:]
    rets = []
    for i in range(1, len(recent)):
        if recent[i - 1] == 0:
            continue
        rets.append((recent[i] - recent[i - 1]) / recent[i - 1])
    if len(rets) < 5:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return math.sqrt(var)


def trend_direction(prices: List[float], lookback: int = 30) -> Optional[int]:
    """
    Simple trend direction based on linear regression slope.
    Returns +1 (up), -1 (down), or 0 (flat).
    """
    if len(prices) < lookback:
        return None
    recent = prices[-lookback:]
    n = len(recent)
    x_mean = (n - 1) / 2.0
    y_mean = sum(recent) / n

    num = sum((i - x_mean) * (recent[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))

    if den == 0:
        return 0

    slope = num / den
    # Normalize slope by price level
    slope_pct = slope / y_mean if y_mean else 0

    if slope_pct > 0.00001:
        return 1
    elif slope_pct < -0.00001:
        return -1
    return 0


def rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """RSI(14) - PDF s.21"""
    if len(prices) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(-period, 0):
        change = prices[i] - prices[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(prices: List[float], fast: int = 12, slow: int = 26, signal_period: int = 9) -> Optional[Dict[str, float]]:
    """MACD(12/26/9) + signal + histogram - PDF s.21"""
    if len(prices) < slow + signal_period:
        return None

    def _ema(data: List[float], period: int) -> List[float]:
        k = 2.0 / (period + 1)
        result = [data[0]]
        for i in range(1, len(data)):
            result.append(data[i] * k + result[-1] * (1 - k))
        return result

    fast_ema = _ema(prices, fast)
    slow_ema = _ema(prices, slow)

    macd_line = [f - s for f, s in zip(fast_ema, slow_ema)]
    signal_line = _ema(macd_line[slow:], signal_period)

    if not signal_line:
        return None

    current_macd = macd_line[-1]
    current_signal = signal_line[-1]
    return {
        "macd": current_macd,
        "signal": current_signal,
        "histogram": current_macd - current_signal,
    }


def bollinger_bands(prices: List[float], period: int = 20, std_mult: float = 2.0) -> Optional[Dict[str, float]]:
    """Bollinger Bands - PDF s.21"""
    if len(prices) < period:
        return None
    recent = prices[-period:]
    middle = sum(recent) / period
    variance = sum((p - middle) ** 2 for p in recent) / period
    std = math.sqrt(variance)

    upper = middle + std_mult * std
    lower = middle - std_mult * std

    current = prices[-1]
    band_width = upper - lower
    position = (current - lower) / band_width if band_width > 0 else 0.5

    return {
        "upper": upper,
        "middle": middle,
        "lower": lower,
        "position": max(0.0, min(1.0, position)),
    }


def orderbook_imbalance(bids: list, asks: list) -> Optional[float]:
    """OBI = (bid_vol - ask_vol) / (bid_vol + ask_vol) — PDF s.21
    -1.0 (tam satis baskisi) to +1.0 (tam alis baskisi)."""
    if not bids and not asks:
        return None
    bid_vol = sum(float(b[1]) for b in bids) if bids else 0.0
    ask_vol = sum(float(a[1]) for a in asks) if asks else 0.0
    total = bid_vol + ask_vol
    if total == 0:
        return 0.0
    return (bid_vol - ask_vol) / total


def volume_trend(volumes: List[float], short: int = 5, long: int = 20) -> Optional[float]:
    """Kisa vadeli volume / uzun vadeli volume orani."""
    if len(volumes) < long:
        return None
    short_avg = sum(volumes[-short:]) / short
    long_avg = sum(volumes[-long:]) / long
    if long_avg == 0:
        return None
    return short_avg / long_avg
