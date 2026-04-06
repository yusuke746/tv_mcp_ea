"""
TradingView アラート管理モジュール。

5分スキャンで検出した S/R レベル・パターン境界に TV アラートを設定し、
価格がレベルに到達した時点で TradingView がリアルタイムにアラートを発火する。

古いアラートは毎スキャン先頭で削除してから新しいレベルを設定する。
アラートメッセージに fx_system が解釈可能な JSON ペイロードを埋め込む。

■ ローカル webhook 転送について
  TradingView のア ラート webhook はTradingView のサーバーから送信されるため、
  localhost や 192.168.x.x 等のプライベートアドレスは到達できない。
  本モジュールは pricealerts API を定期ポーリングし、[MCP-EA] アラートが
  発火（active=False）したタイミングで fx_system へ直接 POST する。
"""

import json
import logging
from pathlib import Path

import aiohttp

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

    def __init__(
        self,
        cdp: CDPClient,
        max_alerts_per_symbol: int = 4,
        tv_alert_webhook_url: str = "",
        local_tv_alert_url: str = "",
    ):
        self._cdp = cdp
        self._max_per_sym = max_alerts_per_symbol
        self._tv_alert_webhook_url = tv_alert_webhook_url or ""
        # ローカル転送先 URL (localhost 等、TV サーバーから到達できない場合に使用)
        self._local_tv_alert_url = local_tv_alert_url or tv_alert_webhook_url or ""
        # 転送済みアラートID（再起動するまで保持、重複転送防止）
        self._forwarded_alert_ids: set = set()

    def update_webhook_url(self, url: str) -> None:
        """webhook URL を動的に更新する（ngrok URL が変わった場合に呼び出す）。"""
        self._tv_alert_webhook_url = url
        self._local_tv_alert_url = url

    async def update_alerts(
        self,
        tv_symbol: str,
        mt5_symbol: str,
        sr_levels: list[SRLevel],
        triangles: list[Triangle],
        channels: list[Channel],
        current_price: float,
        atr: float,
        open_positions: list[dict] | None = None,
    ) -> int:
        """
        指定シンボルの MCP-EA 管理アラートを更新する。

        ポジションが存在する場合、構造レベルを自動的にエントリー/エグジットに分類し、
        エグジット用アラートには signal_source="exit_alert" を設定する。
        これにより fx_system がリアルタイムで構造レベル到達を検知して
        ポジション評価（クローズ判定）を行える。

        Returns:
            作成したアラート数
        """
        # ── 古いアラートの削除 ──────────────────────────────
        await self._delete_old_alerts(tv_symbol)

        # ── 重要レベルの選定 ─────────────────────────────────
        candidates = self._pick_levels(
            sr_levels, triangles, channels, current_price, atr,
            open_positions=open_positions,
        )

        if not candidates:
            logger.debug(f"{mt5_symbol}: no alert levels to set")
            return 0

        # ── 既存アラートの価格を収集（重複スキップ用） ─────────
        existing_prices: set[float] = set()
        try:
            existing = await self._cdp.list_alerts()
            sym_short = tv_symbol.split(":")[-1]
            for a in existing:
                sym_a = a.get("symbol", "")
                if tv_symbol in sym_a or sym_short in sym_a:
                    p = a.get("price", 0)
                    if p:
                        existing_prices.add(round(float(p), 2))
        except CDPError:
            pass

        # ── 新アラート作成 ──────────────────────────────────
        created_ids: list[str] = []
        for level in candidates[: self._max_per_sym]:
            price_rounded = round(level["price"], 2)
            if price_rounded in existing_prices:
                logger.debug(f"Alert already exists: {mt5_symbol} @ {level['price']:.5f} — skip")
                continue
            msg = self._build_message(mt5_symbol, level, atr)
            try:
                # まず REST API を試す
                api_ok = await self._cdp.create_price_alert_api(
                    tv_symbol=tv_symbol,
                    price=level["price"],
                    message=msg,
                    direction=level["direction"],  # "long" (tp) または "short" (sl)
                    webhook_url=self._tv_alert_webhook_url,
                )
                verified = await self._has_remote_alert(tv_symbol, level["price"], msg)

                if not verified:
                    # REST API で作成できなかった場合は UI 操作にフォールバックする
                    if api_ok:
                        logger.warning(
                            f"REST create not visible in list_alerts: {mt5_symbol} @ {level['price']:.5f} "
                            f"— trying UI fallback"
                        )
                    else:
                        logger.debug(
                            f"REST API failed: {mt5_symbol} @ {level['price']:.5f} "
                            f"— trying UI fallback"
                        )
                    ui_ok = await self._cdp.create_price_alert_ui(
                        price=level["price"],
                        message=msg,
                        webhook_url=self._tv_alert_webhook_url,
                    )
                    verified = ui_ok or await self._has_remote_alert(tv_symbol, level["price"], msg)
                else:
                    ui_ok = False

                if verified or ui_ok:
                    created_ids.append(f"{mt5_symbol}_{level['price']:.5f}")
                    existing_prices.add(price_rounded)
                    logger.info(
                        f"Alert set: {mt5_symbol} {level['direction']} "
                        f"@ {level['price']:.5f} ({level['pattern']})"
                    )
                else:
                    logger.warning(
                        f"Alert creation failed: {mt5_symbol} @ {level['price']:.5f} "
                        f"api_ok={api_ok!r}"
                    )
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

        remote_count: int | None = None
        try:
            remote_alerts = await self._cdp.list_alerts()
            sym_short = tv_symbol.split(":")[-1]
            remote_count = sum(
                1
                for a in remote_alerts
                if (tv_symbol in a.get("symbol", "") or sym_short in a.get("symbol", ""))
                and _TAG in a.get("message", "")
            )
        except CDPError:
            pass

        if remote_count is None:
            logger.info(f"{mt5_symbol}: {len(created_ids)} alerts set")
        else:
            logger.info(
                f"{mt5_symbol}: {len(created_ids)} alerts set, remote_pricealerts={remote_count}"
            )
            if created_ids and remote_count == 0:
                logger.warning(
                    f"{mt5_symbol}: alert create reported success but list_alerts returned 0 entries"
                )
        return len(created_ids)

    # ──────────────────────────────────────────────────────────────────────────
    #  内部メソッド
    # ──────────────────────────────────────────────────────────────────────────

    async def _delete_old_alerts(self, tv_symbol: str) -> int:
        """
        TV アラート一覧から対象シンボルの MCP-EA 管理アラートを削除する。
        - [MCP-EA] タグ付きのアラート
        - メッセージなし（UI作成失敗の残骸）
        - active=False（Stopped/Triggered 状態で再利用不可）
        - tv_alert_webhook_url と異なる web_hook を持つアラート（stale URL 対策）

        REST API (pricealerts.tradingview.com) を使用して高速に削除する。
        """
        try:
            all_alerts = await self._cdp.list_alerts()
        except CDPError:
            logger.warning("Failed to list alerts — skipping cleanup")
            return 0

        deleted = 0
        sym_short = tv_symbol.split(":")[-1]
        for alert in all_alerts:
            msg = alert.get("message", "")
            sym = alert.get("symbol", "")
            active = alert.get("active", True)
            alert_webhook = alert.get("web_hook", "")
            is_our_symbol = tv_symbol in sym or sym_short in sym

            # webhook URL が違う（ngrok 等）アラートは削除
            wrong_webhook = (
                bool(self._tv_alert_webhook_url)
                and bool(alert_webhook)
                and alert_webhook != self._tv_alert_webhook_url
            )

            is_triggered = not active  # False / None → triggered (Stopped)

            # 削除条件:
            #   1. [MCP-EA] タグ付き & 発火済み（全シンボル対象 — 他シンボル残骸も一掃）
            #   2. 現シンボルの [MCP-EA] アラート（active 問わず毎スキャン更新）
            #   3. 現シンボルのメッセージなしアラート（UI 作成失敗の残骸）
            #   4. [MCP-EA] タグ付き & webhook URL 違い（ngrok URL 残骸等）
            should_delete = (
                (_TAG in msg and is_triggered)      # 全シンボルの triggered MCP-EA
                or (is_our_symbol and _TAG in msg)  # 現シンボルの全 MCP-EA
                or (is_our_symbol and not msg)      # 現シンボルの空メッセージ
                or (_TAG in msg and wrong_webhook)  # webhook URL 違いの MCP-EA
            )

            if should_delete:
                aid = alert.get("alert_id")
                if aid:
                    try:
                        ok = await self._cdp.delete_alert(aid)
                        if ok:
                            deleted += 1
                        else:
                            logger.debug(f"delete_alert returned False for {aid}")
                    except CDPError as e:
                        logger.debug(f"delete_alert failed for {aid}: {e}")

        if deleted:
            logger.info(
                f"Deleted {deleted} old alerts for {tv_symbol} (tagged/message-less/stopped)"
            )
        else:
            logger.debug(f"No old alerts to delete for {tv_symbol}")

        # ローカル状態もクリア
        state = _load_state()
        if tv_symbol in state:
            state[tv_symbol] = {"alert_ids": [], "levels": []}
            _save_state(state)

        return deleted

    async def forward_triggered_alerts(self) -> int:
        """
        発火済み（active=False）の [MCP-EA] アラートを検出し、
        ローカル webhook URL（localhost 等）に転送する。

        TradingView のクラウドサーバーは localhost へ到達できないため、
        tv_mcp_ea が代わりにポーリングして直接 POST する。

        Returns:
            転送したアラート数
        """
        if not self._local_tv_alert_url:
            return 0

        try:
            all_alerts = await self._cdp.list_alerts()
        except CDPError:
            return 0

        forwarded = 0
        for alert in all_alerts:
            msg = alert.get("message", "")
            active = alert.get("active", True)
            alert_id = alert.get("alert_id")

            # [MCP-EA] タグ付き & 発火済みのみ対象
            if not (_TAG in msg and not active):
                continue

            # 既に転送済みの場合はスキップ
            if alert_id in self._forwarded_alert_ids:
                continue

            # メッセージからJSONペイロードを抽出
            payload = self._extract_json_payload(msg)
            if not payload:
                logger.debug(f"No JSON payload found in alert {alert_id}, skipping forward")
                self._forwarded_alert_ids.add(alert_id)  # 再試行しない
                continue

            try:
                async with aiohttp.ClientSession() as session:
                    resp = await session.post(
                        self._local_tv_alert_url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=5),
                    )
                    if resp.status < 400:
                        logger.info(
                            f"Forwarded triggered alert {alert_id} "
                            f"({payload.get('pair', '?')} {payload.get('direction', '?')}) "
                            f"→ {self._local_tv_alert_url} [{resp.status}]"
                        )
                        self._forwarded_alert_ids.add(alert_id)
                        forwarded += 1
                        # 転送済みアラートを削除（Stopped-Triggered の残骸を一掃）
                        try:
                            await self._cdp.delete_alert(alert_id)
                        except CDPError:
                            pass
                    else:
                        body = await resp.text()
                        logger.warning(
                            f"Forward alert {alert_id} returned {resp.status}: {body[:200]}"
                        )
            except Exception as e:
                logger.warning(f"Failed to forward alert {alert_id}: {e}")

        return forwarded

    @staticmethod
    def _extract_json_payload(message: str) -> dict | None:
        """アラートメッセージの本文から JSON ペイロードを抽出する。"""
        for line in message.split("\n"):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    pass
        return None

    async def _has_remote_alert(self, tv_symbol: str, price: float, message: str) -> bool:
        """pricealerts 一覧に対象アラートが実在するか確認する。"""
        try:
            alerts = await self._cdp.list_alerts()
        except CDPError:
            return False

        sym_short = tv_symbol.split(":")[-1]
        price_rounded = round(price, 2)
        for alert in alerts:
            symbol = alert.get("symbol", "")
            alert_price = round(float(alert.get("price", 0) or 0), 2)
            alert_message = alert.get("message", "")
            if not (tv_symbol in symbol or sym_short in symbol):
                continue
            if alert_price != price_rounded:
                continue
            if alert_message != message:
                continue
            return True
        return False

    def _pick_levels(
        self,
        sr_levels: list[SRLevel],
        triangles: list[Triangle],
        channels: list[Channel],
        current_price: float,
        atr: float,
        open_positions: list[dict] | None = None,
    ) -> list[dict]:
        """
        アラート設定候補レベルを重要度順に返す。

        ポジションが存在する場合:
          - ロング保有 → 上方 resistance = exit_tp, 下方 support = exit_sl
          - ショート保有 → 下方 support = exit_tp, 上方 resistance = exit_sl
          エグジット関連レベルは高優先度で action="exit" をマーク。

        ポジションがない場合:
          - 従来通りエントリー候補として action="entry" をマーク。
        """
        candidates: list[dict] = []

        # ── ポジション情報の抽出 ──
        pos_direction: str | None = None
        pos_ticket: int = 0
        pos_open_price: float = 0.0
        if open_positions:
            pos = open_positions[0]
            pos_direction = "long" if int(pos.get("type", 0)) == 0 else "short"
            pos_ticket = int(pos.get("ticket", 0))
            pos_open_price = float(pos.get("price_open", 0.0))

        # ── S/R レベル ──
        for lvl in sr_levels:
            dist = abs(lvl.price - current_price)
            if dist > atr * 3:
                continue

            is_above = lvl.price > current_price
            direction = "long" if is_above else "short"

            # エグジット分類
            action = "entry"
            exit_type = ""
            if pos_direction:
                if pos_direction == "long":
                    if is_above and lvl.kind in ("resistance", "both"):
                        action = "exit"
                        exit_type = "tp"
                    elif not is_above and lvl.kind in ("support", "both"):
                        action = "exit"
                        exit_type = "sl"
                else:  # short
                    if not is_above and lvl.kind in ("support", "both"):
                        action = "exit"
                        exit_type = "tp"
                    elif is_above and lvl.kind in ("resistance", "both"):
                        action = "exit"
                        exit_type = "sl"

            # エグジットレベルは高優先度（ポジション保護）
            base_priority = lvl.touches * 10 + max(0, 30 - dist / atr * 10)
            if action == "exit":
                base_priority += 60  # エグジットを優先

            candidates.append({
                "price": lvl.price,
                "direction": direction,
                "pattern": f"sr_{lvl.kind}",
                "priority": base_priority,
                "action": action,
                "exit_type": exit_type,
                "ticket": pos_ticket if action == "exit" else 0,
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

                is_above = price > current_price
                action = "entry"
                exit_type = ""
                if pos_direction:
                    if pos_direction == "long" and is_above:
                        action = "exit"
                        exit_type = "tp"
                    elif pos_direction == "long" and not is_above:
                        action = "exit"
                        exit_type = "sl"
                    elif pos_direction == "short" and not is_above:
                        action = "exit"
                        exit_type = "tp"
                    elif pos_direction == "short" and is_above:
                        action = "exit"
                        exit_type = "sl"

                base_priority = 20 + max(0, 30 - dist / atr * 10)
                if action == "exit":
                    base_priority += 60

                candidates.append({
                    "price": price,
                    "direction": direction,
                    "pattern": kind,
                    "priority": base_priority,
                    "action": action,
                    "exit_type": exit_type,
                    "ticket": pos_ticket if action == "exit" else 0,
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

                is_above = price > current_price
                action = "entry"
                exit_type = ""
                if pos_direction:
                    if pos_direction == "long" and is_above:
                        action = "exit"
                        exit_type = "tp"
                    elif pos_direction == "long" and not is_above:
                        action = "exit"
                        exit_type = "sl"
                    elif pos_direction == "short" and not is_above:
                        action = "exit"
                        exit_type = "tp"
                    elif pos_direction == "short" and is_above:
                        action = "exit"
                        exit_type = "sl"

                base_priority = 15 + max(0, 30 - dist / atr * 10)
                if action == "exit":
                    base_priority += 60

                candidates.append({
                    "price": price,
                    "direction": direction,
                    "pattern": kind,
                    "priority": base_priority,
                    "action": action,
                    "exit_type": exit_type,
                    "ticket": pos_ticket if action == "exit" else 0,
                })

        # 重要度順にソート
        candidates.sort(key=lambda c: c["priority"], reverse=True)
        return candidates

    def _build_message(self, mt5_symbol: str, level: dict, atr: float) -> str:
        """
        fx_system が解釈できる JSON を含むアラートメッセージを構築する。

        action="exit" の場合は signal_source="exit_alert" として送信し、
        fx_system が即座にポジション評価（クローズ判定）を行えるようにする。
        """
        action = level.get("action", "entry")

        if action == "exit":
            payload = {
                "signal_source": "exit_alert",
                "pair": mt5_symbol,
                "direction": level["direction"],
                "pattern": level["pattern"],
                "pattern_level": level["price"],
                "atr": round(atr, 5),
                "exit_type": level.get("exit_type", ""),
                "ticket": level.get("ticket", 0),
            }
            tag_label = f"EXIT {level.get('exit_type', '').upper()}"
        else:
            payload = {
                "signal_source": "tv_alert",
                "pair": mt5_symbol,
                "direction": level["direction"],
                "pattern": level["pattern"],
                "pattern_level": level["price"],
                "atr": round(atr, 5),
            }
            tag_label = level["direction"]

        # 可読テキスト + JSON ペイロード
        return f'{_TAG} {mt5_symbol} {tag_label} @ {level["price"]:.5f}\n{json.dumps(payload)}'
