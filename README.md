# AkkiaBot

AkkiaBot 是一个 Telegram 群管理机器人，提供入群人机验证（Cloudflare Turnstile + Telegram Mini App）、基于 LLM 的反垃圾、封禁 / 警告 / 举报、群功能开关、IP whois 查询等功能。

本仓库是 botlab 系列项目（botlab-core、botlab-llm、botlab-fe、botlab-telegram、Toolbox-py）的 monorepo 重写版本，正在逐模块迁移中。

- 架构与技术栈：[docs/architecture.md](docs/architecture.md)
- 迁移计划：[docs/migration-plan.md](docs/migration-plan.md)
- 真实 Telegram 验收（Telegram 测试环境 + 1Password + 测试用户账号）：[docs/e2e-acceptance.md](docs/e2e-acceptance.md)
- 部署与运维（生产 / 预发布 compose、配置项、升级回滚、备份、DNS）：[docs/operations.md](docs/operations.md)
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
just gen-api     # 由后端 OpenAPI 生成前端类型（M6 提供业务 API 后启用）
just docker-build  # 构建后端镜像并运行，打印版本号
just e2e         # 真实 Telegram 验收（需 1Password，见 docs/e2e-acceptance.md；CI 不运行）
```

只改一侧时，可以用 `just lint-py` / `just lint-web`、`just test-py` / `just test-web` 等子命令；`just test-py -k smoke` 会把参数透传给 pytest。

本地配置：复制 `.env.example` 为 `.env` 后填写，`.env` 不入库。变量名为 `<分组>_<字段>`（如 `TELEGRAM_BOT_TOKEN`、`DB_URL`、`OBSERVABILITY_SENTRY_DSN`），完整定义见 `packages/core/src/akkiabot_core/config.py`；必填项（`TELEGRAM_BOT_TOKEN`、`DB_URL`）缺失时启动即报错。

日志模块原样输出消息、extra 字段和异常，不做自动脱敏；记录配置或连接信息时请使用 `Settings.redacted()`、`DatabaseSettings.safe_url` 等安全形式，不要把 token、密码等已知密钥直接写进日志。Bot API 的 URL 中含有 bot token，`Runtime.build()` 因此把 `httpx`、`httpcore` 日志固定在 WARNING，把 PTB 的 `telegram.Bot`、`telegram.ext.ExtBot` 固定在 INFO，即使根日志级别设为 DEBUG 也不会输出这些 URL；启动失败的错误信息也会先改写 token 再记录。

### 运行机器人

`akkiabot serve` 在同一个事件循环中运行 python-telegram-bot 与 uvicorn（FastAPI lifespan 负责启动和关闭机器人、数据库与缓存）：

- 测试环境：`TELEGRAM_TEST_ENVIRONMENT=true` 时 Bot API 改用 Telegram 测试环境端点（`/bot<token>/test/`），只接受测试环境 BotFather 签发的 token；默认 `false` 为生产端点。真实验收只用测试环境，见 [docs/e2e-acceptance.md](docs/e2e-acceptance.md)。
- 接收更新：默认长轮询（`TELEGRAM_MODE=polling`）；设为 `webhook` 时需同时配置 `TELEGRAM_WEBHOOK_URL`（API 公网地址，机器人注册 `<地址>/telegram/webhook`）与 `TELEGRAM_WEBHOOK_SECRET`（校验 `X-Telegram-Bot-Api-Secret-Token`）。两种方式都显式订阅 `message`、`callback_query`、`chat_member`、`my_chat_member`。
- HTTP：`SERVER_HOST:SERVER_PORT`（默认 `0.0.0.0:8000`）提供 `/healthz`（存活）、`/readyz`（数据库与缓存连通，失败时 503）。
- 指标：`/metrics` 只在独立端口 `OBSERVABILITY_METRICS_HOST:OBSERVABILITY_METRICS_PORT`（默认 `127.0.0.1:9464`）提供，不经公网 tunnel 暴露；容器内由 compose 内网访问时把 host 设为 `0.0.0.0` 且不要发布该端口。
- 命令：`bot/commands/__init__.py` 中的 `COMMANDS` 是唯一的声明式列表，同时生成命令处理器、`set_my_commands` 菜单和 `/help` 文本。需要私聊深链的功能用 `StartPayloadStore.create()` 生成一次性 payload（基于 `Cache.pop`，默认 `TELEGRAM_START_PAYLOAD_TTL=600` 秒），并通过 `BotDeps.on_start(kind, handler)` 注册分发。
- 群消息：记录 `message_log`（只存 ID）与成员发言计数；首次见到某个群时写入 `chat`；按群配置 `delete_service_messages`（默认开启）删除入群、退群、置顶等系统消息。`message_log` 由定时任务按 `TELEGRAM_MESSAGE_LOG_RETENTION_DAYS`（默认 30 天）清理。
- 群配置：`services/chat_settings.py` 读写 `chat.settings`（可移植 JSON 列，`ChatSettings` 校验）；读取经共享缓存（5 分钟），写入提交后删除缓存项。`/config` 由群主或具有“封禁成员”权限的管理员在群内发起 → 一次性深链（绑定该群和该用户）→ 私聊中的 inline 开关（反垃圾、入群验证、警告、删除系统消息），每次点击都重新校验发起人在群内的权限。
- 群管理：`/ban`、`/warn` 必须回复要处理的成员的消息使用，规则在 `services/moderation.py`：操作者须为群主或具有“封禁成员”权限的管理员，目标不能是群主或管理员，匿名管理员需关闭匿名。`/ban` 封禁后按 `message_log` 分批（每批 100 条、最多 4 个并发）删除该成员的历史消息，遇到 429 按 `retry_after` 退避重试；`/warn` 需先在 `/config` 开启，达到 `max_warn_count`（默认 3）后自动封禁、删除历史并清零。所有操作写入 `chat_member.restriction`（`admin_ban` / `warn_ban`）。
- 可观测性：每个 handler 自动记录 `akkiabot_handler_duration_seconds`、`akkiabot_handler_errors_total`，命令另记 `akkiabot_command_total{command,result}`，每个 update 记 `akkiabot_updates_total{type}`；群管理记 `akkiabot_moderation_actions_total{action=ban|warn|delete}`（`delete` 按删除的消息条数累加），相关 Bot API 失败记 `akkiabot_telegram_api_errors_total{method}`；handler 异常以 ERROR 日志输出（带 `update_id`、`chat_id`、`user_id`、`trace_id`、`handler`），配置了 Sentry DSN 时同时上报 Sentry，且不会再被 PTB 重复上报。
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

- `ci.yml`：PR 和 master 推送时运行。按改动路径决定执行 Python（lint / typecheck / test）、前端（lint / typecheck / test / build）和镜像构建（含启动冒烟）；`ci-ok` 汇总所有结果，适合作为分支保护的必需检查。
- `release.yml`：master 推送后构建并推送 `ghcr.io/akkialab/akkiabot:<sha>` 与 `:latest`。
- `pr-title.yml`：PR 标题必须形如 `BOT-<编号>: <摘要>`，正文必须含有同编号的 `Closes BOT-<编号>`，否则检查失败。
- 依赖升级由 Renovate 按 `renovate.json` 每周一提交 PR。

## License

[Apache-2.0](LICENSE)
