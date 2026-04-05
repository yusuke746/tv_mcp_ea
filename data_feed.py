"""
MT5 OHLCV データ取得モジュール

MetaTrader5 の copy_rates_from_pos を使い、
各シンボル・時間足の OHLCV バーを pandas DataFrame として返す。
"""

import logging
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5
    _MT5_AVAILABLE = True
except ImportError:
    mt5 = None
    _MT5_AVAILABLE = False
    logger.warning("MetaTrader5 not available. DataFeed will be disabled.")

# MT5 タイムフレーム文字列 → 定数マッピング
_TF_MAP = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 16385,
    "H4": 16388,
    "D1": 16408,
}


@dataclass
class OHLCVData:
    symbol: str
    timeframe: str
    bars: pd.DataFrame      # columns: time, open, high, low, close, volume
    atr14: float            # 最新14期間ATR（カーリング式）


class DataFeed:
    """MT5 から OHLCV を取得するクラス。"""

    def __init__(self, login: int, password: str, server: str):
        self._login = login
        self._password = password
        self._server = server
        self._initialized = False

    def connect(self) -> bool:
        if not _MT5_AVAILABLE:
            logger.error("MetaTrader5 module not available")
            return False
        if self._initialized:
            return True

        if not mt5.initialize(login=self._login, password=self._password, server=self._server):
            logger.error(f"MT5 initialize failed: {mt5.last_error()}")
            return False

        self._initialized = True
        logger.info(f"MT5 DataFeed connected: {self._server} / {self._login}")
        return True

    def disconnect(self) -> None:
        if self._initialized and _MT5_AVAILABLE:
            mt5.shutdown()
            self._initialized = False

    def get_ohlcv(self, symbol: str, timeframe: str, count: int) -> OHLCVData | None:
        """
        指定シンボル・時間足の最新 count バーを取得する。

        Args:
            symbol: MT5シンボル名（例: "USDJPY", "XAUUSD"）
            timeframe: "M15" | "H1" | "H4" など
            count: 取得バー数

        Returns:
            OHLCVData または None（取得失敗時）
        """
        if not self._initialized:
            logger.error("DataFeed not connected")
            return None

        tf_const = self._tf_const(timeframe)
        if tf_const is None:
            logger.error(f"Unknown timeframe: {timeframe}")
            return None

        # copy_rates_from_pos(symbol, timeframe, start_pos, count)
        #   start_pos=0: 現在バーから過去方向に count 本取得
        rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, count)
        if rates is None or len(rates) == 0:
            err = mt5.last_error()
            logger.warning(f"copy_rates_from_pos failed: {symbol} {timeframe} err={err}")
            return None

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.rename(columns={"tick_volume": "volume"})[
            ["time", "open", "high", "low", "close", "volume"]
        ].copy()
        df = df.sort_values("time").reset_index(drop=True)

        atr14 = self._calc_atr(df, period=14)
        return OHLCVData(symbol=symbol, timeframe=timeframe, bars=df, atr14=atr14)

    def get_open_positions(self, symbol: str) -> list[dict]:
        """指定シンボルの現在保有ポジション一覧を返す。"""
        if not self._initialized:
            return []
        try:
            positions = mt5.positions_get(symbol=symbol)
        except Exception:
            return []
        if positions is None:
            return []

        out: list[dict] = []
        for p in positions:
            # type: 0=BUY, 1=SELL
            out.append({
                "ticket": int(getattr(p, "ticket", 0)),
                "type": int(getattr(p, "type", -1)),
                "volume": float(getattr(p, "volume", 0.0)),
                "price_open": float(getattr(p, "price_open", 0.0)),
                "sl": float(getattr(p, "sl", 0.0)),
                "tp": float(getattr(p, "tp", 0.0)),
                "profit": float(getattr(p, "profit", 0.0)),
                "symbol": str(getattr(p, "symbol", symbol)),
            })
        return out

    # ------------------------------------------------------------------ #
    #  内部ヘルパー
    # ------------------------------------------------------------------ #

    @staticmethod
    def _tf_const(tf_str: str) -> int | None:
        """時間足文字列を MT5 定数に変換する。"""
        if not _MT5_AVAILABLE:
            return _TF_MAP.get(tf_str.upper())

        mapping = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
        }
        return mapping.get(tf_str.upper())

    @staticmethod
    def _calc_atr(df: pd.DataFrame, period: int = 14) -> float:
        """True Range の指数移動平均（ATR）を計算して最新値を返す。"""
        high = df["high"].values
        low = df["low"].values
        close = df["close"].values

        tr = np.maximum(
            high[1:] - low[1:],
            np.maximum(
                np.abs(high[1:] - close[:-1]),
                np.abs(low[1:] - close[:-1]),
            ),
        )
        if len(tr) < period:
            return float(tr.mean()) if len(tr) > 0 else 0.0

        # 初期値: 単純平均
        atr = float(tr[:period].mean())
        # Wilder 平滑化
        k = 1.0 / period
        for v in tr[period:]:
            atr = atr * (1 - k) + v * k
        return atr
