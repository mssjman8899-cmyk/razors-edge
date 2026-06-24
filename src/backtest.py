"""
Razor's Edge v2 — 回测模块
趋势过滤 + 严格信号 + 宽止损
"""
import sys
import yaml
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from data import MarketData
from strategy import RazorsEdgeStrategy, Signal
from risk import RiskManager, Trade


class BacktestEngine:
    """回测引擎 v2 — 带趋势过滤"""

    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path) as f:
            self.config = yaml.safe_load(f)

        self.symbols = self.config.get("trading", {}).get("symbols", ["BTC/USDT"])
        self.timeframe = self.config.get("strategy", {}).get("timeframe", "5m")
        self.trend_tf = self.config.get("strategy", {}).get("trend_timeframe", "1h")
        self.cooldown_bars = self.config.get("strategy", {}).get("cooldown_bars", 5)
        self.proxy = self.config.get("trading", {}).get("proxy", "")
        self.exchange_id = self.config.get("trading", {}).get("exchange", "binance")

        self.data = MarketData(exchange_id=self.exchange_id, testnet=True, proxy=self.proxy)
        self.strategy = RazorsEdgeStrategy(self.config)
        self.risk = RiskManager(self.config)

        self.equity_curve: list[float] = []
        self.all_trades: list[dict] = []

    def run(self, symbol: str, days: int = 7) -> pd.DataFrame:
        logger = __import__("logging").getLogger("razors-edge")
        logger.info(f"\n📊 回测 {symbol} | {self.exchange_id.upper()} | TF: {self.timeframe} | {days}天")
        logger.info(f"💰 本金: ${self.risk.capital} | 目标: ${self.risk.daily_profit_target}/天")

        since = int((datetime.now() - timedelta(days=days + 3)).timestamp() * 1000)

        # 拉 5m + 1h 数据
        df = self.data.fetch_ohlcv(symbol, self.timeframe, limit=3000, since=since)
        if df.empty:
            logger.error("5m 数据为空")
            return pd.DataFrame()

        df_1h = self.data.fetch_ohlcv(symbol, self.trend_tf, limit=200, since=since)

        df = self.data.calculate_indicators(df)
        logger.info(f"5m 数据: {len(df)} 根 | {df.index[0]} → {df.index[-1]}")
        if not df_1h.empty:
            logger.info(f"1h 数据: {len(df_1h)} 根 | 趋势过滤: 启用")

        cooldown = 0
        open_trade: Optional[Trade] = None
        trades = []

        for i in range(50, len(df)):
            bar_time = df.index[i]
            current = df.iloc[i]

            # 更新 1h 趋势（每根 5m bar 对应的 1h 窗口）
            if not df_1h.empty:
                df_1h_window = df_1h[df_1h.index <= bar_time]
                self.strategy.set_trend(symbol, df_1h_window)

            # --- 平仓检查 ---
            if open_trade is not None:
                closed = False
                if open_trade.direction == "LONG":
                    if current["low"] <= open_trade.exit_rules["sl"]:
                        self.risk.close_trade(open_trade, open_trade.exit_rules["sl"], "sl")
                        closed = True
                    elif current["high"] >= open_trade.exit_rules["tp"]:
                        self.risk.close_trade(open_trade, open_trade.exit_rules["tp"], "tp")
                        closed = True
                else:
                    if current["high"] >= open_trade.exit_rules["sl"]:
                        self.risk.close_trade(open_trade, open_trade.exit_rules["sl"], "sl")
                        closed = True
                    elif current["low"] <= open_trade.exit_rules["tp"]:
                        self.risk.close_trade(open_trade, open_trade.exit_rules["tp"], "tp")
                        closed = True

                if closed:
                    trades.append({
                        "entry_time": open_trade.entry_time,
                        "exit_time": bar_time,
                        "symbol": open_trade.symbol,
                        "direction": open_trade.direction,
                        "entry_price": open_trade.entry_price,
                        "exit_price": open_trade.exit_price,
                        "pnl": open_trade.pnl,
                        "pnl_pct": open_trade.pnl_pct,
                        "score": open_trade.score,
                        "exit_reason": open_trade.exit_reason,
                    })
                    self.equity_curve.append(self.risk.capital)
                    open_trade = None
                    cooldown = self.cooldown_bars

            # --- 冷却 ---
            if cooldown > 0:
                cooldown -= 1
                continue

            # --- 开仓 ---
            if open_trade is not None:
                continue

            window = df.iloc[: i + 1]
            signal = self.strategy.evaluate(window, symbol)
            if signal is None:
                continue

            can_trade, reason = self.risk.can_trade(signal.score)
            if not can_trade:
                continue

            quantity = self.risk.calculate_position_size(signal.price, signal.stop_loss)
            if quantity <= 0:
                continue

            trade = Trade(
                entry_time=bar_time, symbol=signal.symbol,
                direction=signal.direction, entry_price=signal.price,
                quantity=quantity, score=signal.score,
            )
            trade.exit_rules = {"sl": signal.stop_loss, "tp": signal.take_profit}
            self.risk.open_trade(trade)
            open_trade = trade

        # EOD 平仓
        if open_trade is not None:
            last_price = df["close"].iloc[-1]
            self.risk.close_trade(open_trade, last_price, "eod")
            trades.append({
                "entry_time": open_trade.entry_time,
                "exit_time": df.index[-1],
                "symbol": open_trade.symbol, "direction": open_trade.direction,
                "entry_price": open_trade.entry_price, "exit_price": last_price,
                "pnl": open_trade.pnl, "pnl_pct": open_trade.pnl_pct,
                "score": open_trade.score, "exit_reason": "eod",
            })

        self.all_trades = trades
        return pd.DataFrame(trades)

    def report(self) -> str:
        if not self.all_trades:
            return "📭 回测期间无交易信号（信号门槛太高或趋势过滤太严）"

        df = pd.DataFrame(self.all_trades)
        wins = df[df["pnl"] > 0]
        losses = df[df["pnl"] <= 0]

        total_pnl = df["pnl"].sum()
        total_trades = len(df)
        win_rate = len(wins) / total_trades * 100 if total_trades else 0
        avg_win = wins["pnl"].mean() if len(wins) else 0
        avg_loss = losses["pnl"].mean() if len(losses) else 0

        profit_factor = (
            abs(wins["pnl"].sum() / losses["pnl"].sum())
            if losses["pnl"].sum() != 0 else float("inf")
        )

        if self.equity_curve:
            peak = np.maximum.accumulate(self.equity_curve)
            drawdown = (np.array(self.equity_curve) - peak) / peak * 100
            max_dd = abs(drawdown.min()) if len(drawdown) else 0
        else:
            max_dd = 0

        if len(self.equity_curve) > 1:
            returns = np.diff(self.equity_curve) / np.array(self.equity_curve[:-1])
            sharpe = (np.mean(returns) / np.std(returns) * np.sqrt(252 * 24)) if np.std(returns) > 0 else 0
        else:
            sharpe = 0

        df["date"] = pd.to_datetime(df["exit_time"]).dt.date
        daily = df.groupby("date")["pnl"].sum()
        avg_daily = daily.mean()
        profitable_days = (daily > 0).sum()
        total_days = len(daily)

        lines = [
            "",
            "═" * 55,
            f"📊 剃刀边缘 v2 — 回测报告 ({self.exchange_id.upper()})",
            "═" * 55,
            f"📐 总交易:     {total_trades} 笔",
            f"✅ 胜率:       {win_rate:.1f}% ({len(wins)}W / {len(losses)}L)",
            f"💰 总盈亏:     ${total_pnl:+.2f} ({total_pnl/self.risk.capital*100:+.1f}%)",
            f"📈 平均盈利:   ${avg_win:+.3f}",
            f"📉 平均亏损:   ${avg_loss:+.3f}",
            f"⚖️  盈亏比:     {profit_factor:.2f}",
            f"📉 最大回撤:   {max_dd:.2f}%",
            f"📊 夏普比率:   {sharpe:.2f}",
            f"📅 日均收益:   ${avg_daily:+.3f}",
            f"🗓️  盈利天数:   {profitable_days}/{total_days}",
            "═" * 55,
        ]

        if "exit_reason" in df.columns:
            reason_stats = df.groupby("exit_reason").agg(count=("pnl", "count"), total_pnl=("pnl", "sum"))
            lines.append("📋 平仓原因:")
            for reason, row in reason_stats.iterrows():
                lines.append(f"   {reason}: {int(row['count'])}笔 | ${row['total_pnl']:+.2f}")

        # 方向统计
        dir_stats = df.groupby("direction").agg(count=("pnl", "count"), total_pnl=("pnl", "sum"), win_rate=("pnl", lambda x: (x > 0).sum() / len(x) * 100))
        lines.append("📊 方向统计:")
        for d, row in dir_stats.iterrows():
            lines.append(f"   {d}: {int(row['count'])}笔 | ${row['total_pnl']:+.2f} | 胜率 {row['win_rate']:.0f}%")

        lines.append("═" * 55)
        return "\n".join(lines)


def main():
    config_path = sys.argv[2] if len(sys.argv) > 2 else "config.yaml"
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTC/USDT"

    engine = BacktestEngine(config_path)
    trades_df = engine.run(symbol, days=7)
    print(trades_df.to_string() if not trades_df.empty else "")
    print(engine.report())


if __name__ == "__main__":
    main()
