"""
トライアングルパターン検出モジュール

スイング高値の下降トレンドライン（上辺）と
スイング安値の上昇トレンドライン（下辺）の収束を検出する。
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .swing import SwingPoint


@dataclass
class TrendLine:
    """y = slope * x + intercept の形式（x はバーインデックス）"""
    slope: float
    intercept: float
    r2: float               # 決定係数（フィット精度）
    bar_start: int
    bar_end: int

    def price_at(self, bar_index: int) -> float:
        return self.slope * bar_index + self.intercept

    def price_at_future(self, bars_ahead: int, current_bar: int) -> float:
        return self.price_at(current_bar + bars_ahead)


@dataclass
class Triangle:
    upper_line: TrendLine       # 抵抗ライン（下降 or 水平）
    lower_line: TrendLine       # 支持ライン（上昇 or 水平）
    apex_bar: int               # 収束点（バーインデックス）
    kind: str                   # "symmetrical" | "ascending" | "descending"
    bar_end: int                # パターン右端バーインデックス
    upper_price_now: float      # 現在バーでの上辺価格
    lower_price_now: float      # 現在バーでの下辺価格


def _fit_line(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """最小二乗法で直線フィット。(slope, intercept, r2) を返す。"""
    if len(x) < 2:
        return 0.0, float(np.mean(y)), 0.0
    coeffs = np.polyfit(x, y, 1)
    slope, intercept = float(coeffs[0]), float(coeffs[1])
    y_pred = slope * x + intercept
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return slope, intercept, r2


def find_triangles(
    highs: list[SwingPoint],
    lows: list[SwingPoint],
    current_bar: int,
    min_touches: int = 2,
    max_bars: int = 100,
    min_r2: float = 0.5,
) -> list[Triangle]:
    """
    トライアングルパターンを検出する。

    Args:
        highs: スイング高値リスト
        lows: スイング安値リスト
        current_bar: 現在バーのインデックス
        min_touches: 各辺に必要な最低スイングポイント数
        max_bars: 探索範囲（バー数）
        min_r2: ラインフィットの最低 R²

    Returns:
        Triangle のリスト
    """
    cutoff = current_bar - max_bars
    recent_highs = [h for h in highs if h.bar_index >= cutoff]
    recent_lows = [lw for lw in lows if lw.bar_index >= cutoff]

    if len(recent_highs) < min_touches or len(recent_lows) < min_touches:
        return []

    # 上辺: スイング高値のトレンドライン
    hx = np.array([h.bar_index for h in recent_highs], dtype=float)
    hy = np.array([h.price for h in recent_highs], dtype=float)
    h_slope, h_intercept, h_r2 = _fit_line(hx, hy)

    # 下辺: スイング安値のトレンドライン
    lx = np.array([lw.bar_index for lw in recent_lows], dtype=float)
    ly = np.array([lw.price for lw in recent_lows], dtype=float)
    l_slope, l_intercept, l_r2 = _fit_line(lx, ly)

    if h_r2 < min_r2 or l_r2 < min_r2:
        return []

    # 収束条件: 上辺が下降または水平、下辺が上昇または水平、かつ収束している
    converging = h_slope < l_slope
    if not converging:
        return []

    # 収束点（apex）計算: slope1*x+int1 = slope2*x+int2
    if abs(h_slope - l_slope) < 1e-10:
        return []
    apex_bar = int((l_intercept - h_intercept) / (h_slope - l_slope))

    # apex が未来すぎる場合はスキップ
    if apex_bar < current_bar or apex_bar > current_bar + max_bars * 2:
        return []

    upper_price_now = h_slope * current_bar + h_intercept
    lower_price_now = l_slope * current_bar + l_intercept

    # 上辺が価格より上、下辺が価格より下であることを確認
    if upper_price_now <= lower_price_now:
        return []

    # パターン種別
    if h_slope < -1e-8 and l_slope > 1e-8:
        kind = "symmetrical"
    elif abs(h_slope) < 1e-8 and l_slope > 1e-8:
        kind = "ascending"
    elif h_slope < -1e-8 and abs(l_slope) < 1e-8:
        kind = "descending"
    else:
        kind = "symmetrical"

    upper_line = TrendLine(
        slope=h_slope, intercept=h_intercept, r2=h_r2,
        bar_start=int(hx[0]), bar_end=int(hx[-1]),
    )
    lower_line = TrendLine(
        slope=l_slope, intercept=l_intercept, r2=l_r2,
        bar_start=int(lx[0]), bar_end=int(lx[-1]),
    )

    return [Triangle(
        upper_line=upper_line,
        lower_line=lower_line,
        apex_bar=apex_bar,
        kind=kind,
        bar_end=current_bar,
        upper_price_now=upper_price_now,
        lower_price_now=lower_price_now,
    )]
