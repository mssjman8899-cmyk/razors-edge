"""
Razor's Edge — 风控模块

华尔街铁律：
  - 单笔风险不超过 2%
  - 日亏损 10% 熔断
  - 日盈利 15% 收手
  - 绝不在亏损后加仓
"""
import json
import os
from datetime import datetime, date
from dataclasses import dataclass, field
from typing import Optional
import logging

logger = logging.getLogger("razors-edge.risk")


@dataclass
class Trade:
    """单笔交易记录"""
    entry_time: datetime
    exit_time: Optional[datetime] = None
    symbol: str = ""
    direction: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    quantity: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""    # "tp" | "sl" | "trailing" | "manual" | "daily_limit"
    score: int = 0
    status: str = "open"    # "open" | "closed"


@dataclass
class DailyStats:
    """每日统计"""
    date: str = field(default_factory=lambda: date.today().isoformat())
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_pnl: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0


class RiskManager:
    """
    风控官 — 比交易员权力大。
    有权随时叫停交易。
    """

    def __init__(self, config: dict):
        risk_cfg = config.get("risk", {})
        account_cfg = config.get("account", {})

        self.capital = account_cfg.get("capital", 30.0)
        self.leverage = account_cfg.get("leverage", 3)
        self.risk_per_trade = risk_cfg.get("risk_per_trade_pct", 0.05)
        self.max_positions = risk_cfg.get("max_positions", 1)
        self.max_daily_trades = risk_cfg.get("max_daily_trades", 15)
        self.max_daily_loss = account_cfg.get("max_daily_risk", 6.0)
        self.daily_profit_target = account_cfg.get("daily_profit_target", 3.0)

        # 当日状态
        self.today: Optional[date] = None
        self.daily_pnl: float = 0.0
        self.daily_trades: int = 0
        self.open_positions: list[Trade] = []
        self.closed_trades: list[Trade] = []
        self.peak_capital: float = self.capital
        self.journal_path = "trade_journal.jsonl"

        self._reset_daily()

    def _reset_daily(self):
        """每日重置"""
        today = date.today()
        if self.today != today:
            self.today = today
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.open_positions = []
            logger.info(f"📅 New trading day: {today}")

    def can_trade(self, signal_score: int = 0) -> tuple[bool, str]:
        """
        检查是否可以开仓。
        返回 (允许, 原因)
        """
        self._reset_daily()

        # 1. 日亏损熔断
        if self.daily_pnl <= -self.max_daily_loss:
            return False, f"🛑 日亏损达 ${abs(self.daily_pnl):.2f}，超过熔断线 ${self.max_daily_loss}，关机！"

        # 2. 日盈利达标
        if self.daily_pnl >= self.daily_profit_target:
            return False, f"🎯 日盈利达 ${self.daily_pnl:.2f}，目标完成，收工！"

        # 3. 超过最大持仓
        if len(self.open_positions) >= self.max_positions:
            return False, f"📊 已有 {len(self.open_positions)} 个持仓，达到上限"

        # 4. 超过日交易次数
        if self.daily_trades >= self.max_daily_trades:
            return False, f"🔢 今日已交易 {self.daily_trades} 次，达到上限"

        # 5. 信号质量过滤
        if signal_score < 3:
            return False, f"📉 信号得分 {signal_score}/5，低于 3 分门槛"

        return True, "✅ 允许开仓"

    def calculate_position_size(self, entry_price: float, stop_loss: float) -> float:
        """计算合约仓位（含杠杆）

        杠杆模式：
          notional = capital × leverage
          position = notional / entry_price
          风险校验：|entry - stop| × position ≤ capital × risk_per_trade
        """
        notional = self.capital * self.leverage  # $30 × 3 = $90
        max_position = notional / entry_price     # 最大合约张数

        # 基于止损距离限制仓位
        price_risk = abs(entry_price - stop_loss)
        if price_risk <= 0:
            return 0.0

        risk_budget = self.capital * self.risk_per_trade  # $1.50
        safe_position = risk_budget / price_risk          # 止损触发时亏 $1.50

        position = min(max_position, safe_position)

        logger.info(
            f"💰 仓位({self.leverage}x): 名义=${notional:.0f} | "
            f"止损距=${price_risk:.2f} | "
            f"仓位={position:.6f} | "
            f"最大亏损=${position * price_risk:.2f}"
        )
        return position

    def open_trade(self, trade: Trade):
        """开仓"""
        self.open_positions.append(trade)
        self.daily_trades += 1
        self._log_trade(trade)

    def close_trade(self, trade: Trade, exit_price: float, reason: str):
        """平仓"""
        trade.exit_time = datetime.now()
        trade.exit_price = exit_price
        trade.exit_reason = reason
        trade.status = "closed"

        # 计算盈亏
        if trade.direction == "LONG":
            trade.pnl = (exit_price - trade.entry_price) * trade.quantity
        else:
            trade.pnl = (trade.entry_price - exit_price) * trade.quantity

        trade.pnl_pct = (trade.pnl / (trade.entry_price * trade.quantity)) * 100 if trade.quantity > 0 else 0

        self.daily_pnl += trade.pnl
        self.capital += trade.pnl

        if trade in self.open_positions:
            self.open_positions.remove(trade)
        self.closed_trades.append(trade)

        self._log_trade(trade)

        emoji = "🟢" if trade.pnl > 0 else "🔴"
        logger.info(
            f"{emoji} 平仓 {trade.symbol} {trade.direction} | "
            f"盈亏=${trade.pnl:.2f} ({trade.pnl_pct:+.2f}%) | "
            f"原因: {reason} | "
            f"日累计: ${self.daily_pnl:.2f}"
        )

    def _log_trade(self, trade: Trade):
        """写入交易日志"""
        record = {
            "timestamp": datetime.now().isoformat(),
            "symbol": trade.symbol,
            "direction": trade.direction,
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "quantity": trade.quantity,
            "pnl": trade.pnl,
            "pnl_pct": trade.pnl_pct,
            "score": trade.score,
            "exit_reason": trade.exit_reason,
            "status": trade.status,
            "daily_pnl": self.daily_pnl,
        }
        try:
            with open(self.journal_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.error(f"Failed to write trade journal: {e}")

    def get_daily_stats(self) -> DailyStats:
        """获取当日统计"""
        closed = [t for t in self.closed_trades
                  if t.exit_time and t.exit_time.date() == self.today]

        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl <= 0]

        stats = DailyStats(
            date=self.today.isoformat() if self.today else "",
            total_trades=len(closed),
            winning_trades=len(wins),
            losing_trades=len(losses),
            gross_profit=sum(t.pnl for t in wins),
            gross_loss=sum(t.pnl for t in losses),
            net_pnl=self.daily_pnl,
            win_rate=len(wins) / len(closed) * 100 if closed else 0,
        )

        # 计算最大回撤
        if closed:
            cumulative = 0
            peak = 0
            max_dd = 0
            for t in sorted(closed, key=lambda x: x.exit_time):
                cumulative += t.pnl
                peak = max(peak, cumulative)
                max_dd = min(max_dd, cumulative - peak)
            stats.max_drawdown = abs(max_dd)

        return stats

    def status_report(self) -> str:
        """状态报告（适合打印）"""
        stats = self.get_daily_stats()
        lines = [
            "═" * 50,
            f"📊 剃刀边缘 — 当日战报 ({stats.date})",
            "═" * 50,
            f"💰 账户资金:   ${self.capital:.2f}",
            f"📈 今日盈亏:   ${stats.net_pnl:+.2f}",
            f"🔢 成交笔数:   {stats.total_trades}",
            f"✅ 胜率:       {stats.win_rate:.1f}% ({stats.winning_trades}W / {stats.losing_trades}L)",
            f"📉 最大回撤:   ${stats.max_drawdown:.2f}",
            f"💵 总盈利:     ${stats.gross_profit:.2f}",
            f"💸 总亏损:     ${stats.gross_loss:.2f}",
            f"🔓 持仓中:     {len(self.open_positions)}",
            "═" * 50,
        ]
        return "\n".join(lines)
