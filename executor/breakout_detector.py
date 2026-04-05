"""
ブレイクアウト検出モジュール。

10点満点のスコアリングシステムで直近の 15M 確定足が S/R・
トライアングル・チャネルをブレイクしたかを判定し、
スコア閾値（デフォルト 7）以上なら fx_system へ webhook POST する。

採点基準:
  +2  クローズ確認     : 直近確定足終値がレベルを超えている
  +2  出来高急増       : 直近足の出来高が 20 本平均の 1.5 倍超
  +1  ボディ比率       : (|終値-始値| / (高値-安値)) > 0.60
  +2  HTF バイアス      : 1H EMA20 vs EMA50 がブレイク方向に一致
  +3  パターン品質     : AI スコア（または R² ベース fallback）
      ≥75 → +3、55-74 → +2、40-54 → +1、<40 → +0
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import aiohttp
import numpy as np
import pandas as pd

from analysis.ai_scorer import AIScorer, PatternScore
from data_feed import OHLCVData
from detection.sr_levels import SRLevel
from detection.triangle import Triangle
from detection.channel import Channel

logger = logging.getLogger(__name__)

_COOLDOWN_KEY_FMT = "{symbol}_{kind}_{price:.5f}"


@dataclass
class BreakoutSignal:
    symbol: str             # MT5 シンボル（例: USDJPY）
    tv_symbol: str          # TV シンボル（例: FX:USDJPY）
    direction: str          # "long" | "short"
    close: float            # 確定足終値
    atr: float
    level: float            # ブレイクしたレベル価格
    pattern: str            # "sr_resistance" | "sr_support" | "triangle_sym" | etc.
    breakout_score: int     # 10点中
    ai_quality_score: int   # 0-100
    ai_reason: str          # GPT 根拠テキスト


class BreakoutDetector:
    """
    検出済みパターンリストを巡回し、ブレイクアウトシグナルを生成する。
    """

    def __init__(
        self,
        ai_scorer: AIScorer,
        webhook_url: str,
        webhook_token: str,
        score_threshold: int = 7,
        cooldown_seconds: int = 900,
        volume_surge_ratio: float = 1.5,
        body_ratio_min: float = 0.60,
    ):
        self._scorer = ai_scorer
        self._webhook_url = webhook_url
        self._webhook_token = webhook_token
        self._threshold = score_threshold
        self._cooldown = cooldown_seconds
        self._vol_ratio = volume_surge_ratio
        self._body_min = body_ratio_min
        # クールダウン管理: {cooldown_key: last_trigger_timestamp}
        self._last_trigger: dict[str, float] = {}

    async def detect_and_fire(
        self,
        tv_symbol: str,
        mt5_symbol: str,
        ohlcv_15m: OHLCVData,
        ohlcv_1h: OHLCVData,
        sr_levels: list[SRLevel],
        triangles: list[Triangle],
        channels: list[Channel],
        indicators: dict | None = None,
    ) -> list[BreakoutSignal]:
        """
        全パターンをスキャンし、閾値超えシグナルを webhook POST して返す。
        """
        df = ohlcv_15m.bars
        if len(df) < 3:
            return []

        # 直近確定足（index -2 が最後の閉じた 15M 足）
        closed = df.iloc[-2]
        close_price = float(closed["close"])
        open_price  = float(closed["open"])
        high_price  = float(closed["high"])
        low_price   = float(closed["low"])
        volume      = float(closed.get("volume", 0))

        # 共有素点: 出来高・ボディ比率（全パターン共通）
        vol_points  = self._score_volume(df)
        body_points = self._score_body(close_price, open_price, high_price, low_price)
        htf_long, htf_short = self._htf_bias(ohlcv_1h)

        fired: list[BreakoutSignal] = []

        async def _try_fire(level: float, direction: str, pattern_key: str, pattern_data: dict):
            # +2 クローズ確認
            if direction == "long" and close_price <= level:
                return
            if direction == "short" and close_price >= level:
                return
            close_pts = 2

            # 小数切り捨てでクールダウンキーを生成
            cd_key = f"{mt5_symbol}_{pattern_key}_{direction}"
            if time.time() - self._last_trigger.get(cd_key, 0) < self._cooldown:
                logger.debug(f"Cooldown active: {cd_key}")
                return

            # HTF バイアス
            htf_pts = htf_long if direction == "long" else htf_short

            # AI スコア
            ps: PatternScore = await self._scorer.score_pattern(
                {**pattern_data, "symbol": mt5_symbol, "direction": direction,
                 "current_price": close_price, "atr": ohlcv_15m.atr14}
            )
            ai_pts = 3 if ps.score >= 75 else (2 if ps.score >= 55 else (1 if ps.score >= 40 else 0))

            total = close_pts + vol_points + body_points + htf_pts + ai_pts

            logger.info(
                f"{mt5_symbol} {direction} Pattern={pattern_key} "
                f"score={total}/10 (close=2 vol={vol_points} body={body_points} "
                f"htf={htf_pts} ai={ai_pts} ai_q={ps.score})"
            )

            if total < self._threshold:
                return

            sig = BreakoutSignal(
                symbol=mt5_symbol,
                tv_symbol=tv_symbol,
                direction=direction,
                close=close_price,
                atr=ohlcv_15m.atr14,
                level=level,
                pattern=pattern_key,
                breakout_score=total,
                ai_quality_score=ps.score,
                ai_reason=ps.reason,
            )
            self._last_trigger[cd_key] = time.time()
            await self._post_webhook(sig)
            fired.append(sig)

        # ─── S/R レベル ─────────────────────────────────────────────────────
        # インジケータ辞書を保持（_post_webhook で使う）
        self._current_indicators = indicators or {}
        for lvl in sr_levels:
            pd_data = {
                "pattern_type": "sr_level",
                "kind": lvl.kind,
                "key_level": lvl.price,
                "touches": lvl.touches,
            }
            if lvl.kind in ("resistance", "both"):
                await _try_fire(lvl.price, "long", f"sr_resistance_{lvl.price:.5f}", pd_data)
            if lvl.kind in ("support", "both"):
                await _try_fire(lvl.price, "short", f"sr_support_{lvl.price:.5f}", pd_data)

        # ─── トライアングル ──────────────────────────────────────────────────
        for tri in triangles:
            pd_data = {
                "pattern_type": "triangle",
                "kind": tri.kind,
                "r2_upper": tri.upper_line.r2,
                "r2_lower": tri.lower_line.r2,
                "apex_bars_ahead": max(0, tri.apex_bar - (len(df) - 2)),
            }
            await _try_fire(tri.upper_price_now, "long",  f"triangle_{tri.kind}_upper", pd_data)
            await _try_fire(tri.lower_price_now, "short", f"triangle_{tri.kind}_lower", pd_data)

        # ─── チャネル ────────────────────────────────────────────────────────
        for ch in channels:
            pd_data = {
                "pattern_type": "channel",
                "kind": ch.kind,
                "r2_upper": ch.upper_line.r2,
                "r2_lower": ch.lower_line.r2,
                "width_pips": ch.width_pips,
            }
            await _try_fire(ch.upper_price_now, "long",  f"channel_{ch.kind}_upper", pd_data)
            await _try_fire(ch.lower_price_now, "short", f"channel_{ch.kind}_lower", pd_data)

        return fired

    # ─── スコア素点ヘルパー ────────────────────────────────────────────────────

    def _score_volume(self, df: pd.DataFrame) -> int:
        """出来高が直近 20 本平均の _vol_ratio 倍超なら +2、そうでなければ 0。"""
        if "volume" not in df.columns or len(df) < 22:
            return 0
        avg = float(df["volume"].iloc[-22:-2].mean())
        last_vol = float(df.iloc[-2]["volume"])
        return 2 if (avg > 0 and last_vol > avg * self._vol_ratio) else 0

    def _score_body(self, close: float, open_: float, high: float, low: float) -> int:
        """ボディ比率 (|close-open| / (high-low)) > _body_min なら +1。"""
        candle_range = high - low
        if candle_range <= 0:
            return 0
        return 1 if abs(close - open_) / candle_range > self._body_min else 0

    def _htf_bias(self, ohlcv_1h: OHLCVData) -> tuple[int, int]:
        """
        1H チャートの EMA20/EMA50 関係から HTF バイアスを計算。
        戻り値: (long_points, short_points) — それぞれ 0 or 2
        """
        df = ohlcv_1h.bars if ohlcv_1h else None
        if df is None or len(df) < 52:
            return 0, 0
        ema20 = float(df["close"].ewm(span=20, adjust=False).mean().iloc[-2])
        ema50 = float(df["close"].ewm(span=50, adjust=False).mean().iloc[-2])
        if ema20 > ema50:
            return 2, 0   # 上昇バイアス: ロング有利
        elif ema20 < ema50:
            return 0, 2   # 下降バイアス: ショート有利
        return 0, 0

    # ─── Webhook 送信 ────────────────────────────────────────────────────────

    async def _post_webhook(self, sig: BreakoutSignal) -> None:
        payload = {
            "pair":            sig.symbol,
            "direction":       sig.direction,
            "atr":             sig.atr,
            "close":           sig.close,
            "breakout_score":  sig.breakout_score,
            "pattern":         sig.pattern,
            "pattern_level":   sig.level,
            "ai_quality_score": sig.ai_quality_score,
            "ai_reason":       sig.ai_reason,
            "signal_source":   "mcp",
            "webhook_token":   self._webhook_token,
        }
        # LightGBM 特徴量をペイロードに追加
        if hasattr(self, '_current_indicators') and self._current_indicators:
            payload["indicators"] = self._current_indicators
        logger.info(f"POST webhook: {sig.symbol} {sig.direction} score={sig.breakout_score}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.warning(f"Webhook returned {resp.status}: {text[:200]}")
                    else:
                        logger.info(f"Webhook accepted: {sig.symbol} {sig.direction}")
        except Exception as e:
            logger.error(f"Webhook POST failed: {e}")
