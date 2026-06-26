#!/usr/bin/env python3
"""
Razor's Edge — GitHub Actions 交易脚本
每 5 分钟运行一次，检查持仓 + 开仓信号
支持 OKX / Binance
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import ccxt
import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from data import MarketData
from risk import RiskManager
from strategy import RazorsEdgeStrategy

load_dotenv()

PROJ = Path(__file__).parent.parent
os.chdir(PROJ)

with open("config.yaml") as f:
    cfg = yaml.safe_load(f)

exchange_id = cfg.get("trading", {}).get("exchange", "okx").lower()
testnet = cfg.get("trading", {}).get("testnet", False)
proxy = cfg.get("trading", {}).get("proxy", "")


def build_exchange(exchange_name: str):
    if exchange_name == "okx":
        api_key = os.getenv("OKX_API_KEY", "")
        secret = os.getenv("OKX_SECRET_KEY", "")
        password = os.getenv("OKX_PASSPHRASE", "")
        if not api_key or not secret or not password:
            print("❌ 缺少 OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE")
            sys.exit(1)

        params = {
            "apiKey": api_key,
            "secret": secret,
            "password": password,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }
        if proxy:
            params["proxies"] = {"http": proxy, "https": proxy}
        exchange = ccxt.okx(params)
        exchange.set_sandbox_mode(bool(testnet))
        return exchange

    if exchange_name == "binance":
        api_key = os.getenv("BINANCE_API_KEY", "")
        secret = os.getenv("BINANCE_SECRET_KEY", "")
        if not api_key or not secret:
            print("❌ 缺少 BINANCE_API_KEY / BINANCE_SECRET_KEY")
            sys.exit(1)

        params = {
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        }
        if proxy:
            params["proxies"] = {"http": proxy, "https": proxy}
        exchange = ccxt.binance(params)
        exchange.set_sandbox_mode(bool(testnet))
        return exchange

    print(f"❌ 不支持的交易所: {exchange_name}")
    sys.exit(1)


exchange = build_exchange(exchange_id)
md = MarketData(exchange_id=exchange_id, testnet=testnet, proxy=proxy)
strat = RazorsEdgeStrategy(cfg)
risk = RiskManager(cfg)

STATE = PROJ / "cron_state.json"
JOURNAL = PROJ / "trade_journal.jsonl"
SYMBOLS = cfg["trading"]["symbols"]
COOLDOWN = cfg["strategy"].get("cooldown_bars", 5) * 5 * 60


def normalize_symbol(symbol: str) -> str:
    if exchange_id == "okx":
        return symbol if ":USDT" in symbol else f"{symbol}:USDT"
    return symbol


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"positions": {}, "last_signal": {}, "daily_pnl": 0, "daily_trades": 0}


def save_state(state):
    state["updated"] = datetime.now().isoformat()
    STATE.write_text(json.dumps(state, indent=2))


def log_trade(entry):
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(entry) + "\n")


state = load_state()
positions = state.get("positions", {})
last_signal = state.get("last_signal", {})

print(f"[{datetime.now().isoformat()}] {exchange_id.upper()} 扫描 {SYMBOLS} | 持仓 {len(positions)}")

for sym in list(positions.keys()):
    pos = positions[sym]
    symbol_norm = normalize_symbol(sym)
    try:
        ticker = exchange.fetch_ticker(symbol_norm)
        current = float(ticker["last"])
    except Exception as e:
        print(f"  查询失败 {sym}: {e}")
        continue

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
            side = "sell" if pos["direction"] == "LONG" else "buy"
            order_params = {"reduceOnly": True}
            if exchange_id == "binance":
                order_params["positionSide"] = "BOTH"
            exchange.create_order(
                symbol=symbol_norm,
                type="market",
                side=side,
                amount=pos["qty"],
                params=order_params,
            )
            pnl = (current - pos["entry_price"]) * pos["qty"]
            if pos["direction"] == "SHORT":
                pnl = -pnl

            state["daily_pnl"] += pnl
            emoji = "✅" if hit == "tp" else "🛑"
            print(f"{emoji} {sym} {hit} @ ${current:.2f} | PnL ${pnl:+.3f}")
            log_trade({
                "time": datetime.now().isoformat(),
                "exchange": exchange_id,
                "symbol": sym,
                "action": "close",
                "reason": hit,
                "exit_price": current,
                "pnl": pnl,
            })
            del positions[sym]
        except Exception as e:
            print(f"  平仓失败 {sym}: {e}")

for sym in SYMBOLS:
    if sym in positions:
        continue

    try:
        df_1h = md.fetch_recent_klines(sym, "1h", hours=48)
        strat.set_trend(sym, df_1h)
        df_5m = md.fetch_recent_klines(sym, "5m", hours=24)
        if df_5m.empty:
            continue
        df_5m = md.calculate_indicators(df_5m)
    except Exception as e:
        print(f"  数据失败 {sym}: {e}")
        continue

    sig = strat.evaluate(df_5m, sym)
    if sig is None:
        continue

    if sym in last_signal:
        t = datetime.fromisoformat(last_signal[sym])
        if (datetime.now() - t).total_seconds() < COOLDOWN:
            continue

    ok, reason = risk.can_trade(sig.score)
    if not ok:
        print(f"  风控 {sym}: {reason}")
        continue

    qty = risk.calculate_position_size(sig.price, sig.stop_loss)
    if qty <= 0:
        continue

    try:
        side = "buy" if sig.direction == "LONG" else "sell"
        symbol_norm = normalize_symbol(sym)
        order_params = {}
        if exchange_id == "binance":
            order_params["positionSide"] = "BOTH"
        order = exchange.create_order(
            symbol=symbol_norm,
            type="market",
            side=side,
            amount=qty,
            params=order_params,
        )
        fill = float(order.get("average", sig.price)) if order.get("average") else sig.price

        positions[sym] = {
            "entry_price": fill,
            "qty": qty,
            "direction": sig.direction,
            "sl": sig.stop_loss,
            "tp": sig.take_profit,
        }
        last_signal[sym] = datetime.now().isoformat()
        state["daily_trades"] += 1

        emoji = "📈" if sig.direction == "LONG" else "📉"
        print(f"{emoji} {sym} {sig.direction} {qty:.4f} @ ${fill:.2f} | {sig.reason}")
        log_trade({
            "time": datetime.now().isoformat(),
            "exchange": exchange_id,
            "symbol": sym,
            "action": "open",
            "direction": sig.direction,
            "entry_price": fill,
            "qty": qty,
            "sl": sig.stop_loss,
            "tp": sig.take_profit,
            "score": sig.score,
            "reason": sig.reason,
        })
    except Exception as e:
        print(f"  开仓失败 {sym}: {e}")

state["positions"] = positions
state["last_signal"] = last_signal
save_state(state)

print(f"✓ {exchange_id.upper()} 持仓 {len(positions)} | PnL ${state['daily_pnl']:+.2f} | 成交 {state['daily_trades']}")
