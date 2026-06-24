"""
Razor's Edge — 主交易机器人 v3
合约模式 | 趋势过滤 | 真实下单 | 自动止盈止损

⚠️ 实盘模式会真的在币安下单，谨慎使用。
"""
import os
import sys
import time
import yaml
import ccxt
import signal
from datetime import datetime
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", mode="a"),
    ],
    force=True,
)
logger = logging.getLogger("razors-edge")

sys.path.insert(0, str(Path(__file__).parent))
from data import MarketData
from strategy import RazorsEdgeStrategy, Signal
from risk import RiskManager, Trade

BANNER = r"""
╔══════════════════════════════════════╗
║    ⚔️  RAZOR'S EDGE v3  ⚔️          ║
║    $30 Contract Scalper LIVE         ║
║    "Trend is your only friend"       ║
╚══════════════════════════════════════╝
"""


class RazorsEdgeBot:
    """剃刀边缘 v3 — 实盘交易机器人"""

    def __init__(self, config_path: str = "config.yaml"):
        load_dotenv()
        with open(config_path) as f:
            self.config = yaml.safe_load(f)

        self.exchange_id = self.config.get("trading", {}).get("exchange", "binance")
        self.testnet = self.config.get("trading", {}).get("testnet", True)
        self.symbols = self.config.get("trading", {}).get("symbols", ["BTC/USDT"])
        self.timeframe = self.config.get("strategy", {}).get("timeframe", "5m")
        self.trend_tf = self.config.get("strategy", {}).get("trend_timeframe", "1h")
        self.cooldown_bars = self.config.get("strategy", {}).get("cooldown_bars", 5)
        self.proxy = self.config.get("trading", {}).get("proxy", "")
        self.leverage = self.config.get("account", {}).get("leverage", 5)
        self.direction_filter = self.config.get("strategy", {}).get("direction_filter", "")

        api_key = os.getenv("BINANCE_API_KEY", "")
        secret = os.getenv("BINANCE_SECRET_KEY", "")

        self.data = MarketData(
            exchange_id=self.exchange_id, testnet=self.testnet, proxy=self.proxy,
        )

        # 交易专用 exchange（期货）
        exchange_params = {
            "apiKey": api_key, "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        }
        if self.proxy:
            exchange_params["proxies"] = {"http": self.proxy, "https": self.proxy}

        self.trade_exchange = ccxt.binance(exchange_params)
        if self.testnet:
            self.trade_exchange.set_sandbox_mode(True)
            logger.info("🧪 期货模拟盘模式")
        else:
            logger.info("🔥 期货实盘模式")

        self.strategy = RazorsEdgeStrategy(self.config)
        self.risk = RiskManager(self.config)

        self.running = True
        self.last_signal_time: dict[str, datetime] = {}
        # 订单跟踪: symbol → {"order_id": str, "entry_price": float, "qty": float, "direction": str, "sl": float, "tp": float}
        self.positions: dict[str, dict] = {}

        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    def _shutdown(self, signum, frame):
        logger.info("\n🛑 收到退出信号，平仓中...")
        self.running = False

    def run(self):
        logger.info(BANNER)
        mode = "🧪 模拟盘" if self.testnet else "🔥 实盘"
        direction = "只做多" if self.direction_filter == "long_only" else "多空双向"
        logger.info(f"交易所: {self.exchange_id.upper()} | {mode} | {direction}")
        logger.info(f"本金: ${self.risk.capital:.0f} | 杠杆: {self.leverage}x | 名义: ${self.risk.capital * self.leverage:.0f}")
        logger.info(f"交易对: {', '.join(self.symbols)} | TF: {self.timeframe}")
        logger.info(f"日目标: ${self.risk.daily_profit_target} | 日熔断: ${self.risk.max_daily_loss}")

        # 设置杠杆
        for sym in self.symbols:
            try:
                self.trade_exchange.set_leverage(self.leverage, sym.replace("/", ""))
            except Exception as e:
                logger.warning(f"设置杠杆 {sym}: {e}")

        logger.info("=" * 50)

        last_heartbeat = time.time()
        while self.running:
            try:
                for symbol in self.symbols:
                    if not self.running:
                        break
                    self._process_symbol(symbol)

                if time.time() - last_heartbeat > 300:
                    last_heartbeat = time.time()
                    prices = []
                    for s in self.symbols:
                        p = self.data.get_current_price(s)
                        t = self.strategy.get_trend(s)
                        prices.append(f"{s.split('/')[0]} ${p:.0f}({t})")
                    logger.info(f"💓 {' | '.join(prices)} | P&L ${self.risk.daily_pnl:+.2f} | 持仓 {len(self.positions)}")

                time.sleep(25)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"异常: {e}", exc_info=True)
                time.sleep(60)

        logger.info("\n" + self.risk.status_report())
        logger.info("👋 剃刀入鞘")

    def _process_symbol(self, symbol: str):
        try:
            df_1h = self.data.fetch_recent_klines(symbol, self.trend_tf, hours=48)
            if not df_1h.empty:
                self.strategy.set_trend(symbol, df_1h)
        except Exception:
            pass

        # 已有持仓 → 检查止盈止损
        if symbol in self.positions:
            self._check_exit(symbol)
            return

        df = self.data.fetch_recent_klines(symbol, self.timeframe, hours=24)
        if df.empty:
            return
        df = self.data.calculate_indicators(df)

        signal = self.strategy.evaluate(df, symbol)
        if signal is None:
            return

        now = datetime.now()
        if symbol in self.last_signal_time:
            elapsed = (now - self.last_signal_time[symbol]).total_seconds()
            if elapsed < self.cooldown_bars * 5 * 60:
                return

        trend = self.strategy.get_trend(symbol)
        self._print_signal(signal, trend)

        can_trade, reason = self.risk.can_trade(signal.score)
        if not can_trade:
            logger.warning(f"⛔ {symbol} 风控: {reason}")
            return

        self._place_order(signal)

    def _print_signal(self, signal: Signal, trend: str):
        trend_emoji = {"bullish": "📈", "bearish": "📉", "neutral": "➡️"}.get(trend, "❓")
        dir_emoji = "🟢" if signal.direction == "LONG" else "🔴"
        score_bar = "█" * signal.score + "░" * (5 - signal.score)
        logger.info(f"\n{dir_emoji} {signal.symbol} {signal.direction} [{score_bar}] {signal.score}/5 | {trend_emoji} {trend}")
        logger.info(f"   入场=${signal.price:.2f} | 止损=${signal.stop_loss:.2f} | 止盈=${signal.take_profit:.2f}")
        logger.info(f"   理由: {signal.reason}")

    def _place_order(self, signal: Signal):
        """真实下单到币安"""
        qty = self.risk.calculate_position_size(signal.price, signal.stop_loss)
        if qty <= 0:
            return

        try:
            symbol_fmt = signal.symbol.replace("/", "")
            side = "buy" if signal.direction == "LONG" else "sell"

            order = self.trade_exchange.create_order(
                symbol=symbol_fmt,
                type="market",
                side=side,
                amount=qty,
                params={"positionSide": "BOTH"},
            )

            entry_price = float(order.get("average", signal.price)) if order.get("average") else signal.price
            order_id = order.get("id", "unknown")

            self.positions[signal.symbol] = {
                "order_id": order_id,
                "entry_price": entry_price,
                "qty": qty,
                "direction": signal.direction,
                "sl": signal.stop_loss,
                "tp": signal.take_profit,
            }
            self.last_signal_time[signal.symbol] = datetime.now()

            # 也记录到本地风控
            trade = Trade(
                entry_time=datetime.now(), symbol=signal.symbol,
                direction=signal.direction, entry_price=entry_price,
                quantity=qty, score=signal.score,
            )
            self.risk.open_trade(trade)

            notional = qty * entry_price
            logger.info(f"✅ 下单成功 {signal.symbol} {signal.direction} | {qty:.4f}张 @ ${entry_price:.2f} | 订单#{order_id}")
            logger.info(f"   名义=${notional:.0f} | 止损=${signal.stop_loss:.2f} | 止盈=${signal.take_profit:.2f}")

        except Exception as e:
            logger.error(f"❌ 下单失败 {signal.symbol}: {e}")

    def _check_exit(self, symbol: str):
        """检查是否触及止盈止损"""
        pos = self.positions[symbol]
        current = self.data.get_current_price(symbol)
        if current <= 0:
            return

        hit = None
        if pos["direction"] == "LONG":
            if current >= pos["tp"]:
                hit = "tp"
            elif current <= pos["sl"]:
                hit = "sl"
        else:
            if current <= pos["tp"]:
                hit = "tp"
            elif current >= pos["sl"]:
                hit = "sl"

        if hit:
            try:
                symbol_fmt = symbol.replace("/", "")
                side = "sell" if pos["direction"] == "LONG" else "buy"
                self.trade_exchange.create_order(
                    symbol=symbol_fmt, type="market", side=side,
                    amount=pos["qty"], params={"positionSide": "BOTH", "reduceOnly": True},
                )

                pnl = (current - pos["entry_price"]) * pos["qty"]
                if pos["direction"] == "SHORT":
                    pnl = -pnl

                self.risk.daily_pnl += pnl
                self.risk.capital += pnl

                emoji = "🎯" if hit == "tp" else "🛑"
                logger.info(f"{emoji} 平仓 {symbol} {pos['direction']} @ ${current:.2f} | "
                            f"盈亏 ${pnl:+.2f} | 原因: {hit} | 日累计 ${self.risk.daily_pnl:+.2f}")

                del self.positions[symbol]
            except Exception as e:
                logger.error(f"❌ 平仓失败 {symbol}: {e}")


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    bot = RazorsEdgeBot(config_path)
    bot.run()


if __name__ == "__main__":
    main()
