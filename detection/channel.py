"""
平行チャネル検出モジュール

スイング高値・安値が同一スロープで並行する帯域を検出する。
"""

from dataclasses import dataclass

import numpy as np

from .swing import SwingPoint
from .triangle import TrendLine, _fit_line


@dataclass
class Channel:
    upper_line: TrendLine       # 抵抗ライン
    lower_line: TrendLine       # 支持ライン
    slope: float                # 共通スロープ
    kind: str                   # "ascending" | "descending" | "horizontal"
    bar_end: int
    upper_price_now: float
    lower_price_now: float
    width_pips: float           # チャネル幅（pip換算）


def find_channels(
    highs: list[SwingPoint],
    lows: list[SwingPoint],
    current_bar: int,
    pip_size: float,
    min_touches: int = 2,
    max_bars: int = 80,
    min_r2: float = 0.75,
    slope_tolerance: float = 0.15,
) -> list[Channel]:
    """
    平行チャネルパターンを検出する。

    両辺のスロープが一致（±tolerance 内）し、
    R² が min_r2 以上の場合にチャネルと判定する。

    Args:
        highs: スイング高値リスト
        lows: スイング安値リスト
        current_bar: 現在バーのインデックス
        pip_size: 1pip の価格変動幅
        min_touches: 各辺の最低スイングポイント数
        max_bars: 探索範囲（バー数）
        min_r2: ラインフィットの最低 R²
        slope_tolerance: 両辺スロープの許容差（スロープの相対比率）

    Returns:
        Channel のリスト
    """
    cutoff = current_bar - max_bars
    recent_highs = [h for h in highs if h.bar_index >= cutoff]
    recent_lows = [lw for lw in lows if lw.bar_index >= cutoff]

    if len(recent_highs) < min_touches or len(recent_lows) < min_touches:
        return []

    # 上辺フィット
    hx = np.array([h.bar_index for h in recent_highs], dtype=float)
    hy = np.array([h.price for h in recent_highs], dtype=float)
    h_slope, h_intercept, h_r2 = _fit_line(hx, hy)

    # 下辺フィット
    lx = np.array([lw.bar_index for lw in recent_lows], dtype=float)
    ly = np.array([lw.price for lw in recent_lows], dtype=float)
    l_slope, l_intercept, l_r2 = _fit_line(lx, ly)

    if h_r2 < min_r2 or l_r2 < min_r2:
        return []

    # 平行条件: スロープの差が許容範囲内
    avg_slope = (abs(h_slope) + abs(l_slope)) / 2.0
    if avg_slope > 1e-10:
        slope_diff_ratio = abs(h_slope - l_slope) / avg_slope
        if slope_diff_ratio > slope_tolerance:
            return []
    else:
        # どちらも水平に近い場合は差が pip_size の 5 倍以内
        if abs(h_slope - l_slope) > pip_size * 5:
            return []

    # 上辺が下辺より上であること
    upper_now = h_slope * current_bar + h_intercept
    lower_now = l_slope * current_bar + l_intercept
    if upper_now <= lower_now:
        return []

    # チャネル幅
    width_pips = (upper_now - lower_now) / pip_size

    # 最小幅チェック（ノイズ除去）
    if width_pips < 5.0:
        return []

    avg_slope_val = (h_slope + l_slope) / 2.0
    if abs(avg_slope_val) < pip_size * 0.01:
        kind = "horizontal"
    elif avg_slope_val > 0:
        kind = "ascending"
    else:
        kind = "descending"

    upper_line = TrendLine(
        slope=h_slope, intercept=h_intercept, r2=h_r2,
        bar_start=int(hx[0]), bar_end=int(hx[-1]),
    )
    lower_line = TrendLine(
        slope=l_slope, intercept=l_intercept, r2=l_r2,
        bar_start=int(lx[0]), bar_end=int(lx[-1]),
    )

    return [Channel(
        upper_line=upper_line,
        lower_line=lower_line,
        slope=avg_slope_val,
        kind=kind,
        bar_end=current_bar,
        upper_price_now=upper_now,
        lower_price_now=lower_now,
        width_pips=width_pips,
    )]
