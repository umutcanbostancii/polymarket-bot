"""
Trade Analysis Module - Skorlama ve Pattern Recognition

Her trade'in kalitesini puanla:
- Karar parametrelerini analiz et
- Kazan/kaybet skorları hesapla
- Desenleri tespit et
- AI'ya veri sağla
"""

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

log = logging.getLogger("trade_analysis")

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trades.db")


class TradeAnalyzer:
    """Trade'leri analiz eder ve skorlar."""
    
    def __init__(self):
        self._ensure_tables()
    
    def _ensure_tables(self):
        """Skor tablosu oluştur."""
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER NOT NULL,
                decision_params TEXT,  -- JSON
                market_context TEXT,   -- JSON
                score REAL DEFAULT 0,
                win INTEGER,           -- 1 = win, 0 = loss
                pnl REAL,
                analyzed_at TEXT,
                pattern_tags TEXT,     -- JSON array
                notes TEXT
            )
        """)
        conn.commit()
        conn.close()
    
    def analyze_trade(self, trade_id: int, notes: str, pnl: float) -> Dict:
        """Trade'i analiz et ve skorla."""
        conn = sqlite3.connect(DB_PATH)
        
        # Trade bilgilerini al
        row = conn.execute(
            "SELECT id, side, price, cost, ts, notes, pnl FROM trades WHERE id = ?",
            (trade_id,)
        ).fetchone()
        
        if not row:
            log.warning(f"Trade #{trade_id} not found")
            return {}
        
        trade_id, side, price, cost, ts, notes, pnl = row
        win = 1 if pnl > 0 else 0
        
        # Decision parametrelerini parse et
        decision_params = self._parse_decision_params(notes or "")
        
        # Market context'i parse et
        market_context = self._parse_market_context(ts)
        
        # Skor hesapla
        score = self._calculate_score(win, pnl, decision_params, market_context)
        
        # Pattern'leri tespit et
        pattern_tags = self._detect_patterns(decision_params, market_context, win, pnl)
        
        # Veritabanına kaydet
        conn.execute("""
            INSERT INTO trade_scores 
            (trade_id, decision_params, market_context, score, win, pnl, analyzed_at, pattern_tags, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_id,
            json.dumps(decision_params),
            json.dumps(market_context),
            score,
            win,
            pnl,
            datetime.utcnow().isoformat(),
            json.dumps(pattern_tags),
            notes
        ))
        conn.commit()
        conn.close()
        
        log.info(f"Trade #{trade_id} analyzed: score={score:.2f}, win={win}, patterns={pattern_tags}")
        
        return {
            "trade_id": trade_id,
            "score": score,
            "win": win,
            "pnl": pnl,
            "patterns": pattern_tags,
            "decision_params": decision_params
        }
    
    def _parse_decision_params(self, notes: str) -> Dict:
        """Notes'tan karar parametrelerini çek."""
        params = {}
        
        try:
            # Delta
            if "delta=" in notes:
                start = notes.find("delta=") + 6
                end = notes.find(" ", start)
                if end == -1: end = len(notes)
                params["delta"] = float(notes[start:end])
            
            # Momentum
            if "mom=" in notes:
                start = notes.find("mom=") + 4
                end = notes.find(" ", start)
                if end == -1: end = len(notes)
                params["momentum"] = float(notes[start:end])
            
            # Volatility
            if "vol=" in notes:
                start = notes.find("vol=") + 4
                end = notes.find(" ", start)
                if end == -1: end = len(notes)
                params["volatility"] = float(notes[start:end])
            
            # Remaining time
            if "remaining=" in notes:
                start = notes.find("remaining=") + 10
                end = notes.find("s", start)
                params["seconds_remaining"] = float(notes[start:end])
            
            # BTC price
            if "btc=$" in notes:
                start = notes.find("btc=$") + 5
                params["btc_price"] = float(notes[start:].replace(",", ""))
            
        except Exception as e:
            log.warning(f"Failed to parse decision params: {e}")
        
        return params
    
    def _parse_market_context(self, ts: str) -> Dict:
        """Trade zamanından market context çek."""
        try:
            dt = datetime.fromisoformat(ts.replace("+00:00", ""))
            return {
                "hour": dt.hour,
                "minute": dt.minute,
                "day_of_week": dt.weekday(),
                "timestamp": ts
            }
        except:
            return {}
    
    def _calculate_score(self, win: int, pnl: float, decision_params: Dict, market_context: Dict) -> float:
        """Trade'in kalitesini puanla (0-100)."""
        score = 50.0  # Base score
        
        # Win bonus/penalty
        if win:
            score += 25.0
            # PNL'a göre extra bonus
            if pnl > 30:
                score += 15.0
            elif pnl > 10:
                score += 10.0
        else:
            score -= 25.0
            if pnl < -30:
                score -= 15.0
        
        # Decision quality bonus
        if decision_params:
            # Delta güçlüyse bonus
            delta = abs(decision_params.get("delta", 0))
            if delta > 0.0005:
                score += 10.0
            elif delta > 0.001:
                score += 15.0
            
            # Momentum uyumluysa bonus
            momentum = decision_params.get("momentum", 0)
            delta = decision_params.get("delta", 0)
            if momentum * delta > 0:  # Aynı yönde
                score += 10.0
            
            # İdeal zamanda girildiyse (30-90s arası)
            remaining = decision_params.get("seconds_remaining", 999)
            if 30 <= remaining <= 90:
                score += 10.0
        
        # Time of day bonus
        hour = market_context.get("hour", 12)
        if 2 <= hour <= 6:  # Early morning - often better
            score += 5.0
        
        return max(0.0, min(100.0, score))
    
    def _detect_patterns(self, decision_params: Dict, market_context: Dict, win: int, pnl: float) -> List[str]:
        """Desenleri tespit et."""
        patterns = []
        
        # Time patterns
        hour = market_context.get("hour", 12)
        if 2 <= hour <= 6:
            patterns.append("early_morning")
        elif 8 <= hour <= 12:
            patterns.append("morning")
        elif 14 <= hour <= 18:
            patterns.append("afternoon")
        elif 20 <= hour <= 24:
            patterns.append("evening")
        
        # Decision patterns
        if decision_params:
            delta = abs(decision_params.get("delta", 0))
            if delta > 0.001:
                patterns.append("strong_delta")
            elif delta > 0.0005:
                patterns.append("medium_delta")
            else:
                patterns.append("weak_delta")
            
            momentum = decision_params.get("momentum", 0)
            if momentum * delta > 0:
                patterns.append("momentum_aligned")
            else:
                patterns.append("momentum_divergent")
            
            remaining = decision_params.get("seconds_remaining", 999)
            if remaining < 30:
                patterns.append("late_entry")
            elif remaining > 120:
                patterns.append("early_entry")
        
        # Outcome patterns
        if win and pnl > 40:
            patterns.append("big_win")
        elif win:
            patterns.append("win")
        elif pnl < -20:
            patterns.append("big_loss")
        else:
            patterns.append("loss")
        
        return patterns
    
    def get_pattern_stats(self) -> Dict:
        """Desen istatistiklerini getir."""
        conn = sqlite3.connect(DB_PATH)
        
        # Win rate by pattern
        stats = {}
        
        # Get all patterns
        rows = conn.execute("""
            SELECT pattern_tags, win, score, pnl 
            FROM trade_scores 
            WHERE pattern_tags IS NOT NULL
        """).fetchall()
        
        pattern_wins = {}
        pattern_counts = {}
        
        for row in rows:
            tags_json, win, score, pnl = row
            if not tags_json:
                continue
            try:
                tags = json.loads(tags_json)
                for tag in tags:
                    if tag not in pattern_wins:
                        pattern_wins[tag] = 0
                        pattern_counts[tag] = 0
                    pattern_counts[tag] += 1
                    if win:
                        pattern_wins[tag] += 1
            except:
                pass
        
        # Calculate win rates
        for tag in pattern_counts:
            stats[tag] = {
                "count": pattern_counts[tag],
                "wins": pattern_wins.get(tag, 0),
                "win_rate": (pattern_wins.get(tag, 0) / pattern_counts[tag] * 100) if pattern_counts[tag] > 0 else 0
            }
        
        conn.close()
        return stats
    
    def get_recent_analysis(self, limit: int = 10) -> List[Dict]:
        """Son analizleri getir."""
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT trade_id, score, win, pnl, pattern_tags, analyzed_at
            FROM trade_scores
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()
        
        results = []
        for row in rows:
            results.append({
                "trade_id": row[0],
                "score": row[1],
                "win": bool(row[2]),
                "pnl": row[3],
                "patterns": json.loads(row[4]) if row[4] else [],
                "analyzed_at": row[5]
            })
        
        conn.close()
        return results
    
    def get_ai_context(self) -> str:
        """AI için context oluştur."""
        stats = self.get_pattern_stats()
        recent = self.get_recent_analysis(5)
        
        context = "## Trade Analysis Context\n\n"
        context += "### Best Performing Patterns:\n"
        
        # Sort by win rate
        sorted_patterns = sorted(stats.items(), key=lambda x: x[1]["win_rate"], reverse=True)
        for pattern, data in sorted_patterns[:10]:
            context += f"- {pattern}: {data['win_rate']:.1f}% win rate ({data['wins']}/{data['count']})\n"
        
        context += "\n### Recent Trades:\n"
        for trade in recent:
            status = "✅ WIN" if trade["win"] else "❌ LOSS"
            context += f"- #{trade['trade_id']}: {status} | Score: {trade['score']:.1f} | PnL: ${trade['pnl']:.2f} | Patterns: {', '.join(trade['patterns'])}\n"
        
        return context


# Singleton
analyzer = TradeAnalyzer()
