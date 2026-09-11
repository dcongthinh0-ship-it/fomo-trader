# fomo-trader

Robinhood Chain（chain id `4663`）独立自动交易服务。它只接收 `fomo-monitor` 已通过固定三条件筛选的短 TTL 信号，直接核验 Uniswap 池并管理买入、receipt、持仓和固定 30% 全仓止盈。默认不交易。

## 安全边界

- `fomo-monitor` 永远只读；本仓库独占交易 RPC、钱包和 nonce 状态。
- `LIVE_TRADING_ENABLED=false` 为默认值。未完成 fork/testnet 闭环与独立主网授权时不要改成 `true`。
- 不要在聊天、Git 或 `.env` 中放私钥；私钥只放到宿主机权限为 `0600` 的文件，再只读挂载为 `/run/secrets/trader_private_key`。
- HMAC 共享密钥与钱包私钥是两份不同秘密，禁止复用。
- V2 与 V4 单池执行已经实现但仍受 live 总开关控制；V3 当前 fail-closed。详见 `docs/CONTRACTS.md`。

## 信号契约

`POST /v1/signals` 接收 `trade_signal_v1` 原始 JSON。签名为：

```text
hex(HMAC-SHA256(shared_secret, X-Signal-Timestamp + "." + raw_json_bytes))
```

新事件返回 `202 accepted`，重复 `event_id` 返回 `200 duplicate`。过期、非 4663、非 eligible、格式或签名错误均不入库。`GET /health` 不返回钱包、RPC URL 或密钥。

## 固定交易规则

服务不会重新查询市值或流动性，也不会增加 K 线、风控评分、探针、止损、回撤、加仓或超时卖出。买入确认后以 receipt 的实际 token 数量建仓：

```text
target_proceeds = actual_cost × 1.30
```

gas 不进入收益目标；只有全仓可执行报价达到目标才创建唯一 SELL。卖出失败保持 OPEN，有限重试后标记 `POSITION_STUCK`。

## 本地开发

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt pytest pytest-asyncio ruff
.venv/bin/ruff check .
.venv/bin/pytest -q
```

## Docker（默认禁用交易）

先建立共享私网，并在两个仓库各自准备同一份 HMAC 密钥文件；以下命令仅示例文件操作，终端不要打印秘密：

```bash
docker network inspect fomo-private >/dev/null 2>&1 || docker network create fomo-private
install -d -m 700 secrets
openssl rand -hex 32 > secrets/fomo_trader_shared_secret
chmod 600 secrets/fomo_trader_shared_secret
cp .env.example .env
docker compose build
docker compose up -d
docker compose exec fomo-trader python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8090/health").read().decode())'
```

容器只在 `fomo-private` 上 `expose 8090`，没有宿主机或公网端口映射。交易数据位于独立命名卷 `fomo-trader-data`，不与 monitor 共享数据库。

## 实盘前仍必须由用户在本机完成

1. 配置 `TRADER_WALLET_ADDRESS` 与 `TRADER_PRIVATE_KEY_HOST_FILE=/绝对路径/私钥文件`；Compose 只读挂载到容器，启动时确认派生地址匹配。未配置时挂载 `/dev/null`，live 模式会拒绝启动。
2. 配置稳定的独立生产 RPC，而不是公共限流端点。
3. 明确选择 ETH 或准确地址/精度的 USD 资产与金额、滑点、deadline。
4. 在 Robinhood Chain fork/testnet 完成真实 calldata、approval、买卖、nonce 重启、receipt 资产变化闭环。
5. 单独明确授权一次主网小额闭环；授权前保持 `LIVE_TRADING_ENABLED=false`。
