"""PDF s.35: AI-driven market analysis with DeepSeek.
Her yeni 5-dakika penceresi basinda 1x cagrilir."""

import json
import logging
from typing import Optional

log = logging.getLogger(__name__)


class DeepSeekPredictor:

    def __init__(self, api_key: str, model: str = "deepseek-chat"):
        self.api_key = api_key
        self.model = model
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url="https://api.deepseek.com",
            )
            return self._client
        except ImportError:
            log.error("openai package required for DeepSeek: pip install openai>=1.0.0")
            return None

    async def predict(self, market_data: dict) -> Optional[dict]:
        """
        Input: {
            "klines_30": [...],           # Son 30 mum OHLCV
            "price_changes": {"1h": %, "4h": %, "24h": %},
            "rsi": float,
            "macd": {"macd":, "signal":, "histogram":},
            "bollinger_position": float,
            "volume_trend": float,
            "ob_imbalance": float,
            "poly_spread": float,
        }

        Output: {
            "direction": "up" | "down" | "neutral",
            "confidence": 0.0-1.0,
            "reasoning": "brief explanation",
        }
        """
        client = self._get_client()
        if not client:
            return None

        prompt = self._build_prompt(market_data)

        try:
            resp = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a BTC short-term price prediction model. Respond ONLY with valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=150,
            )
            text = resp.choices[0].message.content.strip()
            return self._parse_response(text)
        except Exception as e:
            log.error(f"DeepSeek prediction error: {e}")
            return None

    def _build_prompt(self, data: dict) -> str:
        """Token-optimize edilmis prompt. ~1500 input token, ~100 output token."""
        klines = data.get("klines_30", [])

        # Compress klines to just close prices and volumes
        closes = [k.get("close", 0) for k in klines[-15:]]
        volumes = [round(k.get("volume", 0), 1) for k in klines[-15:]]

        pc = data.get("price_changes", {})
        rsi_val = data.get("rsi")
        macd_val = data.get("macd", {})
        bb_pos = data.get("bollinger_position")
        vol_trend = data.get("volume_trend")
        obi = data.get("ob_imbalance")
        spread = data.get("poly_spread")

        return f"""BTC 5-minute prediction. Will BTC go UP or DOWN in next 5 minutes?

Recent 15 closes: {[round(c, 1) for c in closes]}
Recent 15 volumes: {volumes}
Price changes: 1h={pc.get('1h', 'N/A')}%, 4h={pc.get('4h', 'N/A')}%, 24h={pc.get('24h', 'N/A')}%
RSI(14): {f'{rsi_val:.1f}' if rsi_val is not None else 'N/A'}
MACD: {json.dumps({k: round(v, 2) for k, v in macd_val.items()}) if macd_val else 'N/A'}
Bollinger position: {f'{bb_pos:.2f}' if bb_pos is not None else 'N/A'} (0=lower,0.5=mid,1=upper)
Volume trend: {f'{vol_trend:.2f}' if vol_trend is not None else 'N/A'}
Orderbook imbalance: {f'{obi:.3f}' if obi is not None else 'N/A'} (-1=sell,+1=buy)
Polymarket spread: {f'{spread:.4f}' if spread is not None else 'N/A'}

Respond with JSON only: {{"direction": "up"|"down"|"neutral", "confidence": 0.0-1.0, "reasoning": "brief"}}"""

    def _parse_response(self, text: str) -> Optional[dict]:
        """JSON extract from LLM response."""
        # Try direct parse first
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to find JSON in text
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    data = json.loads(text[start:end])
                except json.JSONDecodeError:
                    log.warning(f"Failed to parse DeepSeek response: {text[:200]}")
                    return None
            else:
                log.warning(f"No JSON in DeepSeek response: {text[:200]}")
                return None

        direction = data.get("direction", "neutral").lower()
        if direction not in ("up", "down", "neutral"):
            direction = "neutral"

        confidence = float(data.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))

        return {
            "direction": direction,
            "confidence": confidence,
            "reasoning": str(data.get("reasoning", ""))[:200],
        }
