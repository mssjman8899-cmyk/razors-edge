"""
Razor's Edge — 数据模块
支持 Binance / OKX，历史 K 线 + 技术指标
"""
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Optional
import logging

logger = logging.getLogger("razors-edge.data")


class MarketData:
    """多交易所数据源（OKX / Binance）"""

    EXCHANGE_CLASSES = {
        "okx": ccxt.okx,
        "binance": ccxt.binance,
    }

    def __init__(self, exchange_id: str = "okx", api_key: str = "", secret: str = "",
                 password: str = "", testnet: bool = True, proxy: str = ""):
        self.exchange_id = exchange_id
        self.testnet = testnet

        if exchange_id not in self.EXCHANGE_CLASSES:
            raise ValueError(f"Unsupported exchange: {exchange_id}. Use: {list(self.EXCHANGE_CLASSES)}")

        params = {
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }

        if exchange_id == "okx":
            params["password"] = password
            if testnet:
                params["hostname"] = "www.okx.com"  # OKX 没有独立 testnet，用 demo trading

        if proxy:
            params["proxies"] = {"http": proxy, "https": proxy}

        self.exchange = self.EXCHANGE_CLASSES[exchange_id](params)
        logger.info(f"📡 数据源: {exchange_id.upper()} {'(代理: ' + proxy + ')' if proxy else ''}")

    def fetch_ohlcv(
        self, symbol: str, timeframe: str = "5m",
        limit: int = 500, since: Optional[int] = None,
    ) -> pd.DataFrame:
        """获取 K 线数据"""
        try:
            # OKX 用 BTC/USDT:USDT 格式
            if self.exchange_id == "okx" and "/" in symbol and ":USDT" not in symbol:
                symbol = f"{symbol}:USDT"

            raw = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit, since=since)
            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            df = df.astype(float)
            logger.debug(f"Fetched {len(df)} {timeframe} candles for {symbol}")
            return df
        except Exception as e:
            logger.error(f"Failed to fetch OHLCV for {symbol}: {e}")
            return pd.DataFrame()

    def fetch_recent_klines(self, symbol: str, timeframe: str = "5m", hours: int = 24) -> pd.DataFrame:
        """获取最近 N 小时 K 线"""
        since_ms = int((datetime.now() - timedelta(hours=hours)).timestamp() * 1000)
        return self.fetch_ohlcv(symbol, timeframe=timeframe, limit=1000, since=since_ms)

    def get_ticker(self, symbol: str) -> dict:
        try:
            if self.exchange_id == "okx":
                symbol = f"{symbol}:USDT"
            return self.exchange.fetch_ticker(symbol)
        except Exception as e:
            logger.error(f"Failed to fetch ticker: {e}")
            return {}

    def get_current_price(self, symbol: str) -> float:
        ticker = self.get_ticker(symbol)
        return float(ticker.get("last", 0))

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算全部技术指标"""
        df = df.copy()

        # EMA
        df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
        df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()

        # RSI
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        df["rsi"] = 100 - (100 / (1 + rs))

        # ATR
        high_low = df["high"] - df["low"]
        high_close = abs(df["high"] - df["close"].shift(1))
        low_close = abs(df["low"] - df["close"].shift(1))
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()

        # 成交量均线
        df["volume_ma"] = df["volume"].rolling(window=20).mean()
        df["volume_ratio"] = df["volume"] / df["volume_ma"].replace(0, np.nan)

        # EMA 交叉
        df["ema_cross"] = np.where(df["ema9"] > df["ema21"], 1, -1)

        # 支撑阻力
        df["resistance"] = df["high"].rolling(window=20).max()
        df["support"] = df["low"].rolling(window=20).min()

        return df


# 兼容别名
BinanceData = MarketData
