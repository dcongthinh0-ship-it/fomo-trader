# 项目协作规则

- 开始修改前先读 `docs/ARCHITECTURE.md`；合约或实盘相关改动还要读 `docs/CONTRACTS.md`。
- 每次代码、测试、配置或部署变更必须在同一提交同步更新 `docs/ARCHITECTURE.md`。
- 修改后运行匹配的测试和 ruff；每个逻辑阶段独立提交并立即推送。
- 永不提交 `.env`、私钥、共享密钥、RPC Key、数据库、日志或运行数据。
- `LIVE_TRADING_ENABLED` 默认且模板中必须为 `false`。没有独立明确授权，不得广播主网交易。
- 不得把超时当失败后盲目重发；必须先按 tx hash、nonce、receipt 恢复。
- 不使用 GMGN、中心化交易所或未核验第三方代下单服务。
