"""
GPT-5-mini によるチャートパターン品質スコアリング。

score_pattern() はパターン情報を受け取り、0-100 の整数スコアと
その根拠テキストを返す。スコアが高いほど明確で信頼できるパターン。
"""

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class PatternScore:
    score: int          # 0-100
    reason: str


_SYSTEM_PROMPT = """You are a professional FX and gold technical analyst.
Evaluate the quality of a detected chart pattern and return a JSON with exactly two keys:
  "score": integer 0-100 (100 = textbook perfect pattern)
  "reason": single-sentence explanation (max 120 characters, English only)

Scoring guide:
  90-100: Pristine pattern — multiple clean touches, high trendline R², key historical level
   70-89 : Clear pattern — 2-3 good touches, reasonable R², confirms prior structure
   50-69 : Moderate — weak touches or low R², still visible but uncertain
   Below 50: Noisy or incomplete pattern — avoid trading

Respond ONLY with the JSON object, no markdown, no extra text.
"""


class AIScorer:
    """GPT-5-mini を使ってパターン品質を 0-100 で採点するクライアント。"""

    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        try:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=api_key)
        except ImportError:
            self._client = None
            logger.warning("openai package not available; AI scoring disabled")
        self._model = model
        self._enabled = api_key and len(api_key) > 10 and self._client is not None

    async def score_pattern(self, pattern_data: dict) -> PatternScore:
        """
        パターン情報を GPT に送って品質スコアを返す。

        pattern_data の代表的なキー:
          pattern_type: "triangle" | "channel" | "sr_level"
          kind: "symmetrical" | "ascending" | "descending" | "resistance" | "support"
          symbol: "USDJPY"
          current_price: float
          atr: float
          r2_upper: float (任意)
          r2_lower: float (任意)
          touches_upper: int (任意)
          touches_lower: int (任意)
          key_level: float (sr_level のみ)
          touches: int (sr_level のみ)
          apex_bars_ahead: int (triangle のみ)
          width_pips: float (channel のみ)
        """
        if not self._enabled:
            return self._fallback_score(pattern_data)

        user_msg = json.dumps(pattern_data, default=str)

        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.1,
                max_tokens=120,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or "{}"
            parsed = json.loads(raw)
            score = max(0, min(100, int(parsed.get("score", 50))))
            reason = str(parsed.get("reason", ""))[:200]
            return PatternScore(score=score, reason=reason)

        except Exception as e:
            logger.warning(f"AI scoring failed ({e}), using fallback")
            return self._fallback_score(pattern_data)

    def _fallback_score(self, data: dict) -> PatternScore:
        """GPT が使えない場合に R² とタッチ数で機械的に採点する。"""
        r2 = max(
            float(data.get("r2_upper", 0)),
            float(data.get("r2_lower", 0)),
            float(data.get("r2", 0)),
        )
        touches = int(data.get("touches", 0)) + int(data.get("touches_upper", 0)) + int(data.get("touches_lower", 0))

        if r2 >= 0.90 and touches >= 3:
            score = 85
        elif r2 >= 0.75 and touches >= 2:
            score = 70
        elif r2 >= 0.60:
            score = 55
        else:
            score = 40

        return PatternScore(score=score, reason=f"Fallback: r2={r2:.2f}, touches={touches}")
