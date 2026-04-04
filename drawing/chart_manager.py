"""
TradingView チャート描画管理モジュール。

CDP 経由で水平線（S/R）、トレンドライン（トライアングル・チャネル）を
描画・削除する。エンティティ ID は state/drawing_state.json で管理する。

TV は「現在表示中のシンボル」にのみ描画できるため、
チェックしてから描画を行う点に注意。
"""

import json
import logging
from pathlib import Path

import pandas as pd

from cdp_client import CDPClient, CDPError
from detection.sr_levels import SRLevel
from detection.triangle import Triangle
from detection.channel import Channel

logger = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).parent.parent / "state" / "drawing_state.json"

# ─── カラーパレット ───────────────────────────────────────────────────────────
_COLOR_RESISTANCE = "#FF5252"
_COLOR_SUPPORT    = "#4CAF50"
_COLOR_BOTH       = "#FF9800"
_COLOR_TRI_UPPER  = "#FF7043"
_COLOR_TRI_LOWER  = "#66BB6A"
_COLOR_CHAN_UPPER  = "#EF5350"
_COLOR_CHAN_LOWER  = "#43A047"


def _load_state() -> dict:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _bar_to_time(df: pd.DataFrame, bar_index: int) -> int:
    """bar_index（0-based、古い順）から Unix タイムスタンプを求める。"""
    n = len(df)
    if 0 <= bar_index < n:
        return int(df.iloc[int(bar_index)]["time"])
    # 範囲外（未来バー）は最終バーから等間隔で推定
    last_idx = n - 1
    last_time = int(df.iloc[-1]["time"])
    bar_dur = int(df.iloc[-1]["time"] - df.iloc[-2]["time"]) if n >= 2 else 900
    return int(last_time + (bar_index - last_idx) * bar_dur)


class ChartManager:
    """現在表示中のシンボルのパターンを TV チャートに描画・更新する。"""

    def __init__(self, cdp: CDPClient):
        self._cdp = cdp

    # ─── 公開 API ────────────────────────────────────────────────────────────

    async def update_drawings(
        self,
        target_tv_symbol: str,
        df: pd.DataFrame,
        sr_levels: list[SRLevel],
        triangles: list[Triangle],
        channels: list[Channel],
    ) -> None:
        """
        現在 TV に表示されているシンボルが target_tv_symbol と一致する場合のみ
        新しいパターンで描画を更新する。
        """
        current = await self._cdp.get_current_symbol()
        if current != target_tv_symbol:
            logger.debug(f"TV shows {current}, skip drawing for {target_tv_symbol}")
            return

        logger.info(f"Updating drawings for {target_tv_symbol}")

        # 旧描画を削除
        await self._clear_symbol(target_tv_symbol)

        new_ids: list[str] = []

        # S/R レベル
        now_price = float(df.iloc[-2]["close"]) if len(df) >= 2 else 0.0
        ts_now = _bar_to_time(df, len(df) - 2)
        for lvl in sr_levels:
            eid = await self._draw_sr(lvl, ts_now)
            if eid:
                new_ids.append(eid)

        # トライアングル
        for tri in triangles:
            eids = await self._draw_triangle(tri, df)
            new_ids.extend(e for e in eids if e)

        # チャネル
        for ch in channels:
            eids = await self._draw_channel(ch, df)
            new_ids.extend(e for e in eids if e)

        # 状態保存
        state = _load_state()
        state[target_tv_symbol] = {"entity_ids": new_ids}
        _save_state(state)

        logger.info(f"Drew {len(new_ids)} shapes for {target_tv_symbol}")

    async def clear_all(self, symbol: str) -> None:
        """指定シンボルの全描画を削除する（緊急クリア用）。"""
        await self._clear_symbol(symbol)

    # ─── 内部メソッド ─────────────────────────────────────────────────────────

    async def _clear_symbol(self, symbol: str) -> None:
        """state.json に保存された entity_id を TV から削除する。"""
        state = _load_state()
        ids = state.get(symbol, {}).get("entity_ids", [])
        for eid in ids:
            try:
                await self._cdp.remove_shape(eid)
            except CDPError:
                pass  # 既に削除済み等は無視
        if symbol in state:
            state[symbol] = {"entity_ids": []}
            _save_state(state)

    async def _draw_sr(self, lvl: SRLevel, ts: int) -> str | None:
        """S/R 水平線を描画して entity_id を返す。"""
        color = {
            "resistance": _COLOR_RESISTANCE,
            "support":    _COLOR_SUPPORT,
        }.get(lvl.kind, _COLOR_BOTH)

        label = f"{lvl.kind.upper()} {lvl.price:.5f} ({lvl.touches}t)"
        overrides = {"linecolor": color, "linewidth": 2, "linestyle": 0}
        try:
            return await self._cdp.create_shape(
                shape="horizontal_line",
                point={"time": ts, "price": lvl.price},
                overrides=overrides,
                text=label,
            )
        except CDPError as e:
            logger.warning(f"draw_sr failed: {e}")
            return None

    async def _draw_triangle(self, tri: Triangle, df: pd.DataFrame) -> list[str | None]:
        """トライアングルの上辺と下辺を描画して entity_id リストを返す。"""
        t_start_u = _bar_to_time(df, tri.upper_line.bar_start)
        t_end     = _bar_to_time(df, tri.bar_end)
        t_start_l = _bar_to_time(df, tri.lower_line.bar_start)

        upper_p1 = {"time": t_start_u, "price": tri.upper_line.price_at(tri.upper_line.bar_start)}
        upper_p2 = {"time": t_end,     "price": tri.upper_price_now}
        lower_p1 = {"time": t_start_l, "price": tri.lower_line.price_at(tri.lower_line.bar_start)}
        lower_p2 = {"time": t_end,     "price": tri.lower_price_now}

        ov_upper = {"linecolor": _COLOR_TRI_UPPER, "linewidth": 2, "linestyle": 2}
        ov_lower = {"linecolor": _COLOR_TRI_LOWER, "linewidth": 2, "linestyle": 2}
        label = f"TRI {tri.kind.upper()[:3]}"

        results = []
        for p1, p2, ov in [(upper_p1, upper_p2, ov_upper), (lower_p1, lower_p2, ov_lower)]:
            try:
                eid = await self._cdp.create_shape(
                    shape="trend_line", point=p1, point2=p2, overrides=ov, text=label
                )
                results.append(eid)
            except CDPError as e:
                logger.warning(f"draw_triangle line failed: {e}")
                results.append(None)

        return results

    async def _draw_channel(self, ch: Channel, df: pd.DataFrame) -> list[str | None]:
        """チャネルの上辺と下辺を描画して entity_id リストを返す。"""
        t_start_u = _bar_to_time(df, ch.upper_line.bar_start)
        t_end     = _bar_to_time(df, ch.bar_end)
        t_start_l = _bar_to_time(df, ch.lower_line.bar_start)

        upper_p1 = {"time": t_start_u, "price": ch.upper_line.price_at(ch.upper_line.bar_start)}
        upper_p2 = {"time": t_end,     "price": ch.upper_price_now}
        lower_p1 = {"time": t_start_l, "price": ch.lower_line.price_at(ch.lower_line.bar_start)}
        lower_p2 = {"time": t_end,     "price": ch.lower_price_now}

        ov_upper = {"linecolor": _COLOR_CHAN_UPPER, "linewidth": 2, "linestyle": 0}
        ov_lower = {"linecolor": _COLOR_CHAN_LOWER, "linewidth": 2, "linestyle": 0}
        label = f"CH {ch.kind.upper()[:3]} {ch.width_pips:.0f}p"

        results = []
        for p1, p2, ov in [(upper_p1, upper_p2, ov_upper), (lower_p1, lower_p2, ov_lower)]:
            try:
                eid = await self._cdp.create_shape(
                    shape="trend_line", point=p1, point2=p2, overrides=ov, text=label
                )
                results.append(eid)
            except CDPError as e:
                logger.warning(f"draw_channel line failed: {e}")
                results.append(None)

        return results
