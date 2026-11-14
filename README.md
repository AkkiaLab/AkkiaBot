# AkkiaBot

AkkiaBot 是一个 Telegram 群管理机器人，提供入群人机验证（Cloudflare Turnstile + Telegram Mini App）、基于 LLM 的反垃圾、封禁 / 警告 / 举报、群功能开关、IP whois 查询等功能。

本仓库是 botlab 系列项目（botlab-core、botlab-llm、botlab-fe、botlab-telegram、Toolbox-py）的 monorepo 重写版本，正在逐模块迁移中。

- 架构与技术栈：[docs/architecture.md](docs/architecture.md)
- 迁移计划：[docs/migration-plan.md](docs/migration-plan.md)

## 开发

### 目录结构

```text
apps/server        后端服务（Python 包 akkiabot_server，入口命令 akkiabot）
apps/webapp        Telegram Mini App（Vite + React + TypeScript + Tailwind）
packages/core      akkiabot_core：配置、日志、数据库、缓存、领域模型
packages/antispam  akkiabot_antispam：LLM 反垃圾分类器（纯库）
packages/whois     akkiabot_whois：IP 查询 provider 链（纯库）
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
just db-revision "说明"  # 修改模型后自动生成新的 Alembic 迁移
just test-integration  # 连同 compose 中的 PostgreSQL / Redis 一起跑 pytest
just gen-api     # 由后端 OpenAPI 生成前端类型（M2 引入 HTTP API 后启用）
just docker-build  # 构建后端镜像并运行，打印版本号
```

只改一侧时，可以用 `just lint-py` / `just lint-web`、`just test-py` / `just test-web` 等子命令；`just test-py -k smoke` 会把参数透传给 pytest。

本地配置：复制 `.env.example` 为 `.env` 后填写，`.env` 不入库。变量名为 `<分组>_<字段>`（如 `TELEGRAM_BOT_TOKEN`、`DB_URL`、`OBSERVABILITY_SENTRY_DSN`），完整定义见 `packages/core/src/akkiabot_core/config.py`；必填项（`TELEGRAM_BOT_TOKEN`、`DB_URL`）缺失时启动即报错。

日志模块原样输出消息、extra 字段和异常，不做自动脱敏；记录配置或连接信息时请使用 `Settings.redacted()`、`DatabaseSettings.safe_url` 等安全形式，不要把 token、密码等已知密钥直接写进日志。

### 数据库与测试

- 迁移脚本位于 `packages/core/src/akkiabot_core/db/migrations/`（随包发布，镜像内可直接执行 `akkiabot migrate`）。迁移不在应用进程内自动执行，部署时需在启动应用前运行。
- `just test` 默认只用 SQLite 与内存 / fakeredis 运行；设置 `AKKIABOT_TEST_DATABASE_URL`、`AKKIABOT_TEST_REDIS_URL`（或使用 `just test-integration`）后，同一套迁移、repository 和缓存契约测试也会在 PostgreSQL / Redis 上运行。**测试会清空该数据库的 public schema**，不要指向有数据的库。CI 中两者都会运行。

### 模块边界

依赖方向由 import-linter 在 `just lint` 和 CI 中强制检查（规则见根 `pyproject.toml` 的 `[tool.importlinter]`，说明见[架构文档 §5.1](docs/architecture.md#51-模块边界与依赖方向)）：

- `apps/server` 可以依赖任意 `packages/*`，反之不行；
- `core`、`antispam`、`whois` 互不依赖；
- `packages/*` 不能导入 `telegram`、`fastapi`、`starlette`、`uvicorn`。

违反时 `lint-imports` 会输出 `BROKEN` 并以非零状态退出；`tests/test_import_contracts.py` 中的反例测试覆盖了上述每条规则。

### CI 与发布

- `ci.yml`：PR 和 master 推送时运行。按改动路径决定执行 Python（lint / typecheck / test）、前端（lint / typecheck / test / build）和镜像构建（含启动冒烟）；`ci-ok` 汇总所有结果，适合作为分支保护的必需检查。
- `release.yml`：master 推送后构建并推送 `ghcr.io/akkialab/akkiabot:<sha>` 与 `:latest`。
- `pr-title.yml`：PR 标题必须形如 `BOT-<编号>: <摘要>`，正文必须含有同编号的 `Closes BOT-<编号>`，否则检查失败。
- 依赖升级由 Renovate 按 `renovate.json` 每周一提交 PR。

## License

[Apache-2.0](LICENSE)
