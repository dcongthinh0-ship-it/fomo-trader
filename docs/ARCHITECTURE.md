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

- `settings.py`：环境/YAML、固定策略、最多 3 个活跃仓位、public/Alchemy RPC 显式选择、每秒请求数/在途并发数调优与 live 凭据闭锁。
- `auth.py`、`models.py`、`signals.py`、`api.py`：通信认证、验证、幂等接收和健康接口。
- `db.py`：signals/orders/positions/execution_attempts/nonce_state 持久化；签名、广播、approval 与 receipt 事实分步留痕。
- `execution.py`、`worker.py`：适配器协议和可恢复买卖状态机。
- `rpc.py`、`nonce.py`：隔离 RPC、并发安全的请求起始速率限制、仅瞬时故障有限重试、确定性 JSON-RPC 拒绝快速失败、nonce 协调与恢复。
- `pools.py`、`uniswap.py`：官方部署核验、池识别、V2/V3 直接与同协议单桥、V3 QuoterV2 + SwapRouter02，以及 V4 PoolKey 恢复、StateView/Multicall3 活跃流动性筛选、V4Quoter 多池报价、Universal Router 多池买卖、Permit2、签名与 receipt 解析。
- `orders.py`、`positions.py`：唯一订单和 40% 全仓止盈领域写入。

## 不可破坏约束

1. `LIVE_TRADING_ENABLED=false` 是默认值；未授权不得广播。
2. 同一 event 最多一个 BUY 与一个 SELL；签名前即保存唯一订单，签名后、广播前先持久化 tx hash/nonce；超时或重启只按 receipt 恢复，并可重新核验池上下文解析卖出结果，不能重买。
3. 目标固定为 `actual_cost × 1.40`，gas 不计入成本；只卖 100%，不含止损或其他策略。
4. 市值与流动性只来自信号且不在本服务重查；链上池/路由核验不是新入场条件。
5. V4 pool id 是 32 字节标识，绝不能当合约地址调用；PoolKey 必须来自 PoolManager 的对应 `Initialize` 日志或信号字段，并重新计算 pool id 核对。
6. 私钥和共享密钥只从只读文件读取，且被 Git/Docker build context 排除；Compose 未配置私钥时挂载 `/dev/null`，live 启动必然失败；健康接口与日志不泄露任何密钥或完整 RPC URL。
7. 交易 RPC 在进程启动时通过 `ROBINHOOD_TRADING_RPC_PROVIDER=public|alchemy` 选择；两种端点复用同一 RPC 客户端，不做静默自动切换。
8. V4 同币对可能存在大量 PoolKey；解析器以只读 Multicall3 一次读取 StateView 活跃流动性，只保留最高的 8 个非零候选并并发报价，交易仍只提交给官方 Universal Router。
9. 单个进程内缓存不可变的 V4 PoolKey、币对候选和已核验合约代码；独立的池字段/标准 V3 fee tier 查询并发发起，缓存不替代每次交易的实时 Quoter 报价。
10. RPC 同时限制每秒请求起始数和在途请求数；public 默认 `2 req/s + 1` 个在途，Alchemy 默认 `20 req/s + 8` 个在途，并有各自独立环境变量。旧的 `ROBINHOOD_TRADING_RPC_*` 仍可统一覆盖，避免公共节点因并发突刺拒绝整条路由，同时让 Alchemy 保持低延迟。
11. gas price 与 estimate 成功后才保留 nonce；链上已成功的买卖以一个 SQLite 事务同时确认订单、写 execution attempt、开/关仓和推进信号状态，避免重启时出现半完成状态。
12. `CREATED` 且没有 tx hash 的订单属于可安全重试的广播前状态；瞬时 RPC 故障会回到 `RECEIVED/OPEN`。链上卖出成功但 proceeds 无法解析时直接标记 `POSITION_STUCK`，禁止对已经卖出的仓位再次发送卖单。
13. ERC-20/Permit2 approval 使用独立的 `APPROVAL` execution attempt；提交不确定或 receipt 超时只按原 tx hash 恢复，在确认或回滚前不得发送第二笔 approval，也不得把 approval 的 tx hash 写入 BUY/SELL 订单。
14. worker 每 5 秒写入一次心跳并保存最近异常类型/时间；心跳超过 15 秒或存在 `POSITION_STUCK` 时 `/health` 返回 `service=degraded`，但不暴露钱包、RPC URL 或密钥。
15. V4 只读 Router 模拟必须显式提供非零公开 `from` 地址；`TAKE_ALL` 把 `msgSender()` 作为收款人，省略 `from` 会让严格 ERC-20 以 `ERC20InvalidReceiver(0x0)` 回滚，这不是实际签名交易的路由失败。
16. 当前买入金额为 `0.0004 ETH`（配置时约 1 美元）；最多同时存在 3 个非 `CLOSED` 仓位，`POSITION_STUCK` 继续占用名额。达到上限时新信号立即终止为 `SKIPPED/MAX_OPEN_POSITIONS`，不排队且以后不得回买；worker 同一轮仍继续检查卖出，释放名额后只允许新到达的有效信号买入。
17. `eth_estimateGas` 的 `value/chainId/gasPrice` 必须按 JSON-RPC quantity 编码为 `0x...` 字符串；本地签名交易仍保存整数，且 `to` 统一转换为 EIP-55 checksum 地址，避免 Robinhood Go 节点以 `-32602` 拒绝模拟或 `eth-account` 以 `TypeError` 拒绝签名 V2/V3/V4 共用的构建路径。
18. V4 买入报价与代币对 Permit2 的只读授权模拟并行执行；若代币明确拒绝 Permit2，则以 `V4_TOKEN_PERMIT2_UNSUPPORTED` 在创建订单前失败关闭，避免买入后无法通过官方 Universal Router 卖出。RPC 暂时不可用仍按瞬时错误重试，不误判为代币不兼容。
19. Uniswap 执行器启动时必须用链上 pending nonce 覆盖本地缓存；若本地签名失败，必须立即再次对账。这样 gas 模拟后、广播前的失败不会遗留 nonce 空洞，未知广播仍由订单 tx hash 恢复流程防止重复发送。
20. public 模式的只读查询走官方公共 RPC，`eth_sendRawTransaction` 单独走官方 Sequencer；Alchemy 模式读写均走所选 Alchemy URL。所有本地交易哈希统一保存为 `0x` 加 64 位十六进制，receipt/transaction 查询也会兼容修复历史无前缀值。BUY 广播结果未知时只查询原哈希；信号过期且 receipt 与 transaction 都不存在才终止为 `EXPIRED/BROADCAST_NOT_FOUND` 并对账 nonce，绝不重新买入旧信号。
21. 主网交易统一签为 EIP-1559 type 2；`maxFeePerGas` 默认取临近广播时 `eth_gasPrice × 2`，`maxPriorityFeePerGas=0`，使费用上限覆盖 Nitro 单区块最多约 2 倍的 base fee 上涨，同时实际支付仍由当块 base fee 决定。倍率只能配置在 1～10；最终 JSON-RPC 错误响应必须保留原始 code/message，不能降级成无原因的 `UNAVAILABLE`。广播异常事实写入 execution attempt 前会替换长十六进制载荷并截断，避免保存或暴露原始签名交易。

## 数据状态

- 信号：`RECEIVED → BUY_PENDING → BUY_SUBMITTED → OPEN → SELL_PENDING → SELL_SUBMITTED → CLOSED`。
- 失败：买入终止为 `BUY_FAILED`；卖出有限重试后为 `POSITION_STUCK`，此前仓位保持 `OPEN`。
- 放弃：信号到达时持仓已满则终止为 `SKIPPED`，原因是 `MAX_OPEN_POSITIONS`，永不回队。
- 订单：`CREATED / SIGNED / SUBMITTED / CONFIRMED / REVERTED / FAILED / UNKNOWN`。

## 验证

测试按 auth/API、数据库幂等、V2/V3/V4 直连与同协议单桥池、V3/V4 多跳 calldata、Permit2、nonce/receipt 恢复、FakeExecutionAdapter 完整闭环和 Docker 双服务分层。自动测试不得访问主网、真实钱包或真实资金；已使用官方公共 RPC 对真实 V3 和 V4 池完成无签名、无广播的 Quoter 与 Router `eth_call` 校验。
真实只读验证还包括 V2 WETH/USDG 池的正反报价及原生 ETH 买入 Router `eth_call`；最近一次本地双服务交接的计数与耗时记录见 `docs/VALIDATION.md`。
