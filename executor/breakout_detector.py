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

Sweep State Machine:
  S/R レベルに対して、まず「スウィープ（髭タッチ→終値戻り）」を検出して
  pending 状態に遷移し、その後の MSB（終値ブレイク）確認で発火する。
  これにより「罠にかかった逆張り勢の踏み上げ」のタイミングを捉える。
  タイムアウト（デフォルト: sweep_timeout_bars 本の 15M = 4H）で pending をリセット。
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

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
class _SweepPending:
    """スウィープ検出後の MSB 待機状態。"""
    level: float
    direction: str          # 期待するブレイク方向 ("long" | "short")
    detected_at: float      # time.time() でのタイムスタンプ
    timeout_seconds: float  # この秒数を超えたら期限切れ
    pattern_key: str
    pattern_data: dict

    @property
    def is_expired(self) -> bool:
        return time.time() - self.detected_at > self.timeout_seconds


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

    Sweep State Machine (S/R レベルのみ):
      1. スウィープ検出: 直近足の髭が S/R を超えるが終値が戻った場合 → pending へ
      2. MSB 確認: pending 中に終値ブレイクを確認 → 通常の採点 + 発火
      3. タイムアウト: sweep_timeout_bars 分待っても MSB が来なければ期限切れ
    トライアングル・チャネルは従来通りの即時終値ブレイク方式。
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
        sweep_timeout_bars: int = 16,
        sweep_wick_ratio: float = 0.3,
    ):
        self._scorer = ai_scorer
        self._webhook_url = webhook_url
        self._webhook_token = webhook_token
        self._threshold = score_threshold
        self._cooldown = cooldown_seconds
        self._vol_ratio = volume_surge_ratio
        self._body_min = body_ratio_min
        # スウィープ検出設定
        # sweep_timeout_bars: これ × 15M = 待機時間（デフォルト16本=4H）
        self._sweep_timeout_sec = sweep_timeout_bars * 15 * 60
        # sweep_wick_ratio: 髭がATRの何倍以上あればスウィープとみなすか
        self._sweep_wick_ratio = sweep_wick_ratio
        # クールダウン管理: {cooldown_key: last_trigger_timestamp}
        self._last_trigger: dict[str, float] = {}
        # Sweep pending 状態: {symbol: {pattern_key: _SweepPending}}
        self._sweep_pending: dict[str, dict[str, _SweepPending]] = {}

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

        # ── Sweep State Machine ヘルパー ──────────────────────────────────
        sym_pending = self._sweep_pending.setdefault(mt5_symbol, {})

        # 期限切れ pending を削除
        expired = [k for k, p in sym_pending.items() if p.is_expired]
        for k in expired:
            logger.info(f"[Sweep] Timeout: {mt5_symbol} {sym_pending[k].direction} level={sym_pending[k].level:.5f}")
            del sym_pending[k]

        def _is_sweep(level: float, direction: str) -> bool:
            """
            直近確定足が S/R を髭で超えて終値が戻った「スウィープ」を判定する。

            long (上抜きスウィープ): high > level かつ close < level
              → レジスタンス上にある踏み上げ狩りが発生し、下から押し戻された
            short (下抜きスウィープ): low < level かつ close > level
              → サポート下にある逆張り狩りが発生し、上から押し戻された

            さらに、髭の長さが ATR × sweep_wick_ratio 以上必要（ノイズ排除）。
            """
            atr = ohlcv_15m.atr14
            if direction == "long":
                # 髭が level を超えているか
                if high_price <= level:
                    return False
                # 終値がレベルを越えていない（クローズ確認なし = 戻った）
                if close_price >= level:
                    return False
                # 上髭の長さチェック
                upper_wick = high_price - max(close_price, open_price)
                return upper_wick >= atr * self._sweep_wick_ratio
            else:
                if low_price >= level:
                    return False
                if close_price <= level:
                    return False
                lower_wick = min(close_price, open_price) - low_price
                return lower_wick >= atr * self._sweep_wick_ratio

        async def _try_fire_sr(level: float, direction: str, pattern_key: str, pattern_data: dict):
            """S/R レベル専用: Sweep State Machine を経由した発火判定。"""
            cd_key = f"{mt5_symbol}_{pattern_key}_{direction}"

            # ── クールダウン確認 ────────────────────────────────────────
            if time.time() - self._last_trigger.get(cd_key, 0) < self._cooldown:
                logger.debug(f"Cooldown active: {cd_key}")
                return

            close_exceeds = (
                (direction == "long" and close_price > level)
                or (direction == "short" and close_price < level)
            )

            # ── ケース A: Pending 中に MSB 確認 → 発火 ─────────────────
            if cd_key in sym_pending:
                pending = sym_pending[cd_key]
                if close_exceeds:
                    logger.info(
                        f"[Sweep] MSB confirmed: {mt5_symbol} {direction} level={level:.5f} "
                        f"(sweep→MSB elapsed={time.time()-pending.detected_at:.0f}s)"
                    )
                    del sym_pending[cd_key]
                    # 採点して発火（通常と同じ）
                    close_pts = 2
                    htf_pts = htf_long if direction == "long" else htf_short
                    ps: PatternScore = await self._scorer.score_pattern(
                        {**pattern_data, "symbol": mt5_symbol, "direction": direction,
                         "current_price": close_price, "atr": ohlcv_15m.atr14,
                         "sweep_confirmed": True}
                    )
                    ai_pts = 3 if ps.score >= 75 else (2 if ps.score >= 55 else (1 if ps.score >= 40 else 0))
                    # Sweep 確認ボーナス: +1（罠からの逆転シグナルは品質が高い）
                    total = close_pts + vol_points + body_points + htf_pts + ai_pts + 1
                    logger.info(
                        f"{mt5_symbol} {direction} [SWEEP→MSB] Pattern={pattern_key} "
                        f"score={total}/11 (close=2 vol={vol_points} body={body_points} "
                        f"htf={htf_pts} ai={ai_pts} sweep_bonus=1 ai_q={ps.score})"
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
                        pattern=pattern_key + "_sweep_msb",
                        breakout_score=total,
                        ai_quality_score=ps.score,
                        ai_reason=ps.reason,
                    )
                    self._last_trigger[cd_key] = time.time()
                    await self._post_webhook(sig)
                    fired.append(sig)
                # Pending 中だがまだ MSB 待ち → スキップ
                return

            # ── ケース B: スウィープ検出 → Pending 遷移 ─────────────────
            if _is_sweep(level, direction):
                sym_pending[cd_key] = _SweepPending(
                    level=level,
                    direction=direction,
                    detected_at=time.time(),
                    timeout_seconds=self._sweep_timeout_sec,
                    pattern_key=pattern_key,
                    pattern_data=pattern_data,
                )
                logger.info(
                    f"[Sweep] Detected: {mt5_symbol} {direction} level={level:.5f} "
                    f"(timeout={self._sweep_timeout_sec/3600:.1f}h)"
                )
                return

            # ── ケース C: スウィープなしの通常終値ブレイク → 即時採点 ────
            if not close_exceeds:
                return

            htf_pts = htf_long if direction == "long" else htf_short
            ps = await self._scorer.score_pattern(
                {**pattern_data, "symbol": mt5_symbol, "direction": direction,
                 "current_price": close_price, "atr": ohlcv_15m.atr14}
            )
            ai_pts = 3 if ps.score >= 75 else (2 if ps.score >= 55 else (1 if ps.score >= 40 else 0))
            total = 2 + vol_points + body_points + htf_pts + ai_pts

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

        async def _try_fire(level: float, direction: str, pattern_key: str, pattern_data: dict):
            """トライアングル・チャネル用: 従来通りの即時終値ブレイク方式。"""
            # +2 クローズ確認
            if direction == "long" and close_price <= level:
                return
            if direction == "short" and close_price >= level:
                return

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

            total = 2 + vol_points + body_points + htf_pts + ai_pts

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
                await _try_fire_sr(lvl.price, "long", f"sr_resistance_{lvl.price:.5f}", pd_data)
            if lvl.kind in ("support", "both"):
                await _try_fire_sr(lvl.price, "short", f"sr_support_{lvl.price:.5f}", pd_data)

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
