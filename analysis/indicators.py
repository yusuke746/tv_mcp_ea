"""
テクニカル指標計算モジュール — Pine Script mtf_smc_v2_3 互換

MT5 から取得した OHLCV DataFrame を入力に、LightGBM 特徴量として必要な
テクニカル指標を Pine Script と **同一の計算式** で算出する。

■ Pine 互換性保証 (v2.3):
  - 平滑化: ATR / RSI は Wilder (RMA, alpha=1/period)、MACD / BB は標準 EMA/SMA
  - bb_width: (upper - lower) / mid * 100  (百分率)
  - close_vs_ema{20,50}_4h: (close - ema) / ema * 100  (EMA 乖離率 %)
  - trend_direction: 4H EMA20 vs EMA50 → 1 / -1 / 0
  - momentum_long/short: 0〜6 スコア (MACD=1 + RSI=1 + BOS=1 + ChoCH=2 + MSB=2)
  - momentum_3bar: (close - close[3]) / close[3] * 100  (百分率変化率)
  - rsi_zone: oversold=[30,35] → 1,  overbought=[65,70] → -1
  - atr_ratio: ATR(14) / SMA(ATR(14), 80)  on 15M (Pine: i_atr_avg*(60/15)=80)
  - stochastic: %K = raw (no smooth), %D = SMA(%K, 3)

■ 拡張: smc_context パラメータで BOS/ChoCH/MSB イベントを渡すと
  momentum_long/short に Pine 完全互換のスコアを加算できる。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_indicators(
    df_15m: pd.DataFrame,
    df_1h: pd.DataFrame | None,
    df_4h: pd.DataFrame | None,
    atr_14: float,
    *,
    smc_context: dict | None = None,
) -> dict:
    """
    LightGBM 38 特徴量のうち OHLCV から計算可能なものを Pine 互換の式で返す。

    Args:
        df_15m: 15 分足 DataFrame (columns: time, open, high, low, close, ...)
        df_1h:  1 時間足 DataFrame (同上、None 可)
        df_4h:  4 時間足 DataFrame (同上、None 可)
        atr_14: 15 分足の ATR(14) — 外部で既算のもの
        smc_context: SMC 構造イベント (将来拡張用)
            bos_1h_bull / bos_1h_bear  : bool — 1H BOS 方向
            choch_1h_bull / choch_1h_bear: bool — 1H ChoCH 方向
            msb_15m_bull / msb_15m_bear  : bool — 15M MSB 方向

    Returns:
        dict — build_features() の smc_data / market_data いずれにマージしても
               正しく参照されるフラットな辞書
    """
    smc = smc_context or {}
    result: dict = {}

    close = df_15m["close"]
    high = df_15m["high"]
    low = df_15m["low"]
    opn = df_15m["open"]

    # ── ATR Ratio: Pine = ATR(14) / SMA(ATR(14), 80) on 15M ─────────────
    #   Pine: atr_avg_20 = ta.sma(atr_14, i_atr_avg * (60/15))  → 80 bars
    if len(df_15m) >= 100:
        atr_series = _atr(df_15m, 14)
        atr_sma80 = float(atr_series.rolling(80).mean().iloc[-2])
        result["atr_ratio"] = round(atr_14 / atr_sma80, 4) if atr_sma80 > 0 else 1.0
    else:
        result["atr_ratio"] = 1.0

    # ── BB Width: Pine = (upper - lower) / mid * 100 ────────────────────
    if len(close) >= 22:
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        bb_upper = sma20 + 2.0 * std20
        bb_lower = sma20 - 2.0 * std20
        mid = float(sma20.iloc[-2])
        result["bb_width"] = round(
            float(bb_upper.iloc[-2] - bb_lower.iloc[-2]) / mid * 100, 4
        ) if mid > 0 else 0.0
    else:
        result["bb_width"] = 0.0

    # ── 15M High-Low Range: Pine = high - low ───────────────────────────
    if len(df_15m) >= 2:
        result["high_low_range_15m"] = round(
            float(high.iloc[-2] - low.iloc[-2]), 5
        )
    else:
        result["high_low_range_15m"] = 0.0

    # ── 4H EMA: 乖離率 + Trend Direction ────────────────────────────────
    #   Pine: close_vs_ema20_4h = (close - ema20_4h) / ema20_4h * 100
    #   Pine: trend_direction   = ema20_4h > ema50_4h ? 1 : -1
    if df_4h is not None and len(df_4h) >= 52:
        c4h = df_4h["close"]
        ema20_4h = float(c4h.ewm(span=20, adjust=False).mean().iloc[-2])
        ema50_4h = float(c4h.ewm(span=50, adjust=False).mean().iloc[-2])
        last_close = float(close.iloc[-2])  # 15M 確定足の終値
        result["close_vs_ema20_4h"] = round(
            (last_close - ema20_4h) / ema20_4h * 100, 4
        ) if ema20_4h > 0 else 0.0
        result["close_vs_ema50_4h"] = round(
            (last_close - ema50_4h) / ema50_4h * 100, 4
        ) if ema50_4h > 0 else 0.0
        result["trend_direction"] = (
            1 if ema20_4h > ema50_4h else (-1 if ema20_4h < ema50_4h else 0)
        )
    else:
        result["close_vs_ema20_4h"] = 0.0
        result["close_vs_ema50_4h"] = 0.0
        result["trend_direction"] = 0

    # ── MACD (12,26,9) on 15M — 標準 EMA ────────────────────────────────
    macd_signal_cross = 0
    if len(close) >= 35:
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        histogram = macd_line - signal_line
        result["macd_histogram"] = round(float(histogram.iloc[-2]), 6)
        # Pine: macd_cross_up = ta.crossover(macd_hist, 0)
        if len(close) >= 36:
            prev_h = float(histogram.iloc[-3])
            curr_h = float(histogram.iloc[-2])
            if prev_h <= 0 < curr_h:
                macd_signal_cross = 1
            elif prev_h >= 0 > curr_h:
                macd_signal_cross = -1
        result["macd_signal_cross"] = macd_signal_cross
    else:
        result["macd_histogram"] = 0.0
        result["macd_signal_cross"] = 0

    # ── RSI(14) on 15M — Wilder (RMA) ───────────────────────────────────
    #   Pine: rsi_oversold = rsi >= 30 and rsi <= 35  → zone=1
    #          rsi_overbought = rsi >= 65 and rsi <= 70 → zone=-1
    rsi_zone = 0
    if len(close) >= 16:
        rsi = _rsi(close, 14)
        rsi_val = float(rsi.iloc[-2])
        result["rsi_14"] = round(rsi_val, 2)
        if 30.0 <= rsi_val <= 35.0:
            rsi_zone = 1
        elif 65.0 <= rsi_val <= 70.0:
            rsi_zone = -1
        result["rsi_zone"] = rsi_zone
    else:
        result["rsi_14"] = 50.0
        result["rsi_zone"] = 0

    # ── Stochastic: Pine = raw %K (no smooth), %D = SMA(%K,3) ──────────
    if len(df_15m) >= 20:
        stoch_k, stoch_d = _stochastic(df_15m, 14, 3)
        result["stoch_k"] = round(float(stoch_k.iloc[-2]), 2)
        result["stoch_d"] = round(float(stoch_d.iloc[-2]), 2)
    else:
        result["stoch_k"] = 50.0
        result["stoch_d"] = 50.0

    # ── Momentum 3-bar: Pine = (close - close[3]) / close[3] * 100 ─────
    if len(close) >= 5:
        c_now = float(close.iloc[-2])      # confirmed bar
        c_3ago = float(close.iloc[-5])     # confirmed - 3
        result["momentum_3bar"] = round(
            (c_now - c_3ago) / c_3ago * 100, 4
        ) if c_3ago > 0 else 0.0
    else:
        result["momentum_3bar"] = 0.0

    # ── Momentum Long/Short: Pine スコア式 (0〜6) ──────────────────────
    #   Pine: (macd_cross_up?1:0) + (rsi_oversold?1:0)
    #        + (bos_1h_bull?1:0) + (choch_1h_bull?2:0) + (msb_15m_bull?2:0)
    ml = (1 if macd_signal_cross == 1 else 0) + (1 if rsi_zone == 1 else 0)
    ms = (1 if macd_signal_cross == -1 else 0) + (1 if rsi_zone == -1 else 0)
    # SMC コンテキスト (将来 MCP EA が構造イベントを送信可能になった場合)
    ml += int(smc.get("bos_1h_bull", 0))
    ml += 2 * int(smc.get("choch_1h_bull", 0))
    ml += 2 * int(smc.get("msb_15m_bull", 0))
    ms += int(smc.get("bos_1h_bear", 0))
    ms += 2 * int(smc.get("choch_1h_bear", 0))
    ms += 2 * int(smc.get("msb_15m_bear", 0))
    result["momentum_long"] = ml
    result["momentum_short"] = ms

    # ── Prior Candle Body Ratio: Pine = abs(close[1]-open[1])/(high[1]-low[1]) ──
    #   [1] = confirmed bar の 1 本前 → Python iloc[-3]
    if len(df_15m) >= 3:
        o = float(opn.iloc[-3])
        c = float(close.iloc[-3])
        h = float(high.iloc[-3])
        lo = float(low.iloc[-3])
        rng = h - lo
        result["prior_candle_body_ratio"] = round(abs(c - o) / rng, 4) if rng > 0 else 0.5
    else:
        result["prior_candle_body_ratio"] = 0.5

    # ── Consecutive Same Dir: Pine のステートフルカウンター互換 ─────────
    result["consecutive_same_dir"] = _consecutive_same_dir(opn, close)

    return result


# ---------------------------------------------------------------------------
# Pine 互換ヘルパー関数
# ---------------------------------------------------------------------------

def _rma(series: pd.Series, period: int) -> pd.Series:
    """Pine Script 互換 RMA (Wilder's Moving Average, alpha=1/period)."""
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Pine Script 互換 ATR — Wilder 平滑化。"""
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    return _rma(tr, period)


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Pine Script 互換 RSI — Wilder 平滑化。"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = _rma(gain, period)
    avg_loss = _rma(loss, period)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _stochastic(
    df: pd.DataFrame, k_period: int = 14, d_smooth: int = 3,
) -> tuple[pd.Series, pd.Series]:
    """Pine Script 互換 Stochastic — %K は raw (非平滑), %D = SMA(%K, d)."""
    ll = df["low"].rolling(k_period).min()
    hh = df["high"].rolling(k_period).max()
    denom = hh - ll
    raw_k = 100.0 * (df["close"] - ll) / denom.replace(0, np.nan)
    stoch_d = raw_k.rolling(d_smooth).mean()
    return raw_k, stoch_d


def _consecutive_same_dir(opn: pd.Series, close: pd.Series) -> int:
    """Pine 互換 consecutive_same_dir — doji (close==open) でリセット。"""
    n = min(len(close), 50)
    if n < 2:
        return 0
    count = 0
    last_dir = 0
    # iloc[-2] = confirmed bar, iterate backwards
    for i in range(2, n):
        c = float(close.iloc[-i])
        o = float(opn.iloc[-i])
        if c > o:
            cur_dir = 1
        elif c < o:
            cur_dir = -1
        else:
            break  # doji → Pine resets to 0
        if count == 0:
            last_dir = cur_dir
            count = 1
        elif cur_dir == last_dir:
            count += 1
        else:
            break
    return count
    low_min = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    raw_k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    stoch_k = raw_k.rolling(k_smooth).mean()
    stoch_d = stoch_k.rolling(d_smooth).mean()
    return stoch_k, stoch_d
