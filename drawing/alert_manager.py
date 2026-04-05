"""
TradingView アラート管理モジュール。

5分スキャンで検出した S/R レベル・パターン境界に TV アラートを設定し、
価格がレベルに到達した時点で TradingView がリアルタイムにアラートを発火する。

古いアラートは毎スキャン先頭で削除してから新しいレベルを設定する。
アラートメッセージに fx_system が解釈可能な JSON ペイロードを埋め込む。

TV アラートの webhook 配信を有効にするには、TradingView の設定で
webhook URL (例: http://<公開IP>:8000/webhook/tv_alert) を設定すること。
"""

import json
import logging
from pathlib import Path

from cdp_client import CDPClient, CDPError
from detection.sr_levels import SRLevel
from detection.triangle import Triangle
from detection.channel import Channel

logger = logging.getLogger(__name__)

# 自動管理アラートの識別タグ
_TAG = "[MCP-EA]"

_STATE_FILE = Path(__file__).parent.parent / "state" / "alert_state.json"


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


class AlertManager:
    """
    TradingView アラートのライフサイクルを管理する。

    - update_alerts(): 旧アラートを削除 → 重要レベルに新アラートを設定
    - アラートメッセージは JSON 文字列で fx_system が解釈可能
    """

    def __init__(self, cdp: CDPClient, max_alerts_per_symbol: int = 4):
        self._cdp = cdp
        self._max_per_sym = max_alerts_per_symbol

    async def update_alerts(
        self,
        tv_symbol: str,
        mt5_symbol: str,
        sr_levels: list[SRLevel],
        triangles: list[Triangle],
        channels: list[Channel],
        current_price: float,
        atr: float,
    ) -> int:
        """
        指定シンボルの MCP-EA 管理アラートを更新する。

        1. 既存アラートから自タグ付きのものを削除
        2. 重要レベルに新アラートを作成
        3. 作成数を返す

        Returns:
            作成したアラート数
        """
        # ── 古いアラートの削除 ──────────────────────────────
        await self._delete_old_alerts(tv_symbol)

        # ── 重要レベルの選定 ─────────────────────────────────
        candidates = self._pick_levels(
            sr_levels, triangles, channels, current_price, atr
        )

        if not candidates:
            logger.debug(f"{mt5_symbol}: no alert levels to set")
            return 0

        # ── 新アラート作成 ──────────────────────────────────
        created_ids: list[str] = []
        for level in candidates[: self._max_per_sym]:
            msg = self._build_message(mt5_symbol, level, atr)
            try:
                ok = await self._cdp.create_price_alert_ui(
                    price=level["price"],
                    message=msg,
                )
                if ok:
                    created_ids.append(f"{mt5_symbol}_{level['price']:.5f}")
                    logger.info(
                        f"Alert set: {mt5_symbol} {level['direction']} "
                        f"@ {level['price']:.5f} ({level['pattern']})"
                    )
                else:
                    logger.warning(f"Alert creation may have failed: {mt5_symbol} @ {level['price']}")
            except CDPError as e:
                logger.warning(f"Alert creation failed: {e}")

        # ── 状態保存 ────────────────────────────────────────
        state = _load_state()
        state[tv_symbol] = {
            "alert_ids": created_ids,
            "levels": [
                {"price": c["price"], "direction": c["direction"], "pattern": c["pattern"]}
                for c in candidates[: self._max_per_sym]
            ],
        }
        _save_state(state)

        logger.info(f"{mt5_symbol}: {len(created_ids)} alerts set")
        return len(created_ids)

    # ──────────────────────────────────────────────────────────────────────────
    #  内部メソッド
    # ──────────────────────────────────────────────────────────────────────────

    async def _delete_old_alerts(self, tv_symbol: str) -> int:
        """
        TV アラート一覧から _TAG タグ付きかつ対象シンボルのアラートを削除する。

        REST API (pricealerts.tradingview.com) を使用して高速に削除する。
        """
        try:
            all_alerts = await self._cdp.list_alerts()
        except CDPError:
            logger.warning("Failed to list alerts — skipping cleanup")
            return 0

        deleted = 0
        for alert in all_alerts:
            msg = alert.get("message", "")
            sym = alert.get("symbol", "")
            if _TAG in msg and (tv_symbol in sym or tv_symbol.split(":")[-1] in sym):
                aid = alert.get("alert_id")
                if aid:
                    try:
                        ok = await self._cdp.delete_alert(aid)
                        if ok:
                            deleted += 1
                    except CDPError:
                        pass

        if deleted:
            logger.info(f"Deleted {deleted} old alerts for {tv_symbol}")

        # ローカル状態もクリア
        state = _load_state()
        if tv_symbol in state:
            state[tv_symbol] = {"alert_ids": [], "levels": []}
            _save_state(state)

        return deleted

    def _pick_levels(
        self,
        sr_levels: list[SRLevel],
        triangles: list[Triangle],
        channels: list[Channel],
        current_price: float,
        atr: float,
    ) -> list[dict]:
        """
        アラート設定候補レベルを重要度順に返す。

        - 現在価格に近い（ATR×2 以内）レベルを優先
        - S/R はタッチ回数でランク付け
        - パターン境界はレベルとして追加
        """
        candidates: list[dict] = []

        # ── S/R レベル ──
        for lvl in sr_levels:
            dist = abs(lvl.price - current_price)
            if dist > atr * 3:
                continue

            direction = "long" if lvl.price > current_price else "short"
            candidates.append({
                "price": lvl.price,
                "direction": direction,
                "pattern": f"sr_{lvl.kind}",
                "priority": lvl.touches * 10 + max(0, 30 - dist / atr * 10),
            })

        # ── トライアングル ──
        for tri in triangles:
            for price, direction, kind in [
                (tri.upper_price_now, "long", f"tri_{tri.kind}_upper"),
                (tri.lower_price_now, "short", f"tri_{tri.kind}_lower"),
            ]:
                dist = abs(price - current_price)
                if dist > atr * 3:
                    continue
                candidates.append({
                    "price": price,
                    "direction": direction,
                    "pattern": kind,
                    "priority": 20 + max(0, 30 - dist / atr * 10),
                })

        # ── チャネル ──
        for ch in channels:
            for price, direction, kind in [
                (ch.upper_price_now, "long", f"chan_{ch.kind}_upper"),
                (ch.lower_price_now, "short", f"chan_{ch.kind}_lower"),
            ]:
                dist = abs(price - current_price)
                if dist > atr * 3:
                    continue
                candidates.append({
                    "price": price,
                    "direction": direction,
                    "pattern": kind,
                    "priority": 15 + max(0, 30 - dist / atr * 10),
                })

        # 重要度順にソート
        candidates.sort(key=lambda c: c["priority"], reverse=True)
        return candidates

    def _build_message(self, mt5_symbol: str, level: dict, atr: float) -> str:
        """
        fx_system が解釈できる JSON を含むアラートメッセージを構築する。

        TradingView が webhook を発火する際、このメッセージが body として送信される。
        """
        payload = {
            "signal_source": "tv_alert",
            "pair": mt5_symbol,
            "direction": level["direction"],
            "pattern": level["pattern"],
            "pattern_level": level["price"],
            "atr": round(atr, 5),
        }
        # 可読テキスト + JSON ペイロード
        return f'{_TAG} {mt5_symbol} {level["direction"]} @ {level["price"]:.5f}\n{json.dumps(payload)}'
