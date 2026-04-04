"""
CDP (Chrome DevTools Protocol) クライアント

TradingView デスクトップアプリへ直接 WebSocket 接続し、
チャートへの描画操作（水平線・トレンドライン・矩形）を実行する。

CDP の WS エンドポイント: ws://localhost:9222/devtools/page/{targetId}
"""

import asyncio
import json
import logging
from typing import Any

import aiohttp
import websockets

logger = logging.getLogger(__name__)


class CDPError(Exception):
    pass


class CDPClient:
    """TradingView チャートへの CDP 接続を管理するクライアント。"""

    def __init__(self, host: str = "localhost", port: int = 9222):
        self._host = host
        self._port = port
        self._ws = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._listener_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    #  接続管理
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        ws_url = await self._find_chart_target()
        self._ws = await websockets.connect(ws_url, max_size=10 * 1024 * 1024)
        self._listener_task = asyncio.create_task(self._listen())
        logger.info(f"CDP connected: {ws_url}")

    async def disconnect(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("CDP disconnected")

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.disconnect()

    # ------------------------------------------------------------------ #
    #  低レベル通信
    # ------------------------------------------------------------------ #

    async def _find_chart_target(self) -> str:
        """TradingView チャートの WebSocket デバッガ URL を返す。"""
        url = f"http://{self._host}:{self._port}/json/list"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                targets = await resp.json(content_type=None)

        for t in targets:
            title = t.get("title", "")
            if "tradingview.com" in t.get("url", "") and t.get("webSocketDebuggerUrl"):
                logger.debug(f"Found TV target: {title}")
                return t["webSocketDebuggerUrl"]

        raise CDPError("TradingView chart target not found on CDP port 9222.")

    async def _listen(self) -> None:
        """WebSocket メッセージを受信してペンディングの Future に配送する。"""
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                msg_id = msg.get("id")
                if msg_id and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(msg)
        except websockets.ConnectionClosed:
            logger.warning("CDP WebSocket connection closed")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"CDP listener error: {e}")

    async def _send(self, method: str, params: dict | None = None, timeout: float = 10.0) -> Any:
        """CDP コマンドを送信し、レスポンスを返す。"""
        self._msg_id += 1
        msg_id = self._msg_id
        msg = {"id": msg_id, "method": method, "params": params or {}}

        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._pending[msg_id] = fut

        await self._ws.send(json.dumps(msg))

        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise CDPError(f"CDP command timed out: {method}")

        if "error" in result:
            raise CDPError(f"CDP error: {result['error']}")
        return result.get("result")

    async def evaluate(self, expression: str, timeout: float = 10.0) -> Any:
        """Runtime.evaluate を実行し、戻り値を Python オブジェクトとして返す。"""
        result = await self._send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "timeout": int(timeout * 1000),
            },
            timeout=timeout + 2,
        )
        r = result.get("result", {})
        if r.get("type") == "undefined":
            return None
        if r.get("subtype") == "error":
            raise CDPError(f"JS error: {r.get('description')}")
        return r.get("value")

    # ------------------------------------------------------------------ #
    #  チャート API ヘルパー
    # ------------------------------------------------------------------ #

    async def _get_chart_api_path(self) -> str:
        return "window.TradingViewApi._activeChartWidgetWV.value()"

    async def get_current_symbol(self) -> str | None:
        """現在チャートに表示されているシンボルを返す。"""
        expr = "window.TradingViewApi._activeChartWidgetWV.value().symbol()"
        try:
            val = await self.evaluate(expr)
            return str(val) if val else None
        except CDPError:
            return None

    # ------------------------------------------------------------------ #
    #  描画操作
    # ------------------------------------------------------------------ #

    async def create_shape(
        self,
        shape: str,
        point: dict,
        point2: dict | None = None,
        overrides: dict | None = None,
        text: str = "",
    ) -> str | None:
        """
        チャートにシェイプを描画し、entity_id を返す。

        Args:
            shape: "horizontal_line" | "trend_line" | "rectangle" | "text"
            point: {"time": unix_ts, "price": float}
            point2: 2点シェイプの場合の第2点
            overrides: 色・線幅などのスタイル辞書
            text: テキストラベル

        Returns:
            entity_id (str) または None
        """
        api = await self._get_chart_api_path()
        overrides_json = json.dumps(overrides or {})
        text_json = json.dumps(text)

        # 描画前の shape ID 一覧取得
        before = await self.evaluate(
            f"{api}.getAllShapes().map(function(s){{return s.id;}})"
        )
        before = before or []

        if point2:
            await self.evaluate(
                f"{api}.createMultipointShape("
                f"[{{time:{point['time']},price:{point['price']}}},"
                f"{{time:{point2['time']},price:{point2['price']}}}],"
                f"{{shape:'{shape}',overrides:{overrides_json},text:{text_json}}})"
            )
        else:
            await self.evaluate(
                f"{api}.createShape("
                f"{{time:{point['time']},price:{point['price']}}},"
                f"{{shape:'{shape}',overrides:{overrides_json},text:{text_json}}})"
            )

        await asyncio.sleep(0.25)

        after = await self.evaluate(
            f"{api}.getAllShapes().map(function(s){{return s.id;}})"
        )
        after = after or []
        new_ids = [i for i in after if i not in before]
        return new_ids[0] if new_ids else None

    async def remove_shape(self, entity_id: str) -> bool:
        """指定した entity_id のシェイプを削除する。"""
        api = await self._get_chart_api_path()
        try:
            await self.evaluate(f"{api}.removeEntity('{entity_id}')")
            return True
        except CDPError as e:
            logger.warning(f"remove_shape failed for {entity_id}: {e}")
            return False

    async def get_all_shape_ids(self) -> list[str]:
        """チャート上の全シェイプの entity_id 一覧を返す。"""
        api = await self._get_chart_api_path()
        result = await self.evaluate(
            f"{api}.getAllShapes().map(function(s){{return s.id;}})"
        )
        return result or []
