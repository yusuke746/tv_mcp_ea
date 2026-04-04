"""
S/R（Support / Resistance）レベル検出モジュール

スイング高値・安値を価格クラスタリングし、
複数回タッチされた水平ゾーンをS/Rレベルとして抽出する。
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .swing import SwingPoint


@dataclass
class SRLevel:
    price: float                    # クラスター中心価格
    touches: int                    # タッチ回数（強度の指標）
    kind: str                       # "resistance" | "support" | "both"
    last_touch_bar: int             # 最後にタッチしたバーインデックス
    swing_points: list[SwingPoint] = field(default_factory=list)


def find_sr_levels(
    swings: list[SwingPoint],
    pip_size: float = 0.01,
    cluster_pips: float = 5.0,
    min_touches: int = 2,
) -> list[SRLevel]:
    """
    スイングポイントをクラスタリングして S/R レベルを抽出する。

    Args:
        swings: SwingPoint のリスト
        pip_size: 1pip の価格単位（USDJPY=0.01, EURUSD=0.0001, XAUUSD=0.01）
        cluster_pips: 同一クラスターとみなす価格幅（pips）
        min_touches: SR として認定する最低タッチ数

    Returns:
        SRLevel のリスト（タッチ数降順）
    """
    if not swings:
        return []

    cluster_width = cluster_pips * pip_size
    prices = np.array([s.price for s in swings])
    used = [False] * len(swings)
    levels: list[SRLevel] = []

    for i, sp in enumerate(swings):
        if used[i]:
            continue
        cluster = [sp]
        used[i] = True

        for j, other in enumerate(swings):
            if not used[j] and abs(other.price - sp.price) <= cluster_width:
                cluster.append(other)
                used[j] = True

        if len(cluster) < min_touches:
            continue

        center_price = float(np.mean([c.price for c in cluster]))
        high_count = sum(1 for c in cluster if c.kind == "high")
        low_count = sum(1 for c in cluster if c.kind == "low")

        if high_count > low_count:
            kind = "resistance"
        elif low_count > high_count:
            kind = "support"
        else:
            kind = "both"

        last_touch_bar = max(c.bar_index for c in cluster)

        levels.append(SRLevel(
            price=center_price,
            touches=len(cluster),
            kind=kind,
            last_touch_bar=last_touch_bar,
            swing_points=cluster,
        ))

    levels.sort(key=lambda lv: lv.touches, reverse=True)
    return levels


def filter_sr_near_price(
    levels: list[SRLevel],
    current_price: float,
    atr: float,
    atr_multiplier: float = 3.0,
) -> list[SRLevel]:
    """現在価格から ATR * multiplier 以内のレベルだけを返す。"""
    threshold = atr * atr_multiplier
    return [lv for lv in levels if abs(lv.price - current_price) <= threshold]
