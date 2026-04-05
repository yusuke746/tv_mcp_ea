"""
Market Context 送信モジュール。

5分毎のスキャンで検出した S/R レベル・スイング高安・EMA 方向を
fx_system の /webhook/mcp_context エンドポイントに POST し、
保有中ポジションのエグジット判断に活用させる。
"""

import logging
from dataclasses import dataclass

import aiohttp
import pandas as pd

from data_feed import OHLCVData
from detection.sr_levels import SRLevel
from detection.swing import SwingPoint

logger = logging.getLogger(__name__)


@dataclass
class MarketContext:
    pair: str
    current_price: float
    atr: float
    nearest_resistance: float  # 現在価格より上の最寄り S/R (0 = なし)
    nearest_support: float     # 現在価格より下の最寄り S/R (0 = なし)
    resistance_strength: int   # タッチ回数
    support_strength: int
    swing_high: float          # 直近スイング高値
    swing_low: float           # 直近スイング安値
    htf_bias: str              # "long" | "short" | "neutral"
    ema20_1h: float
    ema50_1h: float


class ContextSender:
    """保有ペアの市場コンテキストを fx_system に送信する。"""

    def __init__(self, webhook_url: str, webhook_token: str, context_webhook_url: str | None = None):
        # 既定: /webhook/mcp → /webhook/mcp_context
        base = webhook_url.rstrip("/").replace("/webhook/mcp", "/webhook/mcp_context")
        if "/mcp_context" not in base:
            base = webhook_url.rstrip("/") + "_context"
        self._url = (context_webhook_url or base).rstrip("/")
        self._token = webhook_token

    def _candidate_urls(self) -> list[str]:
        """環境差異向けのフォールバック候補 URL を返す。"""
        urls = [self._url]
        # localhost のみ 80/8000 を相互フォールバック
        if "localhost:80/" in self._url:
            urls.append(self._url.replace("localhost:80/", "localhost:8000/"))
        elif "localhost:8000/" in self._url:
            urls.append(self._url.replace("localhost:8000/", "localhost:80/"))
        # 重複削除
        seen = set()
        unique = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                unique.append(u)
        return unique

    def build_context(
        self,
        mt5_symbol: str,
        current_price: float,
        ohlcv_15m: OHLCVData,
        ohlcv_1h: OHLCVData | None,
        sr_levels: list[SRLevel],
        swings: list[SwingPoint],
    ) -> MarketContext:
        """検出結果から MarketContext を構築する。"""

        # 最寄りの抵抗線・支持線を探す
        nearest_res = 0.0
        nearest_sup = 0.0
        res_strength = 0
        sup_strength = 0

        resistances = sorted(
            [s for s in sr_levels if s.price > current_price],
            key=lambda s: s.price,
        )
        supports = sorted(
            [s for s in sr_levels if s.price < current_price],
            key=lambda s: s.price,
            reverse=True,
        )

        if resistances:
            nearest_res = resistances[0].price
            res_strength = resistances[0].touches
        if supports:
            nearest_sup = supports[0].price
            sup_strength = supports[0].touches

        # 直近スイング高値・安値
        recent_highs = sorted(
            [s for s in swings if s.kind == "high"],
            key=lambda s: s.bar_index,
            reverse=True,
        )
        recent_lows = sorted(
            [s for s in swings if s.kind == "low"],
            key=lambda s: s.bar_index,
            reverse=True,
        )
        swing_high = recent_highs[0].price if recent_highs else 0.0
        swing_low = recent_lows[0].price if recent_lows else 0.0

        # HTF バイアス (1H EMA20 vs EMA50)
        ema20 = 0.0
        ema50 = 0.0
        htf_bias = "neutral"
        if ohlcv_1h is not None and len(ohlcv_1h.bars) >= 52:
            df_1h = ohlcv_1h.bars
            ema20 = float(df_1h["close"].ewm(span=20, adjust=False).mean().iloc[-1])
            ema50 = float(df_1h["close"].ewm(span=50, adjust=False).mean().iloc[-1])
            if ema20 > ema50:
                htf_bias = "long"
            elif ema20 < ema50:
                htf_bias = "short"

        return MarketContext(
            pair=mt5_symbol,
            current_price=current_price,
            atr=ohlcv_15m.atr14,
            nearest_resistance=nearest_res,
            nearest_support=nearest_sup,
            resistance_strength=res_strength,
            support_strength=sup_strength,
            swing_high=swing_high,
            swing_low=swing_low,
            htf_bias=htf_bias,
            ema20_1h=round(ema20, 5),
            ema50_1h=round(ema50, 5),
        )

    async def send(self, ctx: MarketContext) -> bool:
        """Market Context を fx_system に POST する。"""
        payload = {
            "signal_source": "mcp_context",
            "pair": ctx.pair,
            "current_price": ctx.current_price,
            "atr": ctx.atr,
            "nearest_resistance": ctx.nearest_resistance,
            "nearest_support": ctx.nearest_support,
            "resistance_strength": ctx.resistance_strength,
            "support_strength": ctx.support_strength,
            "swing_high": ctx.swing_high,
            "swing_low": ctx.swing_low,
            "htf_bias": ctx.htf_bias,
            "ema20_1h": ctx.ema20_1h,
            "ema50_1h": ctx.ema50_1h,
            "webhook_token": self._token,
        }
        try:
            async with aiohttp.ClientSession() as session:
                for url in self._candidate_urls():
                    try:
                        async with session.post(
                            url,
                            json=payload,
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as resp:
                            if resp.status == 200:
                                if url != self._url:
                                    logger.info(f"Context POST switched endpoint: {self._url} -> {url}")
                                    self._url = url
                                logger.debug(f"Context sent: {ctx.pair}")
                                return True
                            text = await resp.text()
                            logger.warning(f"Context POST {resp.status} ({url}): {text[:200]}")
                    except Exception as e:
                        logger.debug(f"Context POST failed ({url}): {e}")
        except Exception as e:
            logger.debug(f"Context POST failed (session error): {e}")
        return False
