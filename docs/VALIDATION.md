# 验证记录

日期：2026-09-10

## 自动化范围

- HMAC 原始请求体、篡改、时间戳、版本、chain id、地址、TTL、eligible 校验。
- event_id 与 BUY/SELL 唯一约束、旧状态持久化、live=false 不执行。
- 固定 30% 目标、低于目标不卖、FakeExecutionAdapter 买卖闭环、买卖失败状态。
- nonce 取链上 pending 与本地保留值的较大者及重启 reconcile。
- receipt 超时后保留 tx hash，重启只查原交易并恢复建仓，不创建第二笔 BUY。
- V2/V3 池官方 Factory、资产与参数识别，V2 直连/单桥路径；V4 pool id 不作为地址调用。
- 精确金额换算、非零最小输出、receipt Transfer 解析。
- 默认 live=false，以及随机测试密钥的地址匹配/不匹配启动校验；测试密钥不进入 Git。

## 只读主网核验

官方 Robinhood RPC 上对 `docs/CONTRACTS.md` 的关键地址运行 `eth_getCode`，均为非空。本步骤只读，无钱包、签名或广播。

## 尚未执行

- Robinhood Chain fork/testnet 的真实 Uniswap 买入与卖出闭环。
- Docker Secret 中真实专用小额钱包验证。
- 任何主网资金交易。

因此 `LIVE_TRADING_ENABLED` 必须保持 `false`，V3/V4 执行继续 fail-closed。

## 本地双服务交接

以独立临时 SQLite 启动 trader（`LIVE_TRADING_ENABLED=false`、Fake adapter），再由 monitor 的真实 `TradeSignalDispatcher` 发送合格事件：monitor 产生 1 条 decision 和 1 条 outbox，首次投送后状态为 `sent`；重复投送后 trader 仍只有 1 条 signal、0 条 BUY。健康接口保持 `service=ok`、`open_positions=0`。全程未启动飞书发送，也未调用主网 RPC 或广播交易。
