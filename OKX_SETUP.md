# OKX 自动化接入

## 1. 本地 .env

填写：

```env
OKX_API_KEY=你的_okx_api_key
OKX_SECRET_KEY=你的_okx_secret_key
OKX_PASSPHRASE=你的_okx_passphrase
```

## 2. GitHub Secrets

仓库 `Settings -> Secrets and variables -> Actions` 新增：

- `OKX_API_KEY`
- `OKX_SECRET_KEY`
- `OKX_PASSPHRASE`

## 3. 自动运行

工作流文件：`.github/workflows/trade.yml`

- 每 5 分钟自动运行一次
- 支持手动点击 `Run workflow`

## 4. 当前默认

- 交易所：`OKX`
- 市场：`swap`
- 交易对：`BTC/USDT`、`ETH/USDT`
- 模式：`testnet: false`（实盘）

## 5. 备注

如果以后要切回 Binance：

- 改 `config.yaml` 里的 `trading.exchange`
- 保留 / 填写 `BINANCE_API_KEY`、`BINANCE_SECRET_KEY`
