# AkkiaBot 仓库约定（面向智能体与贡献者）

详细说明以 `README.md`、`docs/architecture.md`、`docs/migration-plan.md` 为准；这里只列容易踩错、需要统一遵守的规则。

## 密钥与配置

- 密钥统一放在 1Password 的 `AkkiaBot` vault，条目按 `<环境>-<用途>` 命名（`e2e-*`、`staging-*` 供 dev/staging 共用、`prod-*`）；dev/staging 共用一个 Cloudflare token，prod 用另一个（负责人 2026-11-14 决定）。服务账号可读写，工具会自动写回会话、测试 bot、群 id。测试密钥可在 Multica 内部共享，但仓库是公开的：仓库、PR、CI 日志只出现 `op://` 引用（`tools/e2e/*.env.tpl`）与 `.env.example`。字段清单见 `docs/e2e-acceptance.md` 第 3 节。
- 不得把 token、DSN、Telegram 会话、验证码、两步验证密码写进仓库、PR、日志或命令行参数；写入 1Password 时值经 stdin 传递。交接时说明 1Password 中的位置即可。
- 测试与文档中的凭据样例：一律使用一看就是假的、且**不符合真实凭据格式**的占位值，例如 bot token 用 `123456:fake-bot-token-for-tests`（真实格式为 8–10 位 bot id + `:AA` 开头的 35 位串），其他密钥用 `fake-api-hash-for-tests` 这类低熵字符串。不要照抄官方文档的示例 token，不要手写"看起来真实"的随机串，也不要用字符串拼接（如 `"123456:" + "A" * 35`）凑出真实格式或绕过扫描。需要能被解析器识别的 token（如 BotFather 回复解析）时复用 `akkiabot_e2e.testing.MOCK_BOT_TOKEN`。
- 凭据形样例必须就地声明为 mock（负责人于 BOT-21 要求）：赋给 `MOCK_`（局部变量用 `mock_`）开头的名称，并在同一行或上一行写 `# mock: ...` 注释，说明是人工构造、不可用的测试值；不要在调用参数、字典、f-string 里直接写字面量。`tests/test_secret_samples.py` 会拦截已跟踪文件中的真实 Telegram bot token 格式，以及未按此声明的凭据形字面量（`<数字>:<字母…>`、`ghs_`/`ops_` 等前缀、Sentry DSN、api_hash、webhook secret、Telethon 会话串），失败时只报告位置。
- 凭据扫描门禁见 `docs/secret-scanning.md`：pre-commit 扫暂存区，pre-push 扫所有未推送提交（`just setup` 会同时安装两类钩子），CI 的固定检查 `secret-scan` 扫全部提交，`just secret-scan` 可在本地复现。命中后只报告规则、文件、行号；不要用 `--no-verify`、拼接或宽泛 allowlist 绕过，未知告警不得直接写入 `.gitleaksignore`。
- 安全告警不能只凭来源记录判定为构造值，还要用服务方无副作用的只读接口实际验证（如 Telegram `getMe`），来源证据与验证结果分开记录；凭据只在进程内读取，不进命令参数、日志、含值 URL 或报告。网络失败、限流或本地格式拒绝记为"未验证"；若意外有效，停止调用并请负责人吊销/轮换。
- 日志：调用方在记录前显式改写已知密钥，日志模块不做自动识别或递归脱敏；配置使用 `Settings.redacted()` 等安全形式。会在 URL 或调试日志中带出凭据的第三方库（`httpx`、`httpcore`、PTB 的 Bot 日志、Telethon），在代码中把它们的日志级别固定，不随根日志级别放开。

## 真实 Telegram 验收

- 一律使用 Telegram 测试环境（MTProto 测试 DC、Bot API `/test/` 端点、测试环境 BotFather），无法使用时报错阻塞，不回退生产；产品侧开关为 `TELEGRAM_TEST_ENVIRONMENT`。
- 分层验收（负责人于 2026-11-14 确认，取代“每个需求都完整真实验收”和“staging 客户端验收必须人工完成”的旧要求）：
  - **单个需求 / PR**：只验证改动**明确涉及**的场景（规则适用于所有场景，负责人于 BOT-28 确认）。用 `just e2e --scenarios <场景>`（本机 `--serve`，只复用固定测试群、不新建群），把报告 `report.md` 贴回对应 issue。
    - 无法判断是否受影响时默认跳过；共享配置、handler 注册、依赖升级等也按不受影响处理，除非确实改到了相关场景的代码。staging 发版同样只验收差异，间接影响不会被自动补跑。
    - PR 描述列出“已验证的场景”和“未验证、留给 staging”的部分。报告中未选中的场景记为跳过，不计为通过。
    - 选择规则见 `docs/e2e-acceptance.md` §6“PR 级验收的场景选择”，入群限流见 §7。两次验收之间不再要求固定间隔（负责人于 BOT-16 取消“全量至少间隔约 1 小时”）；失败后先查清原因再重跑，不盲目连续重试，FloodWait、`PEER_FLOOD` 如实报错。
  - **staging 发版**：可以累积多个已合并的任务，再由智能体对已部署的 staging 栈执行**差异**自动化验收（负责人于 BOT-16 决定）：`just e2e-staging --record-status <master sha> --baseline <上一个通过 staging-acceptance 的 sha> --scenarios <场景>`，只跑基线以来产品改动明确涉及的场景，选择规则与 PR 级相同；没改加群流程就不跑入群相关场景，无法判断时默认跳过并如实列出。选中的场景全部实际执行并通过才写入 commit status `staging-acceptance=success`（跳过不计为通过），并在发版记录中写明版本、基线、场景和结果。需要时仍可不带 `--scenarios`/`--baseline` 执行全量。细则见 `docs/e2e-acceptance.md` §6.2。
  - **prod 发布**：只提升已通过 staging 验收的 sha（前端用 `webapp-promote.yml`）。只做环境配置检查、健康检查和必要冒烟，不重复全量验收。生产发布、切换和数据迁移仍需负责人确认；智能体不审批 `webapp-production` 部署。
- 缺少凭据时，工具以退出码 77 跳过，不能当作通过。自动化做不到的步骤要如实列出（报告中的“未自动化”），不能用 mock、测试 key 或绕过检测的手段冒充通过。目前唯一的已知项是 Mini App 中真实 Turnstile 挑战的完成与随后的解除禁言，`m7.captcha` 只自动化到“提交未通过 Turnstile 的 token 被拒绝”。本地 `just e2e-turnstile-test-keys`（`m7.captcha-test-keys`）用 Cloudflare 官方测试 key 跑通通过/拒绝链路，报告单独标为“官方测试 key”，属于另一类证据，不能替代 staging 真实 widget 的证据（`docs/e2e-acceptance.md` §6.4）。
- 只使用白名单中的两个测试账号和专用测试群（staging 发版验收使用单独的“AkkiaBot Staging 发版验收群”）；不删除群、不清理历史（需要时另行确认）。同一时间只在一台机器上运行验收。
- 新功能需要真实验收时，在 `tools/e2e/src/akkiabot_e2e/scenarios.py` 增加场景，并用 `akkiabot_e2e.testing` 的模拟 transport 写单元测试；普通 CI 不发送真实消息。
