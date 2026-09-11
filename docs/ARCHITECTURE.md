# fomo-trader 架构

## 当前同步状态

- 日期：2026-09-11
- 状态：HMAC/SQLite/可恢复状态机、V2 与 V4 单池买卖实现齐备；V3 保持 fail-closed，实盘默认关闭。

## 边界与数据流

`fomo-monitor` 只通过 `fomo-private` 私网发送已判断信号。API 先在原始字节上核验 HMAC、时间戳、版本、链、地址、TTL 和 `eligible=true`，再按 `event_id` 幂等写入 SQLite WAL。HTTP 接收只落库；交易 worker 与请求解耦。

```text
monitor outbox -> POST /v1/signals -> signals(SQLite) -> worker -> Uniswap adapter -> RPC
                                           |             |
                                           |             +-> orders / execution_attempts
                                           +----------------> positions
```

## 模块职责

- `settings.py`：环境/YAML、固定策略与 live 凭据闭锁。
- `auth.py`、`models.py`、`signals.py`、`api.py`：通信认证、验证、幂等接收和健康接口。
- `db.py`：signals/orders/positions/execution_attempts/nonce_state 持久化；签名、广播、approval 与 receipt 事实分步留痕。
- `execution.py`、`worker.py`：适配器协议和可恢复买卖状态机。
- `rpc.py`、`nonce.py`：隔离 RPC、有限重试、nonce 协调与恢复。
- `pools.py`、`uniswap.py`：官方部署核验、池识别、V2 直接/单桥路由，以及 V4 PoolKey 恢复、Quoter 报价、Universal Router 2.1.1/Permit2 单池买卖、签名与 receipt 解析。
- `orders.py`、`positions.py`：唯一订单和 30% 全仓止盈领域写入。

## 不可破坏约束

1. `LIVE_TRADING_ENABLED=false` 是默认值；未授权不得广播。
2. 同一 event 最多一个 BUY 与一个 SELL；签名前即保存唯一订单，签名后、广播前先持久化 tx hash/nonce；超时或重启只按 receipt 恢复，并可重新核验池上下文解析卖出结果，不能重买。
3. 目标固定为 `actual_cost × 1.30`，gas 不计入成本；只卖 100%，不含止损或其他策略。
4. 市值与流动性只来自信号且不在本服务重查；链上池/路由核验不是新入场条件。
5. V4 pool id 是 32 字节标识，绝不能当合约地址调用；PoolKey 必须来自 PoolManager 的对应 `Initialize` 日志或信号字段，并重新计算 pool id 核对。
6. 私钥和共享密钥只从只读文件读取，且被 Git/Docker build context 排除；Compose 未配置私钥时挂载 `/dev/null`，live 启动必然失败；健康接口与日志不泄露任何密钥或完整 RPC URL。

## 数据状态

- 信号：`RECEIVED → BUY_PENDING → BUY_SUBMITTED → OPEN → SELL_PENDING → SELL_SUBMITTED → CLOSED`。
- 失败：买入终止为 `BUY_FAILED`；卖出有限重试后为 `POSITION_STUCK`，此前仓位保持 `OPEN`。
- 订单：`CREATED / SIGNED / SUBMITTED / CONFIRMED / REVERTED / FAILED / UNKNOWN`。

## 验证

测试按 auth/API、数据库幂等、V2/V4 池与 calldata、Permit2、nonce/receipt 恢复、FakeExecutionAdapter 完整闭环和 Docker 双服务分层。自动测试不得访问主网、真实钱包或真实资金；2026-09-11 另以官方公共 RPC 对已初始化 V4 池完成一次无签名、无广播的 Quoter 与 Universal Router `eth_call` 校验。
最近一次本地双服务交接的计数与耗时记录见 `docs/VALIDATION.md`。
