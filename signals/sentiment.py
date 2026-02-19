"""Orderbook sentiment analysis for Polymarket UP vs DOWN tokens."""

import logging
from typing import Optional

log = logging.getLogger(__name__)


async def analyze_sentiment(
    poly_client,
    up_token: str,
    down_token: str,
) -> dict | None:
    """
    UP ve DOWN token orderbook'larini karsilastir.

    Returns:
        {"direction": "up"|"down"|"neutral",
         "score": -1.0..+1.0,
         "up_metrics": dict,
         "down_metrics": dict,
         "details": str}
    """
    try:
        # Fetch both orderbooks
        up_ob = await poly_client.get_orderbook(up_token)
        down_ob = await poly_client.get_orderbook(down_token)

        if not up_ob or not down_ob:
            return None

        up_metrics = poly_client.get_orderbook_metrics(up_ob)
        down_metrics = poly_client.get_orderbook_metrics(down_ob)

        if not up_metrics or not down_metrics:
            return None

        # 1. Price score: UP midpoint vs 0.50
        up_mid = up_metrics.get("midpoint", 0.5)
        price_score = (up_mid - 0.5) * 2.0  # -1..+1
        price_score = max(-1.0, min(1.0, price_score))

        # 2. Depth score: bid depth comparison
        up_bid_depth = up_metrics.get("bid_depth_usd", 0)
        down_bid_depth = down_metrics.get("bid_depth_usd", 0)
        total_depth = up_bid_depth + down_bid_depth
        if total_depth > 0:
            depth_score = (up_bid_depth - down_bid_depth) / total_depth
        else:
            depth_score = 0.0
        depth_score = max(-1.0, min(1.0, depth_score))

        # 3. Spread score: narrower spread = more confident side
        up_spread = up_metrics.get("spread", 1.0)
        down_spread = down_metrics.get("spread", 1.0)
        total_spread = up_spread + down_spread
        if total_spread > 0:
            # If UP spread is tighter, positive score
            spread_score = (down_spread - up_spread) / total_spread
        else:
            spread_score = 0.0
        spread_score = max(-1.0, min(1.0, spread_score))

        # 4. Imbalance difference
        up_imbalance = up_metrics.get("imbalance", 0)
        down_imbalance = down_metrics.get("imbalance", 0)
        imbalance_diff = up_imbalance - down_imbalance
        imbalance_score = max(-1.0, min(1.0, imbalance_diff))

        # Weighted final score
        final = (
            price_score * 0.40
            + depth_score * 0.30
            + spread_score * 0.10
            + imbalance_score * 0.20
        )
        final = max(-1.0, min(1.0, final))

        if final > 0.05:
            direction = "up"
        elif final < -0.05:
            direction = "down"
        else:
            direction = "neutral"

        details = (
            f"price={price_score:.3f} depth={depth_score:.3f} "
            f"spread={spread_score:.3f} imbalance={imbalance_score:.3f}"
        )

        return {
            "direction": direction,
            "score": round(final, 4),
            "up_metrics": {
                "midpoint": round(up_mid, 4),
                "spread": round(up_spread, 4),
                "bid_depth": round(up_bid_depth, 2),
                "imbalance": round(up_imbalance, 4),
            },
            "down_metrics": {
                "midpoint": round(down_metrics.get("midpoint", 0.5), 4),
                "spread": round(down_spread, 4),
                "bid_depth": round(down_bid_depth, 2),
                "imbalance": round(down_imbalance, 4),
            },
            "details": details,
        }

    except Exception as e:
        log.warning(f"Sentiment analysis error: {e}")
        return None
