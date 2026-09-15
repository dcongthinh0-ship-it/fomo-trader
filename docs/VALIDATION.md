# 验证记录

日期：2026-09-10

## 自动化范围

- HMAC 原始请求体、篡改、时间戳、版本、chain id、地址、TTL、eligible 校验。
- event_id 与 BUY/SELL 唯一约束、旧状态持久化、live=false 不执行。
- 固定 40% 目标、低于目标不卖、FakeExecutionAdapter 买卖闭环、买卖失败状态。
- nonce 取链上 pending 与本地保留值的较大者及重启 reconcile。
- receipt 超时后保留 tx hash，重启只查原交易并恢复建仓，不创建第二笔 BUY。
- 广播前 RPC 失败复用同一 `CREATED` 订单，gas estimate 失败不消耗 nonce；买卖提交不确定和 approval 超时均按原 tx hash 恢复，不重复发送。
- 买卖确认后的订单/attempt/仓位/信号原子落库；卖出成功但 proceeds 为零时仓位转 `POSITION_STUCK`，不再卖第二次。
- 多仓位轮转报价，最早仓位未到止盈线不会饿死后续已达标仓位；健康接口报告 worker 心跳、最近异常和 stuck 数。
- V2/V3 池官方 Factory、资产与参数识别，V2/V3 直连/单桥路径；V4 pool id 不作为地址调用，V4 单桥候选按 StateView 活跃流动性限量筛选。
- 精确金额换算、非零最小输出、receipt Transfer 解析。
- 默认 live=false，以及随机测试密钥的地址匹配/不匹配启动校验；测试密钥不进入 Git。
- `0.0004 ETH` 单笔配置与最多 3 个活跃仓位限制；第 4 个信号当场永久跳过，关闭一仓后也不回买旧信号，只允许新信号买入；`POSITION_STUCK` 也占用名额。
- Gas 模拟请求将 `value/chainId/gasPrice` 编码为 JSON-RPC 十六进制 quantity，而签名交易继续使用整数并将 `to` 转为 EIP-55 checksum；防止官方 Go 节点返回 `-32602` 以及 `eth-account` 因全小写 Router 地址拒绝签名。
- V4 买入前并行模拟代币对 Permit2 的授权；明确回滚的代币以 `V4_TOKEN_PERMIT2_UNSUPPORTED` 在创建订单前拒绝，避免买入后无法使用 Universal Router 卖出，普通 RPC 临时失败仍保留重试语义。
- Uniswap 执行器启动时从链上 pending nonce 对账；本地签名异常也会立即对账，防止签名前失败把本地 nonce 永久抬高并造成后续交易卡在 nonce 空洞。
- public 模式将广播定向到官方 Sequencer、只读与 receipt 查询保留官方公共 RPC；交易哈希统一规范成 `0x` 前缀。广播未知的 BUY 过期后只有在 receipt 与 transaction 均不存在时才以 `BROADCAST_NOT_FOUND` 安全终止，不补买并重新对账 nonce。
- 交易使用 EIP-1559 type 2，默认 `maxFeePerGas = eth_gasPrice × 2` 且零 priority fee；gas 模拟请求中的 type/fee/value/chain id 均按 JSON-RPC quantity 编码。本地签名保留整数，最终 RPC 错误保留 code/message 供定位。
- 启动 nonce 对账遭遇临时 RPC 错误时，API 保持在线并标记 degraded，后台退避重试；对账成功前 worker 不运行，对账成功后才处理仍在有效期内的信号。

## 只读主网核验

官方 Robinhood RPC 上对 `docs/CONTRACTS.md` 的关键地址运行 `eth_getCode`，均为非空。本步骤只读，无钱包、签名或广播。

2026-09-15 对最近真实信号 `0xeea…af7` 的 V3 Token/WETH 池完成新增执行器核验：识别 fee 10000，QuoterV2 返回非零结果，带 deadline 的 SwapRouter02 `multicall → exactInput` 使用公开有余额地址做 `eth_call` 成功。解析、报价、模拟耗时约 2677/269/284 ms；没有私钥、签名、广播或余额变化。

同日对真实信号 `0x59ce…ece4` 的非直连 V4 池完成核验：链上存在 204 个原生 ETH/桥接币 PoolKey；代码通过 Multicall3 单次读取 StateView 活跃流动性，筛至 8 个候选后由 V4Quoter 选出可成交路径，生成的 Universal Router `SWAP_EXACT_IN → SETTLE_ALL → TAKE_ALL` 两池原子买入 calldata 经 `eth_call` 返回 `0x`。解析、报价、模拟约 5768/2278/495 ms；没有私钥、签名、广播或余额变化。

同日对 V2 WETH/USDG Pair `0x8803…1c4d` 完成正反向非零报价与原生 ETH 买入模拟，V2Router02 `eth_call` 返回 `0x`；解析、报价、模拟约 2606/526/491 ms。至此 V2、V3、V4 买入 calldata 均有真实链上只读成功样本。

截至 2026-09-15 00:38:12 北京时间，读取 monitor outbox 最近 20 条 eligible 信号（起点 2026-09-14 17:59:19），逐条用 ETH 模式完成解析、实时报价、买入构建和 Router `eth_call`。样本为 V3 14 条、V4 6 条，最终 20/20 成功。公共 RPC 的批量突刺故障通过保守档复核；Pons hook 的两条严格 ERC-20 样本在显式传入 feed 交易的公开非零发送地址作为 `eth_call.from` 后成功，证明此前 `ERC20InvalidReceiver(0x0)` 仅由省略模拟调用者造成。全程没有私钥、签名、广播或资金变化。

## 尚未执行

- Robinhood Chain fork/testnet 的真实签名买入与卖出闭环。
- Docker Secret 中真实专用小额钱包验证。
- 任何主网资金交易。

因此 `LIVE_TRADING_ENABLED` 必须保持 `false`；已实现的 V2/V3/V4 执行仍不得主网广播。

## 本地双服务交接

以独立临时 SQLite 启动 trader（`LIVE_TRADING_ENABLED=false`、Fake adapter），再由 monitor 的真实 `TradeSignalDispatcher` 发送合格事件：monitor 产生 1 条 decision 和 1 条 outbox，首次投送后状态为 `sent`；重复投送后 trader 仍只有 1 条 signal、0 条 BUY。市值 8500 的事件仍生成卡片、outbox 为 0。健康接口保持 `service=ok`、`open_positions=0`。

本机回环样本中，判断完成到 outbox 写入为 `0.284 ms`，outbox 发起到 trader 持久接收为 `2.258 ms`。这是一次本地功能样本，不是生产 SLA。飞书 notifier 与 dispatcher 在代码中是两个独立 asyncio task，各自失败测试已覆盖；本次未发送任何飞书生产消息。全程未调用主网 RPC 或广播交易。
