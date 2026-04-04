# TV MCP EA

TradingView デスクトップをリアルタイムに監視し、チャートパターンのブレイクアウトを自動検出して MT5 に発注する自律トレーディングエージェント。

---

## アーキテクチャ

```
TradingView Desktop (CDP port 9222)
        │
        │ WebSocket (描画更新)
        ▼
   cdp_client.py ──── drawing/chart_manager.py
                              │ entity_id 保存
                        state/drawing_state.json

MT5 Terminal
        │ copy_rates_from_pos
        ▼
   data_feed.py
        │ OHLCV + ATR14
        ▼
   detection/
   ├── swing.py         スイング高値/安値 (scipy argrelextrema)
   ├── sr_levels.py     S/Rレベル (価格クラスタリング)
   ├── triangle.py      トライアングル (収束トレンドライン, 線形回帰)
   └── channel.py       平行チャネル (同一スロープ ±15%)

        │ パターンリスト
        ▼
   analysis/ai_scorer.py   GPT-5-mini で品質スコア 0-100

        │ スコア + パターン情報
        ▼
   executor/breakout_detector.py
        │ 10点スコアリング → 閾値 ≥7 で Fire
        │ HTTP POST
        ▼
   fx_system /webhook/mcp   (FastAPI port 8000)
        │
        ▼
   fx_system/_process_mcp_signal → MT5 発注
```

---

## スキャンサイクル（5分毎）

1. MT5 から 15M・1H OHLCV を取得
2. スイング検出 → S/R・トライアングル・チャネル抽出
3. TV チャートのシンボルが一致する場合、旧描画削除 → 新描画
4. 直近確定 15M 足でブレイクアウト判定
5. スコア ≥ 7 → `fx_system /webhook/mcp` へ POST

---

## ブレイクアウト スコアリング（10点満点）

| 項目 | 点数 | 判定条件 |
|------|-----:|---|
| クローズ確認 | +2 | 直近確定足終値がレベルを超えている |
| 出来高急増 | +2 | 直近足 > 20本平均 × 1.5 |
| ローソク実体比率 | +1 | `|終値-始値| / (高値-安値) > 0.6` |
| HTF バイアス | +2 | 1H EMA20 > EMA50（ロング）or < EMA50（ショート） |
| AI 品質スコア | +3 | GPT-5-mini スコア ≥75→+3 / ≥55→+2 / ≥40→+1 |
| **発注閾値** | **≥7** | `config.yaml` の `score_threshold` で変更可 |

---

## 対応ペアと設定

| ペア | カテゴリ | MT5 シンボル | pip_size | TP ATR倍率 | SL ATR倍率 |
|------|----------|-------------|---------|-----------|-----------|
| USDJPY | FX | USDJPY | 0.01 | 2.0× | 1.5× |
| EURUSD | FX | EURUSD | 0.0001 | 2.0× | 1.5× |
| GBPJPY | FX | GBPJPY | 0.01 | 2.0× | 1.5× |
| XAUUSD | GOLD | XAUUSD | 0.10 | 2.5× | 2.5× |

- FX / GOLD 各最大 1 ポジション（合計 2 枠）= 既存 fx_system の Pine 枠（最大 5）とは独立
- GOLD のロットは FX に対して 50%（`gold_lot_scale: 0.5`）

---

## ディレクトリ構造

```
tv_mcp_ea/
├── main.py                    # エントリーポイント・APScheduler
├── config.yaml                # 全設定
├── requirements.txt
├── cdp_client.py              # CDP WebSocket クライアント
├── data_feed.py               # MT5 OHLCV 取得・ATR 計算
├── analysis/
│   └── ai_scorer.py           # GPT-5-mini パターン採点
├── detection/
│   ├── swing.py               # スイング高値/安値
│   ├── sr_levels.py           # S/R レベル
│   ├── triangle.py            # トライアングル
│   └── channel.py             # 平行チャネル
├── drawing/
│   └── chart_manager.py       # TV 描画管理
├── executor/
│   └── breakout_detector.py   # スコアリング・webhook POST
└── state/
    └── drawing_state.json     # 描画エンティティ ID（自動生成）
```

---

## セットアップ

### 1. 前提条件

- TradingView Desktop が `--remote-debugging-port=9222` で起動済み
- MT5 (XMTrading KIWA 極口座) が起動済み
- `fx_system` が `http://localhost:8000` で稼働中
- Python 3.11+

### 2. 仮想環境 & 依存インストール

```bash
cd tv_mcp_ea
python -m venv .venv
.\.venv\Scripts\activate   # Windows
pip install -r requirements.txt
```

### 3. .env ファイル

`tv_mcp_ea/.env`（または `fx_system/.env` と共用）に以下を設定：

```env
MT5_LOGIN=<ログインID>
MT5_PASSWORD=<パスワード>
MT5_SERVER=XMTrading-Demo3    # 本番: XMTrading-Real15 等
OPENAI_API_KEY=sk-...
WEBHOOK_TOKEN=<fx_system の webhook_secret と同じ値>
```

### 4. TradingView Desktop 起動

```powershell
$exe = "C:\Program Files\WindowsApps\TradingView.Desktop_3.0.0.7652_x64__n534cwy3pjxzj\TradingView.exe"
Start-Process $exe '--remote-debugging-port=9222'
```

### 5. 起動

```bash
python main.py
```

---

## fx_system への変更点

| ファイル | 変更内容 |
|---|---|
| `webhook/server.py` | `/webhook/mcp` エンドポイント追加（XAUUSD 対応） |
| `main.py` | `_process_mcp_signal()` 追加・`signal_source="mcp"` でルーティング |
| `broker/mt5_broker.py` | GOLD `pip_unit = 0.10` 対応 |
| `config.json` | `mcp_ea` セクション追加（倍率・ロットスケール） |

---

## 主要設定項目（config.yaml）

| キー | デフォルト | 説明 |
|------|-----------|------|
| `system.scan_interval_seconds` | 300 | スキャン間隔（秒） |
| `breakout.score_threshold` | 7 | 発注トリガースコア |
| `breakout.cooldown_seconds` | 900 | 同一パターン再発注クールダウン |
| `detection.swing_order` | 5 | スイング感度（前後 N バーで判定） |
| `detection.sr_cluster_pips` | 5.0 | S/R クラスタリング幅（pips） |
| `detection.channel_r2_min` | 0.80 | チャネル線形回帰 R² 最低値 |
| `llm.pattern_score_threshold` | 60 | TV 描画を行う AI スコア下限 |

---

## ログ・状態管理

- ログ: `tv_mcp_ea/logs/tv_mcp_ea_YYYY-MM-DD.log`（14日保持）
- 描画状態: `tv_mcp_ea/state/drawing_state.json`（シンボル → entity_id リスト）
- クールダウン: `BreakoutDetector` インスタンス内メモリ管理（再起動でリセット）
