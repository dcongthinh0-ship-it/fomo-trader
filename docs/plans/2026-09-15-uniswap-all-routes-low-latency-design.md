# Uniswap V2/V3/V4 低延迟执行设计

## 目标与边界

第一目标是让监控信号指向的官方 Uniswap V2、V3、V4 池都进入可执行买卖链路；第二目标是压缩从信号入库到交易构造完成的延迟。交易策略仍只保留固定金额买入、实际到账建仓和 30% 全仓止盈，不增加止损、K 线、评分或额外入场条件。所有实现和验收保持 `LIVE_TRADING_ENABLED=false`，不使用真实私钥、不广播主网交易。

买入资产默认改为原生 ETH。原因是近期 20 条真实合格信号中，15 条是 Token/WETH V3，5 条是 V4；ETH 可直接覆盖全部 15 条 V3 和 3 条原生 ETH V4，同时避免买入前 ERC-20 approval。另 2 条 V4 使用中间报价资产，必须通过官方 V4 池完成 ETH → 报价资产 → 目标币的单笔多跳。USDG 模式继续保留，但不作为低延迟默认值。

## 路由架构

- V2：保留 Router02 精确输入，支持 WETH 直连和同一官方 V2 Factory 的单桥路径。
- V3：使用官方 QuoterV2 报价和 SwapRouter02 `exactInput`；支持 Token/WETH 单池，并为非直连池从官方 Factory 的候选 fee tier 中寻找单桥路径。ETH 买入由 Router 在交易内包装为 WETH；卖出通过 multicall 将 WETH 解包后直接发送 ETH 给钱包。
- V4：保留 `SWAP_EXACT_IN_SINGLE`；目标池不含原生 ETH 时，根据 PoolManager `Initialize` 索引发现原生 ETH 与目标报价资产的官方桥池，使用 `SWAP_EXACT_IN` 在同一笔 Universal Router 交易中完成两跳。所有 PoolKey 和 pool id 重新计算核对，hooks 和动态费池参数原样传递。
- 不在不同协议间拆成两笔交易。找不到同协议单桥时明确失败，不持有中间资产，也不把第三方聚合器作为隐式执行依赖。

## 延迟设计

交易 API 在接受信号后唤醒 worker，避免空闲轮询最多 1 秒的等待。固定官方合约 bytecode 校验做进程内缓存；已解析 PoolKey 和池上下文做缓存，卖出和重启恢复不重复全量发现。独立 RPC provider 仍由 `public|alchemy` 显式切换：公共 RPC 保守限速，Alchemy 可通过环境变量提高并发，不做静默故障转移。

池解析中相互独立的链上读取并行执行；V3 bridge fee 候选并行报价后选择输出最高且链上验证通过的路径。交易构造只保留必要的报价、nonce、gas price 和 estimate 调用。每阶段记录毫秒耗时与稳定错误码，但不记录 RPC URL、钱包秘密或原始签名交易。

## 状态与恢复

任何签名交易（BUY、SELL、ERC20 approval、Permit2 approval）都必须先以独立 operation 身份保存 tx hash 和 nonce，再广播。`CREATED` 但尚未签名的订单允许安全重建；有 tx hash 的订单只能按 receipt/nonce 恢复，禁止盲目重发。成交确认、订单、仓位和信号状态用单一数据库事务完成，避免进程在中间退出后永久卡住。

卖出扫描不再由最早未达标仓位阻塞其他仓位；一轮检查所有到期候选。实际到账必须大于零才能关闭仓位。nonce 在 gas 构造失败且没有签名交易时允许回收或与链上 pending 状态协调，避免本地预留造成 nonce 空洞。

## 验收标准

1. V2、V3、V4 直连买入与反向全仓卖出均有 calldata、报价、approval、receipt 解析测试。
2. V3、V4 单桥路径均有成功与 fail-closed 测试；最近 20 条真实信号在 ETH 模式下全部至少能完成池解析和非零报价。
3. transient RPC、approval timeout、签名后崩溃、receipt 后崩溃、nonce 空洞和多仓位卖出均有回归测试。
4. Ruff、pytest、Docker build 全部通过；官方公共 RPC 上完成无签名、无广播的真实 Quoter 和 `eth_call` 验证，并记录分阶段延迟。
5. 验收结束后仍保持 live=false；只有用户随后提供公开钱包地址、在本机安全放置专用小额钱包私钥并再次明确授权，才进行主网极小额闭环。
