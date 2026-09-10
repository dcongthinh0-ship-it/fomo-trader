# Robinhood Chain 合约核验清单

核验日期：2026-09-10。链 ID `4663`。官方主网 RPC 对下列地址执行 `eth_getCode(..., latest)` 均返回非空 bytecode；括号内为返回十六进制字符串长度。官方公共 RPC 仅用于本次只读交叉核验，不用于生产广播。

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
| WETH | `0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73` | 链资产 | Robinhood 官方文档 + Uniswap 构造参数 | 有（4406） | 否 |
| USDG | `0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168` | ERC-20 | Robinhood 官方文档 | 有（342） | 否 |

“当前允许实盘”全部为否，不代表地址未核验，而是尚未完成本地 fork/testnet 的完整买入、授权、卖出、nonce 重启恢复与 receipt 解析验收，也未获得用户对 Robinhood Chain 主网小额闭环的独立明确授权。

## 代码支持状态

- V2：已实现 Factory/token0/token1/reserves 核对、买入资产直连或同一官方 Factory 单桥路由、精确输入报价、ETH/指定 ERC-20 买卖 calldata、非零 `amountOutMin`、精确额度 approval、本地签名、广播不确定性和 receipt 日志解析。仅待 fork 闭环后才可实盘。
- V3：已实现 Factory/token0/token1/fee/tickSpacing 识别；执行 fail-closed，错误码 `V3_EXECUTION_NOT_FORK_VALIDATED`。
- V4：PoolKey 五字段与 pool id 哈希核对，且只检查官方 PoolManager/StateView/UniversalRouter；从不对 32 字节 pool id 发合约调用。执行 fail-closed，错误码 `V4_EXECUTION_NOT_FORK_VALIDATED`。

这里的 fail-closed 是刻意的安全边界：不能把尚未经过 Robinhood Chain fork 的 V3/V4 calldata 当作可用实盘能力。
