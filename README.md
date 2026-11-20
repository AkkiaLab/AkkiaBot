# AkkiaBot

AkkiaBot 是一个 Telegram 群管理机器人，提供入群人机验证（Cloudflare Turnstile + Telegram Mini App）、基于 LLM 的反垃圾、封禁 / 警告 / 举报、群功能开关、IP whois 查询等功能。

本仓库是 botlab 系列项目（botlab-core、botlab-llm、botlab-fe、botlab-telegram、Toolbox-py）的 monorepo 重写版本，正在逐模块迁移中。

- 架构与技术栈：[docs/architecture.md](docs/architecture.md)
- 迁移计划：[docs/migration-plan.md](docs/migration-plan.md)
- 真实 Telegram 验收（Telegram 测试环境 + 1Password + 测试用户账号）：[docs/e2e-acceptance.md](docs/e2e-acceptance.md)
- 部署与运维（生产 / 预发布 compose、配置项、升级回滚、备份、DNS）：[docs/operations.md](docs/operations.md)
- 可观测性（Grafana Alloy、dashboard、告警、本地 Grafana）：[docs/observability.md](docs/observability.md)
- 仓库约定（密钥、日志、验收）：[AGENTS.md](AGENTS.md)

## 开发

### 目录结构

```text
apps/server        后端服务（Python 包 akkiabot_server，入口命令 akkiabot）
apps/webapp        Telegram Mini App（Vite + React + TypeScript + Tailwind）
packages/core      akkiabot_core：配置、日志、数据库、缓存、领域模型
packages/antispam  akkiabot_antispam：LLM 反垃圾分类器（纯库）
packages/whois     akkiabot_whois：IP 查询 provider 链（纯库）
tools/e2e          akkiabot_e2e：真实 Telegram 验收工具（Telethon 用户账号，不进镜像）
deploy/            Dockerfile 与 compose 文件
scripts/           仓库脚本（如 PR 标题检查）
tests/             仓库级测试（import-linter 反例、脚本测试）
```

### 环境要求

- [uv](https://docs.astral.sh/uv/)（会按 `.python-version` 自动准备 Python 3.13）
- Node.js 22+ 与 pnpm 10（`corepack enable` 后按 `package.json` 的 `packageManager` 自动选择版本）
- [just](https://github.com/casey/just)
- Docker（本地数据库与镜像构建，可选）

### 常用命令

```bash
just setup       # 安装 Python / 前端依赖，并安装 pre-commit 钩子
just lint        # ruff + import-linter + biome
just fmt         # 自动格式化并修复可安全修复的问题
just typecheck   # pyright + tsc
just test        # pytest + vitest
just dev         # 启动本地 postgres / redis（deploy/compose.dev.yaml）和 Mini App 开发服务器
just dev-down    # 停止本地服务
just services    # 只启动本地 postgres / redis
just migrate     # 对 DB_URL 执行数据库迁移（等同 `akkiabot migrate`）
just serve       # 启动机器人与 HTTP API（等同 `akkiabot serve`，需先 migrate）
just db-revision "说明"  # 修改模型后自动生成新的 Alembic 迁移
just test-integration  # 连同 compose 中的 PostgreSQL / Redis 一起跑 pytest
just gen-api     # 导出后端 OpenAPI（apps/webapp/openapi.json）并生成前端类型（src/api/schema.d.ts），两者都要提交
just docker-build  # 构建后端镜像并运行，打印版本号
just e2e         # 真实 Telegram 验收（需 1Password，见 docs/e2e-acceptance.md；CI 不运行）
just antispam-eval  # 用离线评测集评测反垃圾提示词（调用真实 LLM，读取 LLM_* / ANTISPAM_*）
```

只改一侧时，可以用 `just lint-py` / `just lint-web`、`just test-py` / `just test-web` 等子命令；`just test-py -k smoke` 会把参数透传给 pytest。

本地配置：复制 `.env.example` 为 `.env` 后填写，`.env` 不入库。变量名为 `<分组>_<字段>`（如 `TELEGRAM_BOT_TOKEN`、`DB_URL`、`OBSERVABILITY_SENTRY_DSN`），完整定义见 `packages/core/src/akkiabot_core/config.py`；必填项（`TELEGRAM_BOT_TOKEN`、`DB_URL`）缺失时启动即报错。

日志模块原样输出消息、extra 字段和异常，不做自动脱敏；记录配置或连接信息时请使用 `Settings.redacted()`、`DatabaseSettings.safe_url` 等安全形式，不要把 token、密码等已知密钥直接写进日志。Bot API 的 URL 中含有 bot token，`Runtime.build()` 因此把 `httpx`、`httpcore`（以及 openai SDK 使用的 `httpx2`、`httpcore2`）日志固定在 WARNING，把 PTB 的 `telegram.Bot`、`telegram.ext.ExtBot` 固定在 INFO，即使根日志级别设为 DEBUG 也不会输出这些 URL；启动失败的错误信息也会先改写 token 再记录。

### 运行机器人

`akkiabot serve` 在同一个事件循环中运行 python-telegram-bot 与 uvicorn（FastAPI lifespan 负责启动和关闭机器人、数据库与缓存）：

- 测试环境：`TELEGRAM_TEST_ENVIRONMENT=true` 时 Bot API 改用 Telegram 测试环境端点（`/bot<token>/test/`），只接受测试环境 BotFather 签发的 token；默认 `false` 为生产端点。真实验收只用测试环境，见 [docs/e2e-acceptance.md](docs/e2e-acceptance.md)。
- 接收更新：默认长轮询（`TELEGRAM_MODE=polling`）；设为 `webhook` 时需同时配置 `TELEGRAM_WEBHOOK_URL`（API 公网地址，机器人注册 `<地址>/telegram/webhook`）与 `TELEGRAM_WEBHOOK_SECRET`（校验 `X-Telegram-Bot-Api-Secret-Token`）。两种方式都显式订阅 `message`、`callback_query`、`chat_member`、`my_chat_member`、`chat_join_request`。
- HTTP：`SERVER_HOST:SERVER_PORT`（默认 `0.0.0.0:8000`）提供 `/healthz`（存活）、`/readyz`（数据库与缓存连通，失败时 503）与 `POST /api/v1/captcha/verify`（见下方入群验证）。CORS 只放行 `CORS_ALLOW_ORIGINS` 中列出的来源（生产为 `https://bot.akkia.moe`），未配置时不发送任何 CORS 头。
- 指标：`/metrics` 只在独立端口 `OBSERVABILITY_METRICS_HOST:OBSERVABILITY_METRICS_PORT`（默认 `127.0.0.1:9464`）提供，不经公网 tunnel 暴露；容器内由 compose 内网访问时把 host 设为 `0.0.0.0` 且不要发布该端口。
- 命令：`bot/commands/__init__.py` 中的 `COMMANDS` 是唯一的声明式列表，同时生成命令处理器、`set_my_commands` 菜单和 `/help` 文本。需要私聊深链的功能用 `StartPayloadStore.create()` 生成一次性 payload（基于 `Cache.pop`，默认 `TELEGRAM_START_PAYLOAD_TTL=600` 秒），并通过 `BotDeps.on_start(kind, handler)` 注册分发。
- 群消息：记录 `message_log`（只存 ID）与成员发言计数；首次见到某个群时写入 `chat`；按群配置 `delete_service_messages`（默认开启）删除入群、退群、置顶等系统消息。`message_log` 由定时任务按 `TELEGRAM_MESSAGE_LOG_RETENTION_DAYS`（默认 30 天）清理。
- 群配置：`services/chat_settings.py` 读写 `chat.settings`（可移植 JSON 列，`ChatSettings` 校验）；读取经共享缓存（5 分钟），写入提交后删除缓存项。`/config` 由群主或具有“封禁成员”权限的管理员在群内发起 → 一次性深链（绑定该群和该用户）→ 私聊中的 inline 开关（反垃圾、入群验证、警告、删除系统消息），每次点击都重新校验发起人在群内的权限。
- 群管理：`/ban`、`/warn` 必须回复要处理的成员的消息使用，规则在 `services/moderation.py`：操作者须为群主或具有“封禁成员”权限的管理员，目标不能是群主或管理员，匿名管理员需关闭匿名。`/ban` 封禁后按 `message_log` 分批（每批 100 条、最多 4 个并发）删除该成员的历史消息，遇到 429 按 `retry_after` 退避重试；`/warn` 需先在 `/config` 开启，达到 `max_warn_count`（默认 3）后自动封禁、删除历史并清零。所有操作写入 `chat_member.restriction`（`admin_ban` / `warn_ban`）。
- 入群申请（`bot/handlers/join_request.py`，沿用旧版行为）：有人通过需要管理员审批的邀请链接申请加入时（`chat_join_request`，此时还**不是**成员），机器人私聊申请人一次，原样发送旧版文案 `Hello, please wait for the admin to approve your request.`。机器人不批准也不拒绝申请，由管理员处理；入群验证在申请被批准、成员真正入群（`chat_member`）之后才开始。私聊发送失败（申请人已屏蔽机器人、超过 Telegram 允许的 5 分钟窗口等）只记 WARNING 日志 `join request notice failed` 和 `akkiabot_telegram_api_errors_total{method="sendMessage"}`，不重试，不影响申请本身。Telegram 只把入群申请推送给具备“邀请用户”权限的管理员机器人。
- 入群验证（`services/captcha.py`、`bot/captcha.py`）：群内开启“入群验证”后，成员身份从非成员变为成员（含带着限制状态重新入群）时，机器人禁言该成员（`chat_member.restriction = captcha_mute`），创建有效期 `WEBAPP_CAPTCHA_TIMEOUT`（默认 300 秒）的 `captcha_challenge`，并在群内发送脱敏称呼的提示和私聊深链。发起人在私聊 `/start` 后得到打开 `<WEBAPP_URL>/captcha?challenge=<id>` 的 Mini App 按钮；他人点击会被拒绝，且不会让链接失效（该 payload 在有效期内可重复使用）。Mini App 调用 `POST /api/v1/captcha/verify`，依次校验 Turnstile（`TURNSTILE_SECRET_KEY`，未配置时 503）、initData（含 `auth_date` 过期）、challenge 存在且未过期未使用、initData 用户与 challenge 一致、该成员当前是 captcha 禁言，然后解除禁言并通过 `answerWebAppQuery` 回执；错误以固定的 `error` 码返回。超时由 JobQueue 处理（每个 challenge 一个定时任务，另有每 30 秒一次的兜底扫描）：challenge 记为 `expired`，**成员保持禁言**，群内提示被删除；退群再入群会生成新的 challenge，管理员仍可手动解除禁言，此时 `chat_member` 同步为 `none`。机器人必须是群管理员（具备“封禁成员”“删除消息”权限）才能收到 `chat_member` 更新并禁言。
- 反垃圾（`services/antispam_guard.py` + `packages/antispam`）：群内开启“反垃圾”后，成员的文本消息与图片等媒体的说明文字在后台任务中交给 LLM 判定（不阻塞其他 handler）；已记录发言超过 `ANTISPAM_EXEMPT_MESSAGE_COUNT`（默认 3）条的成员豁免，管理员与频道身份发言不处理。命中时删除消息、永久禁言（`chat_member.restriction=spam_mute`）、`ANTISPAM_PURGE_DELAY`（默认 5 秒）后清理其历史消息，并发送一条 `ANTISPAM_NOTICE_TTL`（默认 60 秒）后自动删除的通知。判定结果写入 `message_log.spam_verdict`，命中时同时保存正文用于审计（清理历史时保留该行，直到保留期到期）。LLM 未配置（`LLM_API_KEY`、`LLM_MODEL`）时不做判定。
  - 分类器使用 OpenAI 兼容接口，默认请求 JSON 结构化输出，接口不支持时自动退回旧版的 `0/1` 回答（`ANTISPAM_STRUCTURED_OUTPUT=auto|json_schema|off`）；超时（`ANTISPAM_TIMEOUT`，默认 45 秒；判定在后台执行，推理型模型单次约 10–40 秒）、接口错误或无法解析的回答一律判为“非垃圾”（fail open）。提示词可用 `ANTISPAM_PROMPT_FILE` 覆盖（只替换角色、任务与示例部分，输出格式由代码追加），版本号取 `ANTISPAM_PROMPT_VERSION`，未设置时为 `custom-<内容哈希>`。
  - 配置了 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`（及可选的 `LANGFUSE_HOST`）时，每次 LLM 调用在 Langfuse 中记录为一个 generation：提示词版本、输入输出、耗时、token 用量与错误。
  - `/spam`（回复使用）：群主或有封禁权限的管理员使用时直接封禁并删除历史；普通成员举报时，把被举报人的名字、简介和消息内容交给 LLM，是垃圾信息则封禁（`restricted_by` 为机器人），否则编辑回执消息 @ 全部管理员人工审核。普通成员举报按 `ANTISPAM_REPORT_RATE_LIMIT` / `ANTISPAM_REPORT_RATE_WINDOW`（默认 300 秒内 3 次）限流。
  - 提示词基线：`just antispam-eval`（`akkiabot-antispam-eval`）用 `packages/antispam/tests/fixtures/eval_set.jsonl`（30 条正负样本）调用真实 LLM，输出准确率、精确率、召回率与判错样本；调整提示词或模型前后各跑一次对比。
- Mini App 验证页（`apps/webapp`，`/captcha?challenge=<id>`）：完成 Turnstile 后提交上述接口，显示成功（随后关闭 Mini App）、失败（可重试时提供“重试”）或已超时；Telegram 环境模拟只在开发服务器中启用。构建期变量 `VITE_API_BASE_URL`、`VITE_TURNSTILE_SITE_KEY`，详见 [docs/webapp.md](docs/webapp.md)。
- 可观测性：每个 handler 自动记录 `akkiabot_handler_duration_seconds`、`akkiabot_handler_errors_total`，命令另记 `akkiabot_command_total{command,result}`，每个 update 记 `akkiabot_updates_total{type}`；入群验证记 `akkiabot_captcha_total{result=passed|timeout|failed}`（`failed` 为被拒绝的提交）与 `akkiabot_captcha_duration_seconds`（入群到通过的耗时）；群管理记 `akkiabot_moderation_actions_total{action=ban|warn|mute|delete}`（`delete` 按删除的消息条数累加），相关 Bot API 失败记 `akkiabot_telegram_api_errors_total{method}`；反垃圾记 `akkiabot_antispam_verdict_total{verdict=spam|ham|error|skipped,action}`、`akkiabot_antispam_llm_duration_seconds`、`akkiabot_antispam_llm_errors_total{error}`；handler 异常以 ERROR 日志输出（带 `update_id`、`chat_id`、`user_id`、`trace_id`、`handler`），配置了 Sentry DSN 时同时上报 Sentry，且不会再被 PTB 重复上报。
- Telegram 交互测试：`akkiabot_server.testing` 提供 `FakeBotApi`（替换 Bot 的 HTTP 层，记录并应答所有 Bot API 调用）与 `UpdateFactory`（伪造群 / 私聊消息、回复、系统消息、成员变更、按钮回调），`member_responder()` 用来模拟 `getChatMember`；`apps/server/tests/conftest.py` 的 `runtime` fixture 给出一个已迁移、已初始化的运行时，用 `await runtime.application.process_update(update)` 驱动。

### 数据库与测试

- 迁移脚本位于 `packages/core/src/akkiabot_core/db/migrations/`（随包发布，镜像内可直接执行 `akkiabot migrate`）。迁移不在应用进程内自动执行，部署时需在启动应用前运行（`deploy/compose.yaml` 中由 `migrate` 服务负责）。
- `just test` 默认只用 SQLite 与内存 / fakeredis 运行；设置 `AKKIABOT_TEST_DATABASE_URL`、`AKKIABOT_TEST_REDIS_URL`（或使用 `just test-integration`）后，同一套迁移、repository 和缓存契约测试也会在 PostgreSQL / Redis 上运行。**测试会清空该数据库的 public schema**，不要指向有数据的库。CI 中两者都会运行。

### 模块边界

依赖方向由 import-linter 在 `just lint` 和 CI 中强制检查（规则见根 `pyproject.toml` 的 `[tool.importlinter]`，说明见[架构文档 §5.1](docs/architecture.md#51-模块边界与依赖方向)）：

- `apps/server` 可以依赖任意 `packages/*`，反之不行；
- `core`、`antispam`、`whois` 互不依赖；
- `packages/*` 不能导入 `telegram`、`fastapi`、`starlette`、`uvicorn`；
- `tools/e2e`（验收工具）只通过 Telegram 黑盒验证，不导入产品代码；产品代码也不依赖它和 Telethon。

违反时 `lint-imports` 会输出 `BROKEN` 并以非零状态退出；`tests/test_import_contracts.py` 中的反例测试覆盖了上述每条规则。

### CI 与发布

- 版本开发与发布（`docs/release.md`）：任务提向 `releases/vX.Y.Z`；`release-guard.yml` 检查 PR 目标、冻结与基分支；`release-cascade.yml` 逐级上合到后续版本和 master；`release-window.yml` 按周四、周日窗口冻结、出 RC、封版；`release-rc.yml` 出 RC 并构建镜像，运行机 `just release-staging` 持 staging 租约把同一 sha 的前后端发布到 staging 并验收；`release-seal.yml` 锁分支并打正式 tag；`release-production.yml` 校验后等待负责人审批生产。
- `ci.yml`：PR、master 与 `releases/**` 推送时运行。按改动路径决定执行 Python（lint / typecheck / test）、前端（lint / typecheck / test / build）和镜像构建（含启动冒烟）；`ci-ok` 汇总所有结果，适合作为分支保护的必需检查。
- `release.yml`：master 推送后构建并推送 `ghcr.io/akkialab/akkiabot:<sha>` 与 `:latest`；RC 与正式发布流水线按确定 sha 调用它（只打 sha 标签）。
- `webapp-deploy.yml` / `webapp-promote.yml`：构建 Mini App，检查产物不包含开发用的 Telegram 模拟代码，再用 `wrangler pages deploy` 发布到 Cloudflare Pages。PR 发布到预览地址；持 staging 租约的 RC 发布到 staging（`staging.akkiabot-webapp.pages.dev`，master 推送不再覆盖）；生产（`bot.akkia.moe`）只能通过 `webapp-promote.yml` 提升已通过 staging 发版验收的 sha，并且需要审批。详见 [docs/webapp.md](docs/webapp.md)。
- 前后端契约：pytest 校验 `apps/webapp/openapi.json` 与后端一致，前端 job 校验 `src/api/schema.d.ts` 与 `openapi.json` 一致；接口变更后运行 `just gen-api` 并提交两者。openapi-typescript 依赖 TypeScript 5 的编译器 API，而前端使用的 TypeScript 7 不再提供，因此它单独放在 `tools/api-types` 这个 pnpm 包里，自带 TypeScript 5。
- `pr-title.yml`：PR 标题必须形如 `AKKIA-<编号>: <摘要>`，正文必须含有同编号的 `Closes AKKIA-<编号>`（旧前缀 `BOT-` 不再接受），否则检查失败。脚本从 `GITHUB_EVENT_PATH` 事件文件读取标题与正文，失败提示只说明规则。工作流不得把 PR 标题、正文等不可信文本经步骤 `env`、`with` 或 `run` 中的 `${{ }}` 传入：Actions 会在公开日志中打印步骤的 `env`，`run` 中展开还可能被注入。
- 依赖升级由 Renovate 按 `renovate.json` 每周一提交 PR。

## License

[Apache-2.0](LICENSE)
