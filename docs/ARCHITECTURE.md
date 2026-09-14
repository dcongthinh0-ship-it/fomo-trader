# fomo-trader 架构

## 当前同步状态

- 日期：2026-09-15
- 状态：V2、V3 与 V4 直连/同协议单桥买卖执行齐备；V3、V4 已通过真实主网 Quoter 与无签名 `eth_call`，实盘默认关闭。

## 边界与数据流

`fomo-monitor` 只通过 `fomo-private` 私网发送已判断信号。API 先在原始字节上核验 HMAC、时间戳、版本、链、地址、TTL 和 `eligible=true`，再按 `event_id` 幂等写入 SQLite WAL。HTTP 接收只落库；交易 worker 与请求解耦。

```text
monitor outbox -> POST /v1/signals -> signals(SQLite) -> worker -> Uniswap adapter -> RPC
                                           |             |
                                           |             +-> orders / execution_attempts
                                           +----------------> positions
```

## 模块职责

- `settings.py`：环境/YAML、固定策略、public/Alchemy RPC 显式选择、每秒请求数/在途并发数调优与 live 凭据闭锁。
- `auth.py`、`models.py`、`signals.py`、`api.py`：通信认证、验证、幂等接收和健康接口。
- `db.py`：signals/orders/positions/execution_attempts/nonce_state 持久化；签名、广播、approval 与 receipt 事实分步留痕。
- `execution.py`、`worker.py`：适配器协议和可恢复买卖状态机。
- `rpc.py`、`nonce.py`：隔离 RPC、并发安全的请求起始速率限制、仅瞬时故障有限重试、确定性 JSON-RPC 拒绝快速失败、nonce 协调与恢复。
- `pools.py`、`uniswap.py`：官方部署核验、池识别、V2/V3 直接与同协议单桥、V3 QuoterV2 + SwapRouter02，以及 V4 PoolKey 恢复、StateView/Multicall3 活跃流动性筛选、V4Quoter 多池报价、Universal Router 多池买卖、Permit2、签名与 receipt 解析。
- `orders.py`、`positions.py`：唯一订单和 30% 全仓止盈领域写入。

## 不可破坏约束

1. `LIVE_TRADING_ENABLED=false` 是默认值；未授权不得广播。
2. 同一 event 最多一个 BUY 与一个 SELL；签名前即保存唯一订单，签名后、广播前先持久化 tx hash/nonce；超时或重启只按 receipt 恢复，并可重新核验池上下文解析卖出结果，不能重买。
3. 目标固定为 `actual_cost × 1.30`，gas 不计入成本；只卖 100%，不含止损或其他策略。
4. 市值与流动性只来自信号且不在本服务重查；链上池/路由核验不是新入场条件。
5. V4 pool id 是 32 字节标识，绝不能当合约地址调用；PoolKey 必须来自 PoolManager 的对应 `Initialize` 日志或信号字段，并重新计算 pool id 核对。
6. 私钥和共享密钥只从只读文件读取，且被 Git/Docker build context 排除；Compose 未配置私钥时挂载 `/dev/null`，live 启动必然失败；健康接口与日志不泄露任何密钥或完整 RPC URL。
7. 交易 RPC 在进程启动时通过 `ROBINHOOD_TRADING_RPC_PROVIDER=public|alchemy` 选择；两种端点复用同一 RPC 客户端，不做静默自动切换。
8. V4 同币对可能存在大量 PoolKey；解析器以只读 Multicall3 一次读取 StateView 活跃流动性，只保留最高的 8 个非零候选并并发报价，交易仍只提交给官方 Universal Router。
9. 单个进程内缓存不可变的 V4 PoolKey、币对候选和已核验合约代码；独立的池字段/标准 V3 fee tier 查询并发发起，缓存不替代每次交易的实时 Quoter 报价。
10. RPC 同时限制每秒请求起始数和在途请求数；public 默认 2 个在途，Alchemy 默认 8 个，可用 `ROBINHOOD_TRADING_RPC_MAX_IN_FLIGHT` 显式覆盖，避免公共节点因并发突刺拒绝整条路由。
11. gas price 与 estimate 成功后才保留 nonce；链上已成功的买卖以一个 SQLite 事务同时确认订单、写 execution attempt、开/关仓和推进信号状态，避免重启时出现半完成状态。
12. `CREATED` 且没有 tx hash 的订单属于可安全重试的广播前状态；瞬时 RPC 故障会回到 `RECEIVED/OPEN`。链上卖出成功但 proceeds 无法解析时直接标记 `POSITION_STUCK`，禁止对已经卖出的仓位再次发送卖单。
13. ERC-20/Permit2 approval 使用独立的 `APPROVAL` execution attempt；提交不确定或 receipt 超时只按原 tx hash 恢复，在确认或回滚前不得发送第二笔 approval，也不得把 approval 的 tx hash 写入 BUY/SELL 订单。
14. worker 每 5 秒写入一次心跳并保存最近异常类型/时间；心跳超过 15 秒或存在 `POSITION_STUCK` 时 `/health` 返回 `service=degraded`，但不暴露钱包、RPC URL 或密钥。

## 数据状态

- 信号：`RECEIVED → BUY_PENDING → BUY_SUBMITTED → OPEN → SELL_PENDING → SELL_SUBMITTED → CLOSED`。
- 失败：买入终止为 `BUY_FAILED`；卖出有限重试后为 `POSITION_STUCK`，此前仓位保持 `OPEN`。
- 订单：`CREATED / SIGNED / SUBMITTED / CONFIRMED / REVERTED / FAILED / UNKNOWN`。

## 验证

测试按 auth/API、数据库幂等、V2/V3/V4 直连与同协议单桥池、V3/V4 多跳 calldata、Permit2、nonce/receipt 恢复、FakeExecutionAdapter 完整闭环和 Docker 双服务分层。自动测试不得访问主网、真实钱包或真实资金；已使用官方公共 RPC 对真实 V3 和 V4 池完成无签名、无广播的 Quoter 与 Router `eth_call` 校验。
最近一次本地双服务交接的计数与耗时记录见 `docs/VALIDATION.md`。
