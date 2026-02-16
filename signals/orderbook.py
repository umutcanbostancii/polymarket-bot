"""Order book signals — OBI, spread"""

from typing import Dict, Optional


def order_book_imbalance(book: Dict) -> Optional[float]:
    """
    Order Book Imbalance (OBI)
    +1 = tam alım baskısı, -1 = tam satım baskısı
    """
    bids = book.get("bids", [])
    asks = book.get("asks", [])

    if not bids and not asks:
        return None

    bid_vol = sum(float(b.get("size", 0)) for b in bids[:5])
    ask_vol = sum(float(a.get("size", 0)) for a in asks[:5])

    total = bid_vol + ask_vol
    if total == 0:
        return None

    return (bid_vol - ask_vol) / total


def spread(book: Dict) -> Optional[float]:
    """Best bid-ask spread"""
    bids = book.get("bids", [])
    asks = book.get("asks", [])

    if not bids or not asks:
        return None

    best_bid = float(bids[0].get("price", 0))
    best_ask = float(asks[0].get("price", 0))

    return best_ask - best_bid if best_ask > best_bid else None


def liquidity_depth(book: Dict, levels: int = 10) -> float:
    """Top N seviyedeki toplam likidite"""
    bids = book.get("bids", [])[:levels]
    asks = book.get("asks", [])[:levels]

    bid_depth = sum(float(b.get("size", 0)) for b in bids)
    ask_depth = sum(float(a.get("size", 0)) for a in asks)

    return bid_depth + ask_depth
