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

    # ------------------------------------------------------------------ #
    #  アラート API (TradingView pricealerts REST)
    # ------------------------------------------------------------------ #

    async def list_alerts(self) -> list[dict]:
        """TradingView の pricealerts API から全アラートを取得する。"""
        result = await self.evaluate("""
            (async function() {
                try {
                    var r = await fetch(
                        'https://pricealerts.tradingview.com/list_alerts',
                        {credentials: 'include'}
                    );
                    var d = await r.json();
                    if (d.s !== 'ok' || !Array.isArray(d.r)) return [];
                    return d.r.map(function(a) {
                        var rawSym = a.symbol || '';
                        var sym = rawSym;
                        try { sym = JSON.parse(rawSym.replace(/^=/, '')).symbol || rawSym; }
                        catch(e) {}
                        return {
                            alert_id: a.alert_id,
                            symbol: sym,
                            raw_symbol: rawSym,
                            message: a.message || '',
                            price: a.price || 0,
                            active: a.active,
                            last_fired: a.last_fire_time || 0,
                            created: a.create_time || 0
                        };
                    });
                } catch(e) { return []; }
            })()
        """, timeout=15)
        return result or []

    async def delete_alert(self, alert_id) -> bool:
        """指定 alert_id のアラートを削除する。"""
        result = await self.evaluate(f"""
            (async function() {{
                try {{
                    var r = await fetch(
                        'https://pricealerts.tradingview.com/delete_alert',
                        {{
                            method: 'POST',
                            credentials: 'include',
                            headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                            body: 'alert_id={alert_id}'
                        }}
                    );
                    var d = await r.json();
                    return d.s === 'ok';
                }} catch(e) {{ return false; }}
            }})()
        """, timeout=10)
        return bool(result)

    async def create_price_alert_api(self, tv_symbol: str, price: float, message: str) -> bool:
        """
        REST API 経由で TV 価格アラートを作成する（UIより高速・確実）。
        メッセージが確実に設定されるため [MCP-EA] タグによる削除が機能する。
        """
        sym_json = json.dumps({"symbol": tv_symbol, "adjustment": "splits"})
        name = f"{tv_symbol.split(':')[-1]} MCP @ {price:.2f}"
        result = await self.evaluate(
            f"""
            (async function() {{
                try {{
                    var params = new URLSearchParams();
                    params.set('symbol', '=' + {json.dumps(sym_json)});
                    params.set('condition', JSON.stringify({{type:'crossing',value:{price}}}));
                    params.set('price', String({price}));
                    params.set('name', {json.dumps(name)});
                    params.set('message', {json.dumps(message)});
                    params.set('expiration_time', '0');
                    params.set('auto_deactivate', 'false');
                    var r = await fetch('https://pricealerts.tradingview.com/create_alert', {{
                        method: 'POST',
                        credentials: 'include',
                        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                        body: params.toString()
                    }});
                    var d = await r.json();
                    return (d.s === 'ok') ? (d.r || true) : false;
                }} catch(e) {{ return false; }}
            }})()
            """,
            timeout=10,
        )
        return bool(result)

    async def create_price_alert_ui(self, price: float, message: str) -> bool:
        """
        TV のアラート作成ダイアログを操作して価格アラートを作成する（フォールバック用）。
        """
        # ── Step 1: ダイアログを開く ──
        # ── Step 1: ダイアログを開く ──
        opened = await self.evaluate("""
            (function() {
                var btn = document.querySelector('[aria-label="Create Alert"]')
                    || document.querySelector('[data-name="alerts"]');
                if (btn) { btn.click(); return true; }
                return false;
            })()
        """)
        if not opened:
            # Alt+A ショートカット
            await self._send("Input.dispatchKeyEvent", {
                "type": "keyDown", "modifiers": 1,
                "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65,
            })
            await self._send("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "a", "code": "KeyA",
            })

        await asyncio.sleep(1.0)

        # ── Step 2: 価格を入力 ──
        await self.evaluate(f"""
            (function() {{
                var inputs = document.querySelectorAll(
                    '[class*="alert"] input[type="text"], '
                    + '[class*="alert"] input[type="number"]');
                for (var i = 0; i < inputs.length; i++) {{
                    var label = inputs[i].closest('[class*="row"]')
                        ?.querySelector('[class*="label"]');
                    if (label && /value|price/i.test(label.textContent)) {{
                        var ns = Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype, 'value').set;
                        ns.call(inputs[i], '{price}');
                        inputs[i].dispatchEvent(new Event('input', {{bubbles:true}}));
                        inputs[i].dispatchEvent(new Event('change', {{bubbles:true}}));
                        return true;
                    }}
                }}
                if (inputs.length > 0) {{
                    var ns = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'value').set;
                    ns.call(inputs[0], '{price}');
                    inputs[0].dispatchEvent(new Event('input', {{bubbles:true}}));
                }}
                return false;
            }})()
        """)

        # ── Step 3: メッセージを入力 ──
        msg_js = json.dumps(message)
        await self.evaluate(f"""
            (function() {{
                var ta = document.querySelector('[class*="alert"] textarea')
                    || document.querySelector('textarea[placeholder*="message"]');
                if (ta) {{
                    var ns = Object.getOwnPropertyDescriptor(
                        HTMLTextAreaElement.prototype, 'value').set;
                    ns.call(ta, {msg_js});
                    ta.dispatchEvent(new Event('input', {{bubbles:true}}));
                }}
            }})()
        """)

        await asyncio.sleep(0.5)

        # ── Step 4: Create ボタンをクリック ──
        created = await self.evaluate("""
            (function() {
                var btns = document.querySelectorAll(
                    'button[data-name="submit"], button');
                for (var i = 0; i < btns.length; i++) {
                    if (/^create$/i.test(btns[i].textContent.trim())) {
                        btns[i].click(); return true;
                    }
                }
                return false;
            })()
        """)

        await asyncio.sleep(0.5)
        return bool(created)

    # ------------------------------------------------------------------ #
    #  Pine Script グラフィックス読み取り
    # ------------------------------------------------------------------ #

    async def get_pine_table(self, study_filter: str = "") -> list[dict]:
        """
        チャート上の Pine indicator が table.new() で出力したセルデータを読み取る。

        MCP Server の data_get_pine_tables と同一の JS ロジックを使用。

        Args:
            study_filter: インジケーター名の部分一致フィルタ (例: "SMC Features")

        Returns:
            [{"name": "Study Name", "tables": [{"rows": ["col1 | col2", ...]}]}]
        """
        js = f"""
        (function() {{
          var chart = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget;
          var model = chart.model();
          var sources = model.model().dataSources();
          var results = [];
          var filter = '{study_filter}';
          for (var si = 0; si < sources.length; si++) {{
            var s = sources[si];
            if (!s.metaInfo) continue;
            try {{
              var meta = s.metaInfo();
              var name = meta.description || meta.shortDescription || '';
              if (!name) continue;
              if (filter && name.indexOf(filter) === -1) continue;
              var g = s._graphics;
              if (!g || !g._primitivesCollection) continue;
              var pc = g._primitivesCollection;
              var items = [];
              try {{
                var outer = pc.dwgtablecells;
                if (outer) {{
                  var inner = outer.get('tableCells');
                  if (inner) {{
                    var coll = inner.get(false);
                    if (coll && coll._primitivesDataById && coll._primitivesDataById.size > 0) {{
                      coll._primitivesDataById.forEach(function(v, id) {{ items.push({{id: id, raw: v}}); }});
                    }}
                  }}
                }}
              }} catch(e) {{}}
              if (items.length > 0) results.push({{name: name, count: items.length, items: items}});
            }} catch(e) {{}}
          }}
          return results;
        }})()
        """
        raw = await self.evaluate(js, timeout=15)
        if not raw:
            return []

        studies = []
        for s in raw:
            tables: dict[int, dict[int, dict[int, str]]] = {}
            for item in s.get("items", []):
                v = item.get("raw", {})
                tid = v.get("tid", 0)
                row = v.get("row", 0)
                col = v.get("col", 0)
                text = v.get("t", "")
                tables.setdefault(tid, {}).setdefault(row, {})[col] = text

            table_list = []
            for _tid, rows in sorted(tables.items()):
                formatted = []
                for rn in sorted(rows.keys()):
                    cols = rows[rn]
                    line = " | ".join(cols[cn] for cn in sorted(cols.keys()) if cols[cn])
                    if line:
                        formatted.append(line)
                table_list.append({"rows": formatted})
            studies.append({"name": s.get("name", ""), "tables": table_list})

        return studies
