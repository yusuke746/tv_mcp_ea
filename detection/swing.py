"""
スイング高値・安値検出モジュール

scipy.signal.argrelextrema を使い、
指定された order（前後Nバー）内で局所的な高値・安値を検出する。
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.signal import argrelextrema


@dataclass
class SwingPoint:
    bar_index: int          # DataFrameの行インデックス
    time: pd.Timestamp
    price: float
    kind: str               # "high" | "low"


def find_swings(df: pd.DataFrame, order: int = 5) -> list[SwingPoint]:
    """
    OHLCV DataFrame からスイング高値・安値を検出する。

    Args:
        df: columns [time, open, high, low, close, volume]
        order: 前後 order 本のバーと比較して極値を判定する感度

    Returns:
        SwingPoint のリスト（時系列順）
    """
    highs = df["high"].values
    lows = df["low"].values

    high_idx = argrelextrema(highs, np.greater_equal, order=order)[0]
    low_idx = argrelextrema(lows, np.less_equal, order=order)[0]

    points: list[SwingPoint] = []

    for i in high_idx:
        points.append(SwingPoint(
            bar_index=int(i),
            time=df["time"].iloc[i],
            price=float(highs[i]),
            kind="high",
        ))

    for i in low_idx:
        points.append(SwingPoint(
            bar_index=int(i),
            time=df["time"].iloc[i],
            price=float(lows[i]),
            kind="low",
        ))

    points.sort(key=lambda p: p.bar_index)
    return points


def find_swings_by_kind(df: pd.DataFrame, kind: str, order: int = 5) -> list[SwingPoint]:
    """高値または安値だけを返す便利関数。"""
    return [p for p in find_swings(df, order) if p.kind == kind]
