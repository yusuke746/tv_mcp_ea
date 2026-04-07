"""
アラート削除デバッグスクリプト。

使い方:
    cd tv_mcp_ea
    python debug_delete_alerts.py              # 全 [MCP-EA] アラートを一括削除
    python debug_delete_alerts.py --list-only  # 削除せずに一覧表示のみ
    python debug_delete_alerts.py --id <alert_id>  # 特定IDを削除してレスポンスを表示
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cdp_client import CDPClient


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--id", dest="alert_id", default=None)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9222)
    args = parser.parse_args()

    cdp = CDPClient(host=args.host, port=args.port)
    try:
        await cdp.connect()
        print(f"[CDP] Connected to TradingView at {args.host}:{args.port}")
    except Exception as e:
        print(f"[ERROR] CDP connect failed: {e}")
        print("TradingView が --remote-debugging-port=9222 で起動しているか確認してください。")
        return

    # インターセプター設置（URLパラメータキャプチャ用）
    await cdp._install_pricealerts_interceptors()

    # アラート一覧取得
    alerts = await cdp.list_alerts()
    mcp_alerts = [a for a in alerts if "[MCP-EA]" in a.get("message", "")]
    print(f"\n[LIST] 全アラート: {len(alerts)}件 / MCP-EA タグ: {len(mcp_alerts)}件")
    for a in alerts:
        tag = "★MCP" if "[MCP-EA]" in a.get("message", "") else "  --"
        print(f"  {tag}  id={a['alert_id']}  sym={a.get('symbol','?')[:20]}  "
              f"price={a.get('price',0):.5f}  active={a.get('active')}  "
              f"msg={a.get('message','')[:50]!r}")

    if args.list_only:
        await cdp.disconnect()
        return

    # 単一ID削除
    if args.alert_id:
        print(f"\n[DELETE] id={args.alert_id} のデバッグ削除を実行中...")
        dbg = await cdp.delete_alert_debug(args.alert_id)
        print(json.dumps(dbg, ensure_ascii=False, indent=2))
        await cdp.disconnect()
        return

    # 全MCP-EA一括削除
    if not mcp_alerts:
        print("\n[INFO] 削除対象の [MCP-EA] アラートはありません。")
        await cdp.disconnect()
        return

    print(f"\n[DELETE] {len(mcp_alerts)}件の [MCP-EA] アラートを一括削除します...")
    result = await cdp.delete_all_mcp_alerts()
    print(f"\n[RESULT] deleted={result['deleted']}  failed={result['failed']}")
    for d in result["details"]:
        status_str = "OK" if d["success"] else f"FAIL(HTTP {d.get('status')})"
        print(f"  {status_str}  id={d['alert_id']}  price={d.get('price')}  body={d.get('body','')[:80]}")

    await cdp.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
