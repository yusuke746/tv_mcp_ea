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

    async def get_chart_count(self) -> int:
        """現在のレイアウト内のチャート数を返す。"""
        expr = """
        (function() {
            try {
                var api = window.TradingViewApi;
                if (!api || !api._chartWidgetCollection) return 0;
                var defs = api._chartWidgetCollection._chartWidgetsDefs;
                if (Array.isArray(defs)) return defs.length;
                return 1;
            } catch (e) {
                return 0;
            }
        })()
        """
        try:
            count = await self.evaluate(expr)
            return int(count or 0)
        except CDPError:
            return 0

    async def list_charts(self) -> list[dict]:
        """レイアウト内の全チャートの index/symbol を返す。"""
        expr = """
        (function() {
            try {
                var api = window.TradingViewApi;
                if (!api || !api._chartWidgetCollection) return [];
                var defs = api._chartWidgetCollection._chartWidgetsDefs;
                if (!Array.isArray(defs)) return [];
                var out = [];
                for (var i = 0; i < defs.length; i++) {
                    var chart = defs[i] ? defs[i].chartWidget : null;
                    var symbol = '';
                    try {
                        if (chart && chart._symbolWV && typeof chart._symbolWV.value === 'function') {
                            symbol = chart._symbolWV.value() || '';
                        }
                    } catch (e) {}
                    out.push({index: i, symbol: symbol});
                }
                return out;
            } catch (e) {
                return [];
            }
        })()
        """
        result = await self.evaluate(expr, timeout=8)
        return result or []

    async def find_chart_index_by_symbol(self, symbol: str) -> int | None:
        """指定シンボルを表示しているチャート index を返す。"""
        charts = await self.list_charts()
        short_symbol = symbol.split(":")[-1]
        for item in charts:
            sym = str(item.get("symbol") or "")
            if sym == symbol or sym == short_symbol or sym.endswith(short_symbol):
                return int(item.get("index", 0))
        return None

    async def set_active_chart(self, chart_index: int, settle_seconds: float = 0.5) -> bool:
        """マルチチャートレイアウトのアクティブペインを切り替える。"""
        expr = f"""
        (async function() {{
            try {{
                var idx = {int(chart_index)};
                var api = window.TradingViewApi;
                if (!api || !api._chartWidgetCollection) return false;
                var defs = api._chartWidgetCollection._chartWidgetsDefs;
                if (!Array.isArray(defs) || idx < 0 || idx >= defs.length) return false;
                var target = defs[idx] ? defs[idx].chartWidget : null;
                if (!target) return false;

                try {{
                    if (target._isActive && typeof target._isActive.value === 'function' && target._isActive.value()) {{
                        return true;
                    }}
                }} catch (e) {{}}

                if (typeof api._activateChart !== 'function') return false;
                var ret = api._activateChart(target);
                if (ret && typeof ret.then === 'function') await ret;

                try {{
                    return !!(target._isActive && typeof target._isActive.value === 'function' && target._isActive.value());
                }} catch (e) {{
                    return false;
                }}
            }} catch (e) {{
                return false;
            }}
        }})()
        """
        ok = bool(await self.evaluate(expr, timeout=max(5.0, settle_seconds + 3.0)))
        if ok and settle_seconds > 0:
            await asyncio.sleep(settle_seconds)
        return ok

    async def get_current_symbol(self) -> str | None:
        """現在チャートに表示されているシンボルを返す。"""
        expr = "window.TradingViewApi._activeChartWidgetWV.value().symbol()"
        try:
            val = await self.evaluate(expr)
            return str(val) if val else None
        except CDPError:
            return None

    async def set_current_symbol(self, symbol: str, settle_seconds: float = 1.5) -> bool:
        """現在チャートのシンボルを切り替える。"""
        symbol_json = json.dumps(symbol)
        expr = f"""
        (async function() {{
            try {{
                var widget = window.TradingViewApi._activeChartWidgetWV.value();
                if (!widget) return false;
                if (widget.symbol && widget.symbol() === {symbol_json}) return true;

                async function setVia(obj) {{
                    if (!obj || typeof obj.setSymbol !== 'function') return false;
                    return await new Promise(function(resolve) {{
                        var settled = false;
                        function done(ok) {{
                            if (settled) return;
                            settled = true;
                            resolve(ok);
                        }}
                        try {{
                            var ret = obj.setSymbol({symbol_json}, function() {{ done(true); }});
                            if (ret && typeof ret.then === 'function') {{
                                ret.then(function() {{ done(true); }}).catch(function() {{ done(false); }});
                            }}
                            setTimeout(function() {{
                                try {{
                                    done(widget.symbol && widget.symbol() === {symbol_json});
                                }} catch (e) {{
                                    done(false);
                                }}
                            }}, 1200);
                        }} catch (e) {{
                            done(false);
                        }}
                    }});
                }}

                if (await setVia(widget)) return true;
                if (widget.activeChart && await setVia(widget.activeChart())) return true;
                if (widget._chartWidget && widget._chartWidget.activeChart && await setVia(widget._chartWidget.activeChart())) return true;
                return widget.symbol && widget.symbol() === {symbol_json};
            }} catch (e) {{
                return false;
            }}
        }})()
        """
        ok = bool(await self.evaluate(expr, timeout=max(5.0, settle_seconds + 3.0)))
        if ok and settle_seconds > 0:
            await asyncio.sleep(settle_seconds)
        return ok

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

    async def _install_pricealerts_interceptors(self, webhook_url: str = "") -> bool:
        """
        pricealerts リクエストの URL パラメータをキャプチャし、
        create_alert リクエストの webhook URL を差し替えるインターセプターを設置する。
        既に設置済みの場合は webhook_url だけ更新する。
        """
        hook_js = json.dumps(webhook_url) if webhook_url else "null"
        result = await self.evaluate(f"""
            (function() {{
                // webhook URL を更新（既存インターセプターがあれば更新のみ）
                window.__tvMcpWebhookUrl = {hook_js};

                if (window.__tvMcpInterceptorInstalled) return 'updated';

                var origFetch = window.fetch;
                window.fetch = function(url, opts) {{
                    var urlStr = url ? url.toString() : '';
                    // pricealerts の URL パラメータをキャプチャ（list_alerts, create_alert 両方から）
                    if (urlStr.includes('pricealerts.tradingview.com')) {{
                        try {{
                            var qIdx = urlStr.indexOf('?');
                            if (qIdx >= 0) {{
                                window.__tvCreateAlertUrlParams = urlStr.substring(qIdx + 1);
                            }}
                        }} catch(e) {{}}
                    }}
                    // create_alert POST の price / webhook を差し替え
                    if (urlStr.includes('pricealerts') && urlStr.includes('create_alert') &&
                        opts && opts.body) {{
                        try {{
                            var body = JSON.parse(opts.body);
                            if (body.payload) {{
                                // price: React の内部状態（ダイアログのデフォルト値）を正しい値で上書き
                                if (window.__tvMcpAlertPrice != null) {{
                                    body.payload.price = window.__tvMcpAlertPrice;
                                }}
                                if (window.__tvMcpWebhookUrl) {{
                                    body.payload.web_hook = window.__tvMcpWebhookUrl;
                                }}
                                opts = Object.assign({{}}, opts, {{body: JSON.stringify(body)}});
                            }}
                        }} catch(e) {{}}
                    }}
                    return origFetch.call(this, url, opts);
                }};
                window.__tvMcpInterceptorInstalled = true;
                return 'installed';
            }})()
        """)
        return result in ("installed", "updated")

    async def list_alerts(self) -> list[dict]:
        """TradingView の pricealerts API から全アラートを取得する。"""
        # インターセプターを設置して URL パラメータをキャプチャする（初回のみ効果あり）
        await self._install_pricealerts_interceptors()
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
                            web_hook: a.web_hook || '',
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

    async def create_price_alert_api(
        self,
        tv_symbol: str,
        price: float,
        message: str,
        direction: str = "long",
        webhook_url: str = "",
    ) -> bool:
        """
        REST API 経由で TV 価格アラートを作成する（UIより高速・確実）。
        TradingView UI が送信するのと同じ {"payload": {...}} 形式 + URL パラメータを使用。

        Args:
            direction:   "long" (>価格) or "short" (<価格) — 方向付き条件
            webhook_url: 各アラートに個別設定する webhook 送信先 URL
        """
        # インターセプターを設置して URL パラメータをキャプチャする
        await self._install_pricealerts_interceptors(webhook_url)
        webhook_js = json.dumps(webhook_url) if webhook_url else "null"

        result = await self.evaluate(
            f"""
            (async function() {{
                try {{
                    // TV UI が使う URL パラメータを取得（ページ内でキャプチャ済みの情報を使用）
                    var baseUrl = 'https://pricealerts.tradingview.com/create_alert';
                    var urlParams = window.__tvCreateAlertUrlParams || null;
                    if (urlParams) {{
                        baseUrl += '?' + urlParams;
                    }}

                    // TV UI と同じ payload 形式
                    var symbol = {json.dumps(tv_symbol)};
                    // currency-id は OANDA等の場合 USD、デフォルト USD
                    var symParts = symbol.split(':');
                    var symObj = {{"currency-id":"USD","session":"regular","symbol": symbol}};

                    var payload = {{
                        symbol: '=' + JSON.stringify(symObj),
                        resolution: '1',
                        message: {json.dumps(message)},
                        sound_file: null,
                        sound_duration: 0,
                        popup: false,
                        expiration: null,
                        auto_deactivate: true,
                        email: false,
                        sms_over_email: false,
                        mobile_push: false,
                        web_hook: {webhook_js} !== null ? {webhook_js} : undefined,
                        name: null,
                        conditions: [{{
                            type: 'cross',
                            frequency: 'on_first_fire',
                            series: [{{type: 'barset'}}, {{type: 'value', value: {price}}}],
                            resolution: '1'
                        }}],
                        active: true,
                        ignore_warnings: true
                    }};
                    if (payload.web_hook === undefined) delete payload.web_hook;

                    var r = await fetch(baseUrl, {{
                        method: 'POST',
                        credentials: 'include',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{payload: payload}})
                    }});
                    var d = await r.json();
                    if (d.s !== 'ok' || !d.r) {{ return false; }}
                    return d.r;
                }} catch(e) {{ return false; }}
            }})()
            """,
            timeout=10,
        )
        return bool(result)

    async def create_price_alert_ui(self, price: float, message: str, webhook_url: str = "") -> bool:
        """
        TV のアラート作成ダイアログを操作して価格アラートを作成する（フォールバック用）。

        fetch インターセプターで webhook URL を差し替えるため、Notifications パネルへの
        ナビゲーションは不要。
        """
        # Step 1: fetch インターセプターを設置（webhook URL と price を差し替え）
        await self._install_pricealerts_interceptors(webhook_url)
        # ダイアログが開いたデフォルト価格（現在価格）を正しい価格で上書きするため
        # window.__tvMcpAlertPrice に設定しておく（インターセプターが create_alert 送信時に注入）
        await self.evaluate(f"window.__tvMcpAlertPrice = {price!r};")

        # Step 2: 既存ダイアログをすべて閉じる
        await self.evaluate("""
            (function() {
                document.querySelectorAll('button').forEach(function(b) {
                    var txt = b.textContent.trim();
                    if (txt === 'Cancel' || txt === 'Close menu') b.click();
                });
            })()
        """)
        await asyncio.sleep(0.3)

        # Step 3: ツールバーの "Alert" ボタンでダイアログを開く
        opened = await self.evaluate("""
            (function() {
                var btns = document.querySelectorAll('button');
                for (var i = 0; i < btns.length; i++) {
                    if (btns[i].textContent.trim() === 'Alert') {
                        btns[i].click();
                        return true;
                    }
                }
                return false;
            })()
        """)
        if not opened:
            return False

        await asyncio.sleep(2.0)

        # ダイアログが開いたか確認
        if not await self.evaluate("!!document.querySelector('[class*=\"dialog-YKU5b5xj\"]')"):
            return False

        # Step 4: 価格を設定
        await self.evaluate(f"""
            (function() {{
                var d = document.querySelector('[class*="dialog-YKU5b5xj"]');
                if (!d) return;
                var inp = d.querySelector('input.input-RUSovanF') ||
                          d.querySelector('input[class*="with-end-slot"]');
                if (!inp) return;
                var ns = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                ns.call(inp, '{price}');
                inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                inp.dispatchEvent(new Event('change', {{bubbles: true}}));
            }})()
        """)
        await asyncio.sleep(0.3)

        # Step 5: メッセージを設定
        # メッセージボタン（最初の button-KijOUKJc）をクリック → Edit message パネルへ
        if message:
            msg_js = json.dumps(message)
            await self.evaluate("""
                (function() {
                    var d = document.querySelector('[class*="dialog-YKU5b5xj"]');
                    if (!d) return;
                    var btns = d.querySelectorAll('[class*="button-KijOUKJc"]');
                    if (btns.length > 0) btns[0].click();
                })()
            """)
            await asyncio.sleep(1.0)

            # textarea にカスタムメッセージを設定
            await self.evaluate(f"""
                (function() {{
                    var d = document.querySelector('[class*="dialog-YKU5b5xj"]');
                    if (!d) return;
                    var ta = d.querySelector('textarea.textarea-x5KHDULU') ||
                             d.querySelector('textarea');
                    if (!ta) return;
                    var ns = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
                    ns.call(ta, {msg_js});
                    ta.dispatchEvent(new Event('input', {{bubbles: true}}));
                    ta.dispatchEvent(new Event('change', {{bubbles: true}}));
                }})()
            """)
            await asyncio.sleep(0.3)

            # "Apply" でメッセージを保存してメインダイアログに戻る
            await self.evaluate("""
                (function() {
                    var d = document.querySelector('[class*="dialog-YKU5b5xj"]');
                    if (!d) return;
                    for (var b of d.querySelectorAll('button')) {
                        if (b.textContent.trim() === 'Apply') { b.click(); return; }
                    }
                })()
            """)
            await asyncio.sleep(0.8)

        # Step 6: Create ボタンをクリック
        # fetch インターセプターが webhook URL を差し替えてから送信する
        created = await self.evaluate("""
            (function() {
                var d = document.querySelector('[class*="dialog-YKU5b5xj"]');
                if (!d) return false;
                var submit = d.querySelector('[class*="submitBtn"]');
                if (!submit) return false;
                var txt = submit.textContent.trim();
                if (txt !== 'Create') return false;
                submit.click();
                return true;
            })()
        """)

        await asyncio.sleep(0.5)
        # 使用済みの price をクリア（次のアラートに誤って引き継がないため）
        await self.evaluate("window.__tvMcpAlertPrice = null;")
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
