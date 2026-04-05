"""
Pine Script テーブル特徴量リーダー

TradingView チャートに登録された "SMC Features" インジケーターの
table.new() 出力を CDP 経由で読み取り、LightGBM 互換の dict を返す。

読み取り失敗時は indicators.py (Python 近似) へフォールバックする。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cdp_client import CDPClient

logger = logging.getLogger(__name__)

# テーブルに出力される特徴量名 (30個)
# ── SMC フラグ (bool → int) ──
_BOOL_FEATURES = frozenset({
    "fvg_4h_zone_active", "ob_4h_zone_active",
    "liq_sweep_1h", "liq_sweep_qualified",
    "bos_1h", "choch_1h", "msb_15m_confirmed",
})

# ── int 変換する特徴量 ──
_INT_FEATURES = frozenset({
    "mtf_confluence", "trend_direction",
    "momentum_long", "momentum_short",
    "macd_signal_cross", "rsi_zone",
    "consecutive_same_dir", "sweep_pending_bars",
})

STUDY_FILTER = "SMC Features"


async def read_pine_features(cdp: "CDPClient") -> dict | None:
    """
    CDP 経由で "SMC Features" インジケーターのテーブルを読み取る。

    Returns:
        30 特徴量の dict (Pine 完全一致値) or None (テーブル未検出)
    """
    try:
        studies = await cdp.get_pine_table(study_filter=STUDY_FILTER)
    except Exception as e:
        logger.debug(f"Pine table read failed: {e}")
        return None

    if not studies:
        logger.debug("SMC Features indicator not found on chart")
        return None

    # 最初にマッチしたスタディのテーブルを使用
    study = studies[0]
    tables = study.get("tables", [])
    if not tables or not tables[0].get("rows"):
        logger.debug("SMC Features table has no rows")
        return None

    result: dict = {}
    for row_str in tables[0]["rows"]:
        # "feature_name | value" 形式
        parts = row_str.split(" | ", 1)
        if len(parts) != 2:
            continue
        key = parts[0].strip()
        val_str = parts[1].strip()
        result[key] = _parse_value(key, val_str)

    if len(result) < 20:
        logger.warning(
            f"SMC Features table only returned {len(result)} features "
            f"(expected ~30), falling back to Python indicators"
        )
        return None

    logger.info(f"Pine features read: {len(result)} features from '{study.get('name')}'")
    return result


def _parse_value(key: str, val_str: str):
    """特徴量名に応じて文字列を適切な Python 型に変換する。"""
    # booleans: Pine の str.tostring(bool) は "true" / "false"
    if key in _BOOL_FEATURES:
        return 1 if val_str.lower() == "true" else 0

    if key in _INT_FEATURES:
        try:
            return int(float(val_str))
        except (ValueError, TypeError):
            return 0

    # float
    try:
        return float(val_str)
    except (ValueError, TypeError):
        return 0.0
