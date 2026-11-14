# AkkiaBot 仓库约定（面向智能体与贡献者）

详细说明以 `README.md`、`docs/architecture.md`、`docs/migration-plan.md` 为准；这里只列容易踩错、需要统一遵守的规则。

## 密钥与配置

- 测试环境密钥统一放在 1Password 的 `AkkiaBot-Staging` vault；该 vault 的服务账号可读写（负责人 2026-11-14 决定），工具会自动写回会话、测试 bot、群 id。测试密钥可在 Multica 内部共享，但仓库是公开的：仓库、PR、CI 日志只出现 `op://` 引用（`tools/e2e/*.env.tpl`）与 `.env.example`。字段清单见 `docs/e2e-acceptance.md` 第 3 节。
- 不得把 token、DSN、Telegram 会话、验证码、两步验证密码写进仓库、PR、日志或命令行参数；写入 1Password 时值经 stdin 传递。交接时说明 1Password 中的位置即可。
- 日志：调用方在记录前显式改写已知密钥，日志模块不做自动识别或递归脱敏；配置使用 `Settings.redacted()` 等安全形式。会在 URL 或调试日志中带出凭据的第三方库（`httpx`、`httpcore`、PTB 的 Bot 日志、Telethon），在代码中把它们的日志级别固定，不随根日志级别放开。

## 真实 Telegram 验收

- 一律使用 Telegram 测试环境（MTProto 测试 DC、Bot API `/test/` 端点、测试环境 BotFather），无法使用时报错阻塞，不回退生产；产品侧开关为 `TELEGRAM_TEST_ENVIRONMENT`。
- 涉及真实 Telegram 行为的验收标准（M2 起），用 `just e2e` 执行，方法见 `docs/e2e-acceptance.md`；把报告 `report.md` 贴回对应 issue。缺少凭据时工具以退出码 77 跳过，不能当作通过。
- 只使用白名单中的两个测试账号和专用测试群；不删除群、不清理历史（需要时另行确认）。同一时间只在一台机器上运行验收。
- Mini App 真实客户端和正式 Turnstile 成功路径不能用用户账号自动化或测试站点 key 代替验收。
- 新功能需要真实验收时，在 `tools/e2e/src/akkiabot_e2e/scenarios.py` 增加场景，并用 `akkiabot_e2e.testing` 的模拟 transport 写单元测试；普通 CI 不发送真实消息。
