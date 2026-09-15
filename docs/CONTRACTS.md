# Robinhood Chain 合约核验清单

核验日期：2026-09-15。链 ID `4663`。官方主网 RPC 对下列地址执行 `eth_getCode(..., latest)` 均返回非空 bytecode；括号内为返回十六进制字符串长度。小额实盘已由用户明确授权；public 模式只读走公共 RPC，广播走官方 Sequencer，Alchemy 模式读写走用户所选 Alchemy URL。

主要来源：

- [Uniswap 官方 chain 4663 部署文件](https://github.com/Uniswap/contracts/blob/main/deployments/4663.md)
- [Robinhood Chain 官方代币合约](https://docs.robinhood.com/chain/contracts/)
- [Robinhood Chain 官方连接参数](https://docs.robinhood.com/chain/connecting/)

| 合约 | 地址 | 版本 | 部署交易/来源 | bytecode | 当前允许实盘 |
|---|---|---|---|---:|---|
| UniswapV2Factory | `0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f` | V2 | `0x2fc08b6c…6681edaf7` / Uniswap 部署文件 | 有（27720） | 否 |
| UniswapV2Router02 | `0x89e5db8b5aa49aa85ac63f691524311aeb649eba` | V2 | `0xd475f23d…ce866c` / Uniswap 部署文件 | 有（43806） | 否 |
| UniswapV3Factory | `0x1f7d7550b1b028f7571e69a784071f0205fd2efa` | V3 | `0x8add72fb…ff8977` / Uniswap 部署文件 | 有（49072） | 否 |
| SwapRouter02 | `0xcaf681a66d020601342297493863e78c959e5cb2` | V3 | `0xeaa1bf6b…cdf92` / Uniswap 部署文件 | 有（48996） | 否 |
| QuoterV2 | `0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7` | V3 | `0x62f59304…721b5` / Uniswap 部署文件 | 有（16548） | 否 |
| PoolManager | `0x8366a39cc670b4001a1121b8f6a443a643e40951` | V4 | `0x4fb28d49…c44c41` / Uniswap 部署文件 | 有（48020） | 否 |
| V4Quoter | `0x8dc178efb8111bb0973dd9d722ebeff267c98f94` | V4 | `0x6bf436d7…bc4ab4` / Uniswap 部署文件 | 有（长度已核验） | 否 |
| StateView | `0xf3334192d15450cdd385c8b70e03f9a6bd9e673b` | V4 | `0x3d61e2c9…0582f4` / Uniswap 部署文件 | 有（7064） | 否 |
| UniversalRouter | `0x8876789976decbfcbbbe364623c63652db8c0904` | 通用/V4 | `0x422569c9…9ed1fa` / Uniswap 部署文件 | 有（49094） | 否 |
| Permit2 | `0x000000000022d473030f116ddee9f6b43ac78ba3` | 通用 | Uniswap 部署文件（该文件未列创建交易） | 有（18306） | 否 |
| Multicall3 | `0xcA11bde05977b3631167028862bE2a173976CA11` | 只读批量查询 | 4663 链上代码核验 | 有（3808 bytes） | 不执行交易 |
| WETH | `0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73` | 链资产 | Robinhood 官方文档 + Uniswap 构造参数 | 有（4406） | 否 |
| USDG | `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168` | ERC-20 | Robinhood 官方文档 | 有（342） | 否 |

表中历史“当前允许实盘”列记录初始审查状态；实际运行仍由本机未入库的 `LIVE_TRADING_ENABLED` 控制。用户已在 2026-09-15 明确授权专用小额钱包主网交易，代码默认与示例配置继续保持 `false`。

## 代码支持状态

- V2：已实现 Factory/token0/token1/reserves 核对、买入资产直连或同一官方 Factory 单桥路由、精确输入报价、ETH/指定 ERC-20 买卖 calldata、非零 `amountOutMin`、精确额度 approval、本地签名、广播不确定性和 receipt 日志解析。仅待 fork 闭环后才可实盘。
- V3：已实现 Factory/token0/token1/fee/tickSpacing 识别、所有标准 fee tier 的同协议单桥发现、QuoterV2 精确输入多跳报价、SwapRouter02 原生 ETH 买入、ERC-20 approval、反向卖出与 WETH 解包；路径带非零 `amountOutMinimum` 和 deadline multicall。
- V4：从 PoolManager 对应 `Initialize` 日志自动恢复 PoolKey（也兼容信号直接携带），重新计算并核对 pool id；检查官方 PoolManager/StateView/V4Quoter/UniversalRouter bytecode。直连使用 `quoteExactInputSingle`；非直连用只读 Multicall3 批量读取 StateView 活跃流动性，筛选最多 8 个非零单桥后并发调用 `quoteExactInput`，选择最佳报价。Universal Router 使用 `SWAP_EXACT_IN_SINGLE` 或 `SWAP_EXACT_IN` 完成原子多池精确输入买卖，并支持原生 ETH 结算、ERC-20 → Permit2 → Universal Router 精确额度授权，以及买入 token/卖出 proceeds 的 receipt 解析。从不对 32 字节 pool id 发合约调用。

2026-09-11 使用官方公共 RPC 对已初始化的原生 ETH/USDG V4 池完成了只读验证：V4Quoter 返回非零报价，代码生成的 Universal Router calldata 经 `eth_call` 成功返回 `0x`。2026-09-15 又对监控真实命中的 V3 Token/WETH 池完成 QuoterV2 非零报价与 SwapRouter02 原生 ETH 买入 `eth_call`；池解析、报价和模拟分别约 2677 ms、269 ms 和 284 ms。同日对此前无法直连的真实 V4 信号 `0x59ce…ece4` 完成 204 个桥接 PoolKey 的批量流动性筛选、8 候选报价和两池原子买入模拟，Universal Router 返回 `0x`；解析、报价、模拟约 5768/2278/495 ms，后续低延迟阶段继续优化。所有验证均没有私钥、签名、广播或资金变化；所有版本仍受全局 live 开关和上表“当前允许实盘”边界约束。

2026-09-15 对 V2 WETH/USDG Pair `0x8803…1c4d` 完成 Factory、bytecode、非零 reserves、正反向 `getAmountsOut` 和原生 ETH 买入模拟；Router `eth_call` 返回 `0x`，解析、报价、模拟约 2606/526/491 ms。由此 V2、V3、V4 三种买入编码均已有真实链上只读成功样本。

2026-09-15 00:38 北京时间截取监控器最近 20 条 eligible 信号（决策时间 2026-09-14 17:59:19 至 2026-09-15 00:38:12）逐条执行池解析、ETH 报价、买入 calldata 和 Router `eth_call`：14 条 V3、6 条 V4，最终 20/20 成功。三条首次受公共 RPC 或零地址模拟调用者影响的 V4 样本，改用各自 feed 交易的公开非零发送地址作为只读 `from` 后均返回 `0x`；没有使用这些地址的私钥。该批没有 V2，V2 由上面的独立真实池样本覆盖。
