"""
TV MCP EA — メインエントリーポイント

APScheduler で 5 分ごとに全ペアをスキャンし、
ブレイクアウト検出 → fx_system webhook POST を実行する。

TradingView デスクトップの CDP が利用可能な場合は
チャートパターンの描画も行う。
"""

import asyncio
import os
import signal
import sys
from pathlib import Path

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from loguru import logger

# ─── ローカルモジュール ──────────────────────────────────────────────────────

sys.path.insert(0, str(Path(__file__).parent))

from cdp_client import CDPClient, CDPError
from data_feed import DataFeed
from detection import find_swings, find_sr_levels, filter_sr_near_price, find_triangles, find_channels
from analysis.ai_scorer import AIScorer
from analysis.indicators import compute_indicators
from analysis.pine_features import read_pine_features
from drawing.chart_manager import ChartManager
from drawing.alert_manager import AlertManager
from executor.breakout_detector import BreakoutDetector, BreakoutSignal
from executor.context_sender import ContextSender

# ─── 定数 ────────────────────────────────────────────────────────────────────

_CONFIG_FILE = Path(__file__).parent / "config.yaml"


# ─── メインクラス ─────────────────────────────────────────────────────────────

class TVMcpEA:
    def __init__(self, cfg: dict):
        self._cfg = cfg
        sys_cfg  = cfg["system"]
        det_cfg  = cfg["detection"]
        brk_cfg  = cfg["breakout"]
        llm_cfg  = cfg["llm"]
        mt5_cfg  = cfg["mt5"]

        # MT5 DataFeed
        self._data_feed = DataFeed(
            login=int(os.environ[mt5_cfg["login_env"]]),
            password=os.environ[mt5_cfg["password_env"]],
            server=os.environ[mt5_cfg["server_env"]],
        )

        # CDP クライアント（描画用）
        self._cdp = CDPClient(
            host=sys_cfg["cdp_host"],
            port=int(sys_cfg["cdp_port"]),
        )
        self._cdp_connected = False

        # AI スコアラー
        openai_key = os.environ.get(llm_cfg["api_key_env"], "")
        self._scorer = AIScorer(api_key=openai_key, model=llm_cfg["model"])

        # チャートマネージャ
        self._chart_mgr = ChartManager(self._cdp)

        # ブレイクアウト検出器
        self._detector = BreakoutDetector(
            ai_scorer=self._scorer,
            webhook_url=sys_cfg["fx_system_webhook_url"],
            webhook_token=os.environ.get("WEBHOOK_SECRET", os.environ.get("WEBHOOK_TOKEN", "")),
            score_threshold=int(brk_cfg["score_threshold"]),
            cooldown_seconds=int(brk_cfg["cooldown_seconds"]),
            volume_surge_ratio=float(brk_cfg["volume_surge_ratio"]),
            body_ratio_min=float(brk_cfg["body_ratio_min"]),
        )

        # Market Context 送信器（エグジット支援用）
        self._context_sender = ContextSender(
            webhook_url=sys_cfg["fx_system_webhook_url"],
            webhook_token=os.environ.get("WEBHOOK_SECRET", os.environ.get("WEBHOOK_TOKEN", "")),
        )

        # アラート管理（TV アラート設定 + 削除）
        alert_cfg = cfg.get("alerts", {})
        self._alert_mgr = AlertManager(
            cdp=self._cdp,
            max_alerts_per_symbol=int(alert_cfg.get("max_per_symbol", 4)),
        )
        self._alerts_enabled = bool(alert_cfg.get("enabled", True))

        # スケジューラ
        self._scheduler = AsyncIOScheduler()
        self._scan_interval = int(sys_cfg["scan_interval_seconds"])

    # ─── ライフサイクル ─────────────────────────────────────────────────────

    async def start(self) -> None:
        logger.info("TV MCP EA starting...")

        # MT5 接続
        if not self._data_feed.connect():
            logger.error("MT5 connection failed. Exiting.")
            return

        # CDP 接続（失敗しても続行）
        await self._try_connect_cdp()

        # APScheduler 設定
        self._scheduler.add_job(
            self._scan_job,
            trigger="interval",
            seconds=self._scan_interval,
            id="scan_all_pairs",
            max_instances=1,
            misfire_grace_time=60,
        )
        self._scheduler.start()
        logger.info(f"Scheduler started. Scan interval: {self._scan_interval}s")

        # 起動直後に 1 回スキャン
        await self._scan_job()

        # 終了シグナル処理（Windows は add_signal_handler 非対応なので try/except）
        if sys.platform != "win32":
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        logger.info("TV MCP EA running. Press Ctrl+C to stop.")
        try:
            while self._scheduler.running:
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        logger.info("TV MCP EA shutting down...")
        self._scheduler.shutdown(wait=False)
        if self._cdp_connected:
            await self._cdp.disconnect()
        self._data_feed.disconnect()
        logger.info("TV MCP EA stopped.")

    # ─── CDP 接続管理 ───────────────────────────────────────────────────────

    async def _try_connect_cdp(self) -> bool:
        if self._cdp_connected:
            return True
        try:
            await self._cdp.connect()
            self._cdp_connected = True
            logger.info("CDP connected to TradingView")
            return True
        except Exception as e:
            logger.warning(f"CDP not available: {e} — drawings disabled")
            self._cdp_connected = False
            return False

    # ─── スキャンジョブ ─────────────────────────────────────────────────────

    async def _scan_job(self) -> None:
        logger.info("=== Scan cycle start ===")
        det_cfg = self._cfg["detection"]

        for pair in self._cfg["pairs"]:
            tv_sym  = pair["tv_symbol"]
            mt5_sym = pair["mt5_symbol"]
            pip     = float(pair["pip_size"])

            try:
                await self._scan_pair(pair, det_cfg, tv_sym, mt5_sym, pip)
            except Exception as e:
                logger.error(f"Error scanning {mt5_sym}: {e}", exc_info=True)

        logger.info("=== Scan cycle end ===")

    async def _scan_pair(
        self, pair: dict, det_cfg: dict,
        tv_sym: str, mt5_sym: str, pip: float,
    ) -> None:
        # ─── データ取得 ─────────────────────────────────────────────────────
        ohlcv_15m = self._data_feed.get_ohlcv(
            mt5_sym, det_cfg["timeframe_entry"], int(det_cfg["bars_entry"])
        )
        ohlcv_1h = self._data_feed.get_ohlcv(
            mt5_sym, det_cfg["timeframe_bias"], int(det_cfg["bars_bias"])
        )
        if ohlcv_15m is None:
            logger.warning(f"MT5 data unavailable for {mt5_sym}")
            return

        df = ohlcv_15m.bars

        # ─── パターン検出 ────────────────────────────────────────────────────
        current_bar = len(df) - 2  # 最後の確定バー

        swings   = find_swings(df, order=int(det_cfg["swing_order"]))
        highs    = [s for s in swings if s.kind == "high"]
        lows     = [s for s in swings if s.kind == "low"]

        sr_levels = find_sr_levels(
            swings,
            pip_size=pip,
            cluster_pips=float(det_cfg["sr_cluster_pips"]),
            min_touches=int(det_cfg["sr_min_touches"]),
        )
        # 現在価格の ±ATR×3 以内のレベルに絞り込む
        current_price = float(df.iloc[-2]["close"]) if len(df) >= 2 else 0.0
        sr_levels = filter_sr_near_price(
            sr_levels, current_price, ohlcv_15m.atr14, atr_multiplier=3.0
        )

        triangles = find_triangles(
            highs, lows, current_bar,
            min_touches=int(det_cfg["triangle_min_touches"]),
            max_bars=int(det_cfg["triangle_max_bars"]),
        )

        channels = find_channels(
            highs, lows, current_bar,
            pip_size=pip,
            max_bars=int(det_cfg["channel_max_bars"]),
            min_r2=float(det_cfg["channel_r2_min"]),
        )

        logger.debug(
            f"{mt5_sym}: SR={len(sr_levels)} TRI={len(triangles)} CH={len(channels)}"
        )

        # ─── チャート描画更新（CDP 使用時のみ）──────────────────────────────
        if self._cdp_connected:
            # CDP が切れている可能性を考慮して再接続を試みる
            try:
                ai_threshold = int(self._cfg["llm"]["pattern_score_threshold"])
                # AI スコアの高いパターンのみ描画対象に絞る（省略可）
                await self._chart_mgr.update_drawings(
                    target_tv_symbol=tv_sym,
                    df=df,
                    sr_levels=sr_levels,
                    triangles=triangles,
                    channels=channels,
                )
            except CDPError as e:
                logger.warning(f"CDP drawing failed: {e} — reconnecting next cycle")
                self._cdp_connected = False

        # ─── TV アラート更新（古いアラート削除 → 新アラート設定）────────────
        if self._cdp_connected and self._alerts_enabled:
            try:
                n_alerts = await self._alert_mgr.update_alerts(
                    tv_symbol=tv_sym,
                    mt5_symbol=mt5_sym,
                    sr_levels=sr_levels,
                    triangles=triangles,
                    channels=channels,
                    current_price=current_price,
                    atr=ohlcv_15m.atr14,
                )
                logger.debug(f"{mt5_sym}: {n_alerts} TV alerts updated")
            except Exception as e:
                logger.warning(f"Alert update failed: {e}")

        # ─── ブレイクアウト検出 & webhook POST ─────────────────────────────
        if ohlcv_1h is None:
            logger.warning(f"1H data unavailable for {mt5_sym}, HTF bias skipped")
            from data_feed import OHLCVData
            import pandas as pd
            ohlcv_1h_safe = OHLCVData(mt5_sym, "H1", pd.DataFrame(), 0.0)
        else:
            ohlcv_1h_safe = ohlcv_1h

        # 4H データ取得（LightGBM 特徴量用）
        ohlcv_4h = self._data_feed.get_ohlcv(
            mt5_sym, det_cfg.get("timeframe_htf", "H4"), int(det_cfg.get("bars_htf", 60))
        )
        df_4h = ohlcv_4h.bars if ohlcv_4h is not None and len(ohlcv_4h.bars) > 0 else None

        # テクニカル指標計算: Pine テーブル優先 → Python 近似フォールバック
        indicators = None
        if self._cdp and self._cdp._ws:
            try:
                indicators = await read_pine_features(self._cdp)
                if indicators:
                    logger.debug(f"{mt5_sym}: Pine features read ({len(indicators)} keys)")
            except Exception as e:
                logger.debug(f"{mt5_sym}: Pine features unavailable: {e}")

        if indicators is None:
            indicators = compute_indicators(
                df_15m=df,
                df_1h=ohlcv_1h_safe.bars if ohlcv_1h_safe and len(ohlcv_1h_safe.bars) > 0 else None,
                df_4h=df_4h,
                atr_14=ohlcv_15m.atr14,
            )
            logger.debug(f"{mt5_sym}: Using Python indicator fallback")

        fired = await self._detector.detect_and_fire(
            tv_symbol=tv_sym,
            mt5_symbol=mt5_sym,
            ohlcv_15m=ohlcv_15m,
            ohlcv_1h=ohlcv_1h_safe,
            sr_levels=sr_levels,
            triangles=triangles,
            channels=channels,
            indicators=indicators,
        )

        if fired:
            logger.info(f"{mt5_sym}: {len(fired)} breakout signal(s) fired")

        # ─── Market Context 送信（エグジット支援）──────────────────────────
        ctx = self._context_sender.build_context(
            mt5_symbol=mt5_sym,
            current_price=current_price,
            ohlcv_15m=ohlcv_15m,
            ohlcv_1h=ohlcv_1h_safe,
            sr_levels=sr_levels,
            swings=swings,
        )
        await self._context_sender.send(ctx)


# ─── エントリーポイント ────────────────────────────────────────────────────────

def main() -> None:
    # .env 読込（fx_system の .env と共有可）
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        env_path = Path(__file__).parent.parent / "fx_system" / ".env"
    load_dotenv(dotenv_path=env_path)

    # ログ設定
    with open(_CONFIG_FILE, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    log_level = cfg.get("system", {}).get("log_level", "INFO")
    logger.remove()
    logger.add(sys.stderr, level=log_level)
    logger.add(
        Path(__file__).parent / "logs" / "tv_mcp_ea_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="14 days",
        level="DEBUG",
    )

    # 標準 logging → loguru 転送（chart_manager, ai_scorer 等）
    import logging

    class _InterceptHandler(logging.Handler):
        def emit(self, record):
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno
            logger.opt(depth=6, exception=record.exc_info).log(level, record.getMessage())

    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)

    ea = TVMcpEA(cfg)
    asyncio.run(ea.start())


if __name__ == "__main__":
    main()
