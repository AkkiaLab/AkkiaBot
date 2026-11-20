## 凭据扫描门禁

本文说明仓库在提交前、push 前和 CI 中如何拦截凭据，以及历史基线与例外的依据。测试样例的写法（mock 命名与注释）见 `AGENTS.md`。告警发现后的响应流程不在本文范围内。

> 文中括注的 `BOT-<编号>` 是旧工作区的任务编号，已不可访问，只作历史出处；相关结论已写在本文中。

### 组成

| 层 | 触发时机 | 命令 | 扫描范围 |
| --- | --- | --- | --- |
| pre-commit `gitleaks` | 每次 `git commit` | 自动执行 | 暂存区改动 |
| pre-push `gitleaks-unpushed` | 每次 `git push`，**包括新分支的首次 push** | 自动执行；手动执行 `uv run pre-commit run gitleaks-unpushed --hook-stage pre-push` | 公开 remote `origin` 上还没有的待推送提交（`<要推送的 ref> --not --remotes=origin`），逐个提交扫描，不只看最终文件。只排除 `origin`：已推到其他 remote 的提交，推公开库时仍会被扫描 |
| pre-push `test-secret-samples` | 每次 `git push` | 自动执行 | 同下方测试守卫，检查当前工作区的已跟踪文件（不是逐个待推送提交） |
| 手动全量 | 随时 | `just secret-scan` | HEAD 可达的全部提交 |
| CI 检查 `secret-scan` | 每个 PR、每次 push 到 master；不受路径过滤影响 | 同 `just secret-scan` | 检出 ref 可达的全部提交：PR 时为 master 历史加上 PR 的每个提交 |
| 测试守卫 `tests/test_secret_samples.py` | `just test` / CI python job | `uv run pytest tests/test_secret_samples.py` | 当前已跟踪文件：真实 Telegram token 格式，以及未就地声明为 mock 的凭据形字面量 |

- 安装：`just setup` 会执行 `pre-commit install`，并按 `default_install_hook_types` 同时安装 pre-commit 和 pre-push 两类钩子。已有 checkout 需要重新执行一次 `uv run pre-commit install`。pre-push 钩子读取的是当前检出中的 `.pre-commit-config.yaml`。
- 本地钩子和 CI 使用同一个 gitleaks：版本由 `.pre-commit-config.yaml` 中的 `rev` 固定（目前为 v8.30.0），规则来自 `.gitleaks.toml`，例外来自 `.gitleaksignore`。入口脚本为 `scripts/secret_scan.sh`。
- `secret-scan` 也列在 `ci-ok` 的 `needs` 中，所以 `ci-ok` 失败时也可能是凭据扫描失败。
- Ruff 的 `S105`/`S106` 和 `tests/test_secret_samples.py` 只是辅助手段，不能代替 gitleaks 做通用的凭据扫描。

### 规则

`.gitleaks.toml` 沿用 gitleaks 默认规则集（`useDefault = true`），并新增以下规则：

- `akkiabot-telegram-bot-token-bare`：不依赖关键词，匹配裸的 Telegram bot token 形态（bot id + `:A` + 33–34 位）。默认的 `telegram-bot-api-token` 规则要求值前面有 `telegram` 之类的关键词，因此漏掉了 BOT-21 告警 #1 中 `BOT_TOKEN = "..."` 形式的值。本规则加上后，全量扫描就能命中该值。

### 失败时怎么处理

1. 输出始终脱敏，只显示规则、文件、行号和提交，不会出现值。贴到 issue 或 PR 时也只贴这些信息。
2. 如果是测试或文档样例：改成 `AGENTS.md` 要求的明显不可用、不符合真实格式且就地声明为 mock 的占位值，然后重新提交。命中值还在本地提交里没有 push 时，改写这些本地提交（如 `git commit --amend` 或交互式变基），不要再追加修正提交。
3. 如果可能是真实凭据：不要 push。按安全工单流程，用服务方无副作用的只读接口验证，并请凭据所有者吊销或轮换。已经 push 的，删除代码不等于凭据失效。
4. 不要用 `--no-verify` 跳过 gitleaks 等扫描钩子，不要用拼接字符串或宽泛的 allowlist 绕过；CI 的 `secret-scan` 会再扫一遍全部提交。
5. 不要把未知的告警直接加进 `.gitleaksignore`，见下一节。

### 历史基线与例外

首次全量扫描在 2026-11-14 进行（BOT-24），使用本文规则，覆盖 HEAD 可达的提交以及 `--all` 范围内的远端分支。共有 5 条命中，涉及 2 个值，全部来自 BOT-7，并已在 BOT-21 中替换。`.gitleaksignore` 按精确指纹（`提交:文件:规则:行号`，不含值）只忽略这 5 条，新出现的同一个值仍会被拦截。

| 值（以 sha256 前 12 位区分） | 位置（提交） | 来源证据 | 有效性验证（Telegram `getMe`，生产与测试环境） |
| --- | --- | --- | --- |
| `2f059ebc7649` | `test_observability.py`：`e41198a6`:29、`6a76d770`:116、`bcfa463d`:116 | GitHub 告警 #1。BOT-21 核实：该值由模型会话直接写出，未在环境变量、1Password、旧仓库或本机文件中找到 | 2026-11-14 19:55 UTC：两个环境均返回 HTTP 401 / `error_code=401`，结论为无效 |
| `731fa294d036` | `test_telegram_utils.py`：`5cabac56`:16、`bcfa463d`:16 | 照抄自 Bot API 官方文档的示例 token：与 core.telegram.org/bots/api 页面上唯一的 token 形态值哈希一致 | 2026-11-14 20:00 UTC：两个环境均返回 HTTP 401 / `error_code=401`，结论为无效 |

验证方式：值只在验证进程内从 git 历史中读取，不出现在命令参数、日志或报告里；只调用官方文档中用于测试 token 的 `getMe`，不调用会产生副作用的方法；报告只记录时间、方法、HTTP 状态码、错误码和结论。网络失败、限流或本地格式检查失败都只能记为“未验证”。如果验证结果为有效，应立即停止调用，并请凭据所有者吊销或轮换。“无效”只说明该值现在不能用，不等于证明它是人工构造的，所以来源证据与验证结果分开记录。

### 分支保护

master 由仓库 ruleset `master 保护`（id 27269655）保护，包含禁止删除、禁止非快进、要求 PR（仅 squash、评审线程须全部解决），以及必需状态检查。

- 必需检查：`secret-scan` 和 `ci-ok`，来源为 GitHub Actions（integration_id 15368），不要求分支保持最新（`strict_required_status_checks_policy: false`）。
- 变更记录：2026-11-14 核实时 ruleset 中没有任何必需检查。经维护者授权（BOT-24），在原有三条规则后追加了 `required_status_checks` 规则，其他字段不变，并通过 `gh api repos/AkkiaLab/AkkiaBot/rules/branches/master` 回读确认生效。
- 仓库已开启 GitHub secret scanning 和 push protection。
- 修改检查名称（job 的 `name`）前，要先同步修改 ruleset，否则所有 PR 都会因为等不到这两项检查而无法合并。
