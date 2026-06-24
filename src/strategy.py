"""
Razor's Edge v2 — 策略模块
动量剥头皮 + 趋势过滤 + 均值回归

信号评分（满分 5 分）：
  - EMA 金叉/死叉: 2 分
  - RSI 极端区域反弹: 2 分
  - 成交量放量确认: 1 分
  - 趋势方向加分: +1（顺势）
  - 逆势减分: -2（过滤）
  开仓阈值: ≥ 3 分
"""
import pandas as pd
import numpy as np
from typing import Optional, Tuple
from dataclasses import dataclass
import logging

logger = logging.getLogger("razors-edge.strategy")


@dataclass
class Signal:
    timestamp: pd.Timestamp
    symbol: str
    direction: str        # "LONG" | "SHORT"
    score: int            # 0-5
    price: float
    ema_cross: bool
    rsi_signal: bool
    volume_spike: bool
    stop_loss: float
    take_profit: float
    reason: str


class RazorsEdgeStrategy:
    """剃刀边缘 v2 — 加趋势过滤、收紧信号"""

    def __init__(self, config: dict):
        cfg = config.get("strategy", {})
        self.ema_fast = cfg.get("ema_fast", 9)
        self.ema_slow = cfg.get("ema_slow", 21)
        self.rsi_period = cfg.get("rsi_period", 14)
        self.rsi_oversold = cfg.get("rsi_oversold", 30)
        self.rsi_overbought = cfg.get("rsi_overbought", 70)
        self.volume_spike_mult = cfg.get("volume_spike_mult", 1.8)
        self.min_score = cfg.get("min_signal_score", 3)
        self.cooldown_bars = cfg.get("cooldown_bars", 5)
        self.trend_filter_enabled = cfg.get("trend_filter", True)
        self.trend_timeframe = cfg.get("trend_timeframe", "1h")
        self.direction_filter = cfg.get("direction_filter", "")  # "" | "long_only" | "short_only"

        risk_cfg = config.get("risk", {})
        self.stop_atr_mult = risk_cfg.get("stop_loss_atr_mult", 2.5)
        self.tp_rr = risk_cfg.get("take_profit_rr", 1.8)
        self.min_stop_pct = risk_cfg.get("min_stop_pct", 0.004)

        # 1h 趋势缓存
        self._trend_cache: dict[str, str] = {}

    def set_trend(self, symbol: str, df_1h: pd.DataFrame):
        """设置 1h 趋势（由外部注入）"""
        if df_1h.empty or len(df_1h) < 22:
            self._trend_cache[symbol] = "neutral"
            return

        df_1h = df_1h.copy()
        df_1h["ema21"] = df_1h["close"].ewm(span=21, adjust=False).mean()
        latest = df_1h.iloc[-1]
        prev = df_1h.iloc[-2] if len(df_1h) >= 2 else latest

        if latest["close"] > latest["ema21"] and latest["ema21"] > prev.get("ema21", latest["ema21"]):
            self._trend_cache[symbol] = "bullish"
        elif latest["close"] < latest["ema21"] and latest["ema21"] < prev.get("ema21", latest["ema21"]):
            self._trend_cache[symbol] = "bearish"
        else:
            self._trend_cache[symbol] = "neutral"

    def get_trend(self, symbol: str) -> str:
        return self._trend_cache.get(symbol, "neutral")

    def evaluate(self, df: pd.DataFrame, symbol: str) -> Optional[Signal]:
        """评估最新 K 线，返回信号或 None"""
        if len(df) < 50:
            return None

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        trend = self.get_trend(symbol)

        # ── 多头信号 ──
        if self.direction_filter != "short_only" and trend != "bearish":
            long_score, long_reasons = self._score_long(df, latest, prev, trend)
            if long_score >= self.min_score:
                entry_price = latest["close"]
                stop_loss = self._calc_stop_loss(df, "LONG", entry_price)
                take_profit = self._calc_take_profit(entry_price, stop_loss, "LONG")
                return Signal(
                    timestamp=df.index[-1], symbol=symbol, direction="LONG",
                    score=long_score, price=entry_price,
                    ema_cross="ema" in long_reasons,
                    rsi_signal="rsi" in long_reasons,
                    volume_spike="vol" in long_reasons,
                    stop_loss=stop_loss, take_profit=take_profit,
                    reason=" | ".join(long_reasons),
                )

        # ── 空头信号 ──
        if self.direction_filter != "long_only" and trend != "bullish":
            short_score, short_reasons = self._score_short(df, latest, prev, trend)
            if short_score >= self.min_score:
                entry_price = latest["close"]
                stop_loss = self._calc_stop_loss(df, "SHORT", entry_price)
                take_profit = self._calc_take_profit(entry_price, stop_loss, "SHORT")
                return Signal(
                    timestamp=df.index[-1], symbol=symbol, direction="SHORT",
                    score=short_score, price=entry_price,
                    ema_cross="ema" in short_reasons,
                    rsi_signal="rsi" in short_reasons,
                    volume_spike="vol" in short_reasons,
                    stop_loss=stop_loss, take_profit=take_profit,
                    reason=" | ".join(short_reasons),
                )

        return None

    def _score_long(self, df: pd.DataFrame, latest: pd.Series, prev: pd.Series, trend: str) -> Tuple[int, list]:
        score = 0
        reasons = []

        # 1. EMA 金叉 (2 分)
        if prev["ema9"] <= prev["ema21"] and latest["ema9"] > latest["ema21"]:
            score += 2
            reasons.append("ema↑")
        elif latest["ema9"] > latest["ema21"] and latest["close"] > latest["ema9"]:
            score += 1
            reasons.append("ema_bull")

        # 2. RSI 超卖反弹 (2 分) — 只在 < 35 时给分
        if prev["rsi"] < self.rsi_oversold:
            score += 2
            reasons.append("rsi_os")
        elif latest["rsi"] < 40:
            score += 1
            reasons.append("rsi_low")

        # 3. 放量 (1 分)
        vol_ratio = latest.get("volume_ratio", 1)
        if pd.notna(vol_ratio) and vol_ratio >= self.volume_spike_mult:
            score += 1
            reasons.append("vol↑")

        # 趋势加分
        if trend == "bullish":
            score += 1
            reasons.append("trend↑")

        # 过滤：价格在 EMA21 下方且无反弹迹象 → 不做
        if latest["close"] < latest["ema21"] and latest["rsi"] > 40:
            score -= 2

        return max(score, 0), reasons

    def _score_short(self, df: pd.DataFrame, latest: pd.Series, prev: pd.Series, trend: str) -> Tuple[int, list]:
        score = 0
        reasons = []

        # 1. EMA 死叉 (2 分)
        if prev["ema9"] >= prev["ema21"] and latest["ema9"] < latest["ema21"]:
            score += 2
            reasons.append("ema↓")
        elif latest["ema9"] < latest["ema21"] and latest["close"] < latest["ema9"]:
            score += 1
            reasons.append("ema_bear")

        # 2. RSI 超买回落 (2 分)
        if prev["rsi"] > self.rsi_overbought:
            score += 2
            reasons.append("rsi_ob")
        elif latest["rsi"] > 60:
            score += 1
            reasons.append("rsi_high")

        # 3. 放量 (1 分)
        vol_ratio = latest.get("volume_ratio", 1)
        if pd.notna(vol_ratio) and vol_ratio >= self.volume_spike_mult:
            score += 1
            reasons.append("vol↑")

        # 趋势加分
        if trend == "bearish":
            score += 1
            reasons.append("trend↓")

        # 过滤：价格在 EMA21 上方且无回落迹象 → 不做
        if latest["close"] > latest["ema21"] and latest["rsi"] < 60:
            score -= 2

        return max(score, 0), reasons

    def _calc_stop_loss(self, df: pd.DataFrame, direction: str, entry: float) -> float:
        """计算止损 — ATR 和百分比取大值"""
        atr = df["atr"].iloc[-1]
        if pd.isna(atr) or atr <= 0:
            atr = entry * 0.002

        atr_stop = atr * self.stop_atr_mult
        pct_stop = entry * self.min_stop_pct
        # 止损距离 = max(ATR止损, 最小百分比止损)
        stop_distance = max(atr_stop, pct_stop)
        # 上限 3%（不能亏太多）
        stop_distance = min(stop_distance, entry * 0.03)

        if direction == "LONG":
            return entry - stop_distance
        else:
            return entry + stop_distance

    def _calc_take_profit(self, entry: float, stop_loss: float, direction: str) -> float:
        """基于盈亏比计算止盈"""
        risk = abs(entry - stop_loss)
        if direction == "LONG":
            return entry + risk * self.tp_rr
        else:
            return entry - risk * self.tp_rr
