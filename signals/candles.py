"""1-second candle analysis for rapid signal strategy."""

import logging
import math
from typing import List, Optional

log = logging.getLogger(__name__)


def analyze_candles(klines: list[dict], window: str = "5m") -> dict | None:
    """
    1-saniye mumlarindan yon ve guc analizi.

    Args:
        klines: [{"open","high","low","close","volume","ts"}, ...]
        window: "5m" (son 60 mum) veya "15m" (son 300 mum)

    Returns:
        {"direction": "up"|"down"|"neutral",
         "score": -1.0..+1.0,
         "pattern": str,
         "trend_strength": 0.0..1.0,
         "volume_confirmation": bool,
         "candle_count": int}
    """
    if not klines or len(klines) < 10:
        return None

    trend_score, trend_r2 = _compute_trend(klines)
    body_score = _body_analysis(klines)
    volume_score = _volume_profile(klines)
    wick_score = _wick_analysis(klines)
    pattern = _micro_patterns(klines)

    # Weighted composite score (-1.0 to +1.0)
    final = (
        trend_score * 0.35
        + body_score * 0.30
        + volume_score * 0.20
        + wick_score * 0.15
    )
    final = max(-1.0, min(1.0, final))

    if final > 0.05:
        direction = "up"
    elif final < -0.05:
        direction = "down"
    else:
        direction = "neutral"

    # Volume confirmation: volume aligns with direction
    vol_confirmed = (
        (direction == "up" and volume_score > 0)
        or (direction == "down" and volume_score < 0)
        or direction == "neutral"
    )

    return {
        "direction": direction,
        "score": round(final, 4),
        "pattern": pattern,
        "trend_strength": round(abs(trend_score) * trend_r2, 4),
        "volume_confirmation": vol_confirmed,
        "candle_count": len(klines),
        "components": {
            "trend": round(trend_score, 4),
            "body": round(body_score, 4),
            "volume": round(volume_score, 4),
            "wick": round(wick_score, 4),
        },
    }


def _compute_trend(klines: list[dict]) -> tuple[float, float]:
    """Linear regression ile trend yonu + R-squared."""
    closes = [k["close"] for k in klines]
    n = len(closes)
    if n < 5:
        return 0.0, 0.0

    x_mean = (n - 1) / 2.0
    y_mean = sum(closes) / n

    num = sum((i - x_mean) * (closes[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den == 0:
        return 0.0, 0.0

    slope = num / den

    # R-squared
    ss_tot = sum((closes[i] - y_mean) ** 2 for i in range(n))
    if ss_tot == 0:
        return 0.0, 0.0

    predicted = [y_mean + slope * (i - x_mean) for i in range(n)]
    ss_res = sum((closes[i] - predicted[i]) ** 2 for i in range(n))
    r_squared = max(0.0, 1.0 - ss_res / ss_tot)

    # Normalize slope by price level
    slope_normalized = slope / y_mean if y_mean else 0.0
    # Scale to -1..+1 range (0.001 per second = very strong trend)
    score = max(-1.0, min(1.0, slope_normalized * 1000))

    return score * r_squared, r_squared


def _body_analysis(klines: list[dict]) -> float:
    """Toplam body momentum: sum(close-open)/sum(range)."""
    total_body = 0.0
    total_range = 0.0

    for k in klines:
        body = k["close"] - k["open"]
        high_low = k["high"] - k["low"]
        total_body += body
        total_range += high_low

    if total_range == 0:
        return 0.0

    return max(-1.0, min(1.0, total_body / total_range))


def _volume_profile(klines: list[dict]) -> float:
    """Hacim yonune bak: yukari mumlarda mi yoksa asagi mumlarda mi hacim fazla."""
    up_volume = 0.0
    down_volume = 0.0

    for k in klines:
        vol = k.get("volume", 0)
        if k["close"] >= k["open"]:
            up_volume += vol
        else:
            down_volume += vol

    total = up_volume + down_volume
    if total == 0:
        return 0.0

    # -1 (all volume on down candles) to +1 (all volume on up candles)
    ratio = (up_volume - down_volume) / total
    return max(-1.0, min(1.0, ratio))


def _wick_analysis(klines: list[dict]) -> float:
    """Rejection wicks: uzun ust fitil = satis baskisi, uzun alt fitil = alis baskisi."""
    total_upper_wick = 0.0
    total_lower_wick = 0.0
    total_range = 0.0

    for k in klines:
        high = k["high"]
        low = k["low"]
        o = k["open"]
        c = k["close"]
        body_high = max(o, c)
        body_low = min(o, c)

        upper_wick = high - body_high
        lower_wick = body_low - low
        candle_range = high - low

        total_upper_wick += upper_wick
        total_lower_wick += lower_wick
        total_range += candle_range

    if total_range == 0:
        return 0.0

    # Lower wicks = buying pressure (positive), upper wicks = selling pressure (negative)
    score = (total_lower_wick - total_upper_wick) / total_range
    return max(-1.0, min(1.0, score))


def _micro_patterns(klines: list[dict]) -> str:
    """Son 10 mumda: V-reversal, spike, consolidation, breakout."""
    if len(klines) < 10:
        return "insufficient_data"

    recent = klines[-10:]
    closes = [k["close"] for k in recent]
    ranges = [k["high"] - k["low"] for k in recent]

    # Consolidation: low range variance
    avg_range = sum(ranges) / len(ranges) if ranges else 0
    if avg_range == 0:
        return "flat"

    range_variance = sum((r - avg_range) ** 2 for r in ranges) / len(ranges)
    range_cv = math.sqrt(range_variance) / avg_range if avg_range > 0 else 0

    # V-reversal: first half down, second half up (or vice versa)
    mid = len(closes) // 2
    first_half_change = closes[mid] - closes[0]
    second_half_change = closes[-1] - closes[mid]

    if closes[0] != 0:
        first_pct = abs(first_half_change / closes[0])
        second_pct = abs(second_half_change / closes[mid]) if closes[mid] != 0 else 0

        if first_pct > 0.0001 and second_pct > 0.0001:
            if first_half_change < 0 and second_half_change > 0:
                return "v_reversal_up"
            elif first_half_change > 0 and second_half_change < 0:
                return "v_reversal_down"

    # Spike: last 3 candles have large range
    last3_avg_range = sum(ranges[-3:]) / 3
    if last3_avg_range > avg_range * 2:
        if closes[-1] > closes[-3]:
            return "spike_up"
        else:
            return "spike_down"

    # Breakout: price moves beyond recent range
    recent_high = max(k["high"] for k in klines[-10:-3])
    recent_low = min(k["low"] for k in klines[-10:-3])
    if closes[-1] > recent_high:
        return "breakout_up"
    elif closes[-1] < recent_low:
        return "breakout_down"

    # Consolidation: low CV
    if range_cv < 0.5:
        return "consolidation"

    return "mixed"
