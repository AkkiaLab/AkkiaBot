## 凭据扫描门禁

本文说明仓库在提交前、push 前和 CI 中如何拦截凭据，以及历史基线与例外的依据。测试样例的写法（mock 命名与注释）见 `AGENTS.md`。告警发现后的响应流程不在本文范围内。

> 文中括注的 `BOT-<编号>` 是旧工作区的任务编号，已不可访问，只作历史出处；相关结论已写在本文中。

### 组成

| 层 | 触发时机 | 命令 | 扫描范围 |
| --- | --- | --- | --- |
| pre-commit `gitleaks` | 每次 `git commit` | 自动执行 | 暂存区改动 |
| pre-push `gitleaks-unpushed` | 每次 `git push`，**包括新分支的首次 push** | 自动执行；手动执行 `uv run pre-commit run gitleaks-unpushed --hook-stage pre-push` | 公开 remote `origin` 上还没有的待推送提交（`<要推送的 ref> --not --remotes=origin`），逐个提交扫描，不只看最终文件。只排除 `origin`：已推到私有预审库的提交，推公开库时仍会被扫描 |
| pre-push `test-secret-samples` | 每次 `git push` | 自动执行 | 同下方测试守卫，检查当前工作区的已跟踪文件（不是逐个待推送提交） |
| pre-push `preapproval-check` | 推往公开仓库 `AkkiaLab/AkkiaBot` 时 | 自动执行（`scripts/internal_review.py check-push`） | 有公开库尚未包含的新提交时，待推送 head 必须等于评审者在 issue 中批准的 SHA，见下文“推送前的内部预审” |
| 手动全量 | 随时 | `just secret-scan` | HEAD 可达的全部提交 |
| CI 检查 `secret-scan` | 每个 PR、每次 push 到 master；不受路径过滤影响 | 同 `just secret-scan` | 检出 ref 可达的全部提交：PR 时为 master 历史加上 PR 的每个提交 |
| 测试守卫 `tests/test_secret_samples.py` | `just test` / CI python job | `uv run pytest tests/test_secret_samples.py` | 当前已跟踪文件：真实 Telegram token 格式，以及未就地声明为 mock 的凭据形字面量 |

- 安装：`just setup` 会执行 `pre-commit install`，并按 `default_install_hook_types` 同时安装 pre-commit 和 pre-push 两类钩子。已有 checkout 需要重新执行一次 `uv run pre-commit install`。pre-push 钩子读取的是当前检出中的 `.pre-commit-config.yaml`，基于旧 release 的检出要合入本改动后才有预审核对。
- 本地钩子和 CI 使用同一个 gitleaks：版本由 `.pre-commit-config.yaml` 中的 `rev` 固定（目前为 v8.30.0），规则来自 `.gitleaks.toml`，例外来自 `.gitleaksignore`。入口脚本为 `scripts/secret_scan.sh`。
- `secret-scan` 也列在 `ci-ok` 的 `needs` 中，所以 `ci-ok` 失败时也可能是凭据扫描失败。
- Ruff 的 `S105`/`S106` 和 `tests/test_secret_samples.py` 只是辅助手段，不能代替 gitleaks 做通用的凭据扫描。

### 规则

`.gitleaks.toml` 沿用 gitleaks 默认规则集（`useDefault = true`），并新增以下规则：

- `akkiabot-telegram-bot-token-bare`：不依赖关键词，匹配裸的 Telegram bot token 形态（bot id + `:A` + 33–34 位）。默认的 `telegram-bot-api-token` 规则要求值前面有 `telegram` 之类的关键词，因此漏掉了 BOT-21 告警 #1 中 `BOT_TOKEN = "..."` 形式的值。本规则加上后，全量扫描就能命中该值。

### 推送前的内部预审

负责人于 2026-11-20 确认（AKKIA-12、AKKIA-24）：推送公开仓库前，先在私有仓库完成小队内部评审，凭据和敏感内容在公开前被拦下，而不是公开后再发现。

**仓库**：`AkkiaLab/AkkiaBot-internal`，私有、关闭 Actions、不设分支保护。只用于评审，不在私有库合并。公开库的 `releases/*` 同步过去作为私有 PR 的 base。

**流程**

1. 实现者照常从目标 `releases/vX.Y.Z` 建分支 `akkia-<编号>-<描述>` 并提交，先推到私有库：`git push internal <分支>`（remote 不存在时执行一次 `just internal-sync`，会自动添加 `internal`）。
2. 在私有库开 PR，规则和公开侧相同：标题 `AKKIA-<编号>: <摘要>`，正文第一行 `Closes AKKIA-<编号>`，目标为对应的 `releases/*`。私有 PR 会被 Multica 关联到 issue，这是允许的（工作区已关闭“PR 合并自动完成 issue”）。PR 正文按公开标准写，公开 PR 原样使用。
3. 评审者在私有 PR 上逐提交审查，可开行内评论，需要时可运行测试；实现者修复后继续推私有库。中间提交含敏感内容时，先在私有库改写历史（squash / rebase 后 force push，私有库允许），再请求批准。
4. 所有智能体共用同一个 GitHub 账号，GitHub 上无法 Approve 自己的 PR，所以批准写在 Multica issue 评论中，单独一行、格式固定（行首不缩进、不加引用符号或反引号，冒号为全角）：
   - `预审通过：AkkiaLab/AkkiaBot-internal@<40 位 SHA>`
   - 撤销：`预审撤销：AkkiaLab/AkkiaBot-internal@<40 位 SHA>`

   引用（`>` 开头）、代码块（```` ``` ```` / `~~~` 围起，按 CommonMark 规则：关闭围栏只能是不短于开头的同种围栏字符加空白，后面带文字的不算关闭，未关闭的代码块延续到评论末尾）和行内代码中的同样文字都不算结论，所以回复中引用旧批准不会让撤销失效。
5. 实现者把**这个 SHA** 推到公开库（`git push origin <分支>`），pre-push 钩子核对通过后按现行规则开公开 PR，后续评审合并、CI 与评审线程处理不变。公开侧需要修改时回到第 3 步。
6. 公开 PR 合并后关闭私有 PR，删除私有分支。

**`preapproval-check` 钩子的判定**（`scripts/internal_review.py check-push`）

- 公开仓库按 GitHub 地址识别：`https://github.com/…`、`git@github.com:…`、`ssh://git@github.com/…`，以及官方 SSH 443 入口 `ssh.github.com`（端口和用户名不影响识别）。路径是 `AkkiaLab/AkkiaBot` 但主机无法确认（SSH 别名、代理、镜像等）时，守卫和预审核对都直接拒绝，请改用上述地址。
- 目标不是公开仓库（例如推私有库）、删除分支：放行。
- 先由多 ref 守卫检查完整的推送输入（见下一条），再进入 pre-commit 的单个 ref 判定。
- 待推送的提交都已在公开 remote 上（如发布分支、上合、已公开提交的新分支名）：放行。
- 否则从远端分支名（其次本地分支名）解析 `akkia-<编号>`，用 multica CLI（`$MULTICA_CLI_PATH`）读取该 issue 的全部评论，只认本机配置的评审者智能体写的预审结论，取最新一条：
  - 没有结论、最新为撤销、或批准的 SHA 与待推送 head 不完全一致：拒绝；
  - 分支名没有编号、本机未配置评审者、找不到 multica CLI 或读取失败：拒绝。
- **多 ref 守卫**：pre-commit 只把一次推送中的一个 ref 交给钩子（gitleaks、预审核对都只覆盖它）。因此 `just setup` 另外安装 `hooks/pre-push.legacy`（`scripts/internal_review.py install-guard`），pre-commit 生成的 pre-push 钩子会把完整的推送输入交给它。推往公开库时，只要有一个 ref 带有公开库没有的提交，本次推送就只能更新这一个 ref（删除不计），否则拒绝，请逐个分支推送；都不带新提交的多 ref 推送（如 tag）照常放行。守卫未安装时 `preapproval-check` 拒绝带新提交的公开推送并提示执行 `just setup`。守卫不读取 `SKIP`。
- 评审者的智能体 ID 只放在运行机的 git 配置中，不写进仓库：`git config --add akkia.preapproval.reviewer <agent id>`（受管检出共享 bare 仓库配置，每台运行机设置一次）。
- 确需跳过时显式执行 `SKIP=preapproval-check git push …`，并在 issue 中写明原因（负责人决定不在运行机上禁止跳过）。负责人手动推送、没有 multica CLI 时也用这种方式。

**同步发布分支**：`just internal-sync` 把公开库的全部 `releases/*` fast-forward 到私有库。方向只从公开到私有，不 force；私有侧分叉时推送被拒绝并报错，需人工查明，不要覆盖。推送前核对私有 remote 的全部有效推送地址（`git remote get-url --push --all`，含 `pushurl` 与 `pushInsteadOf` 改写）都指向 `AkkiaBot-internal`，且未设置 `mirror`，否则停止。输出只包含 ref 结果和退出码，不打印 remote URL 或 git 原始错误（可能含认证信息）。新建 release 分支后或私有 PR 的 base 落后时执行。

**已知限制**

- 钩子只核对提交内容。PR 标题、正文和公开评论无法由钩子拦截，靠评审者在私有 PR 上审阅正文、实现者原样发布。
- 钩子可以被跳过，CI `secret-scan` 仍是公开后的兜底。

### 失败时怎么处理

1. 输出始终脱敏，只显示规则、文件、行号和提交，不会出现值。贴到 issue 或 PR 时也只贴这些信息。
2. 如果是测试或文档样例：改成 `AGENTS.md` 要求的明显不可用、不符合真实格式且就地声明为 mock 的占位值，然后重新提交。命中值还在本地提交里没有 push 时，改写这些本地提交（如 `git commit --amend` 或交互式变基），不要再追加修正提交。
3. 如果可能是真实凭据：不要 push。按安全工单流程，用服务方无副作用的只读接口验证，并请负责人吊销或轮换。已经 push 的，删除代码不等于凭据失效。
4. 不要用 `--no-verify` 跳过 gitleaks 等扫描钩子（预审核对的跳过方式见下文），不要用拼接字符串或宽泛的 allowlist 绕过；CI 的 `secret-scan` 会再扫一遍全部提交。
5. 不要把未知的告警直接加进 `.gitleaksignore`，见下一节。

### 历史基线与例外

首次全量扫描在 2026-11-14 进行（BOT-24），使用本文规则，覆盖 HEAD 可达的提交以及 `--all` 范围内的远端分支。共有 5 条命中，涉及 2 个值，全部来自 BOT-7，并已在 BOT-21 中替换。`.gitleaksignore` 按精确指纹（`提交:文件:规则:行号`，不含值）只忽略这 5 条，新出现的同一个值仍会被拦截。

| 值（以 sha256 前 12 位区分） | 位置（提交） | 来源证据 | 有效性验证（Telegram `getMe`，生产与测试环境） |
| --- | --- | --- | --- |
| `2f059ebc7649` | `test_observability.py`：`e41198a6`:29、`6a76d770`:116、`bcfa463d`:116 | GitHub 告警 #1。BOT-21 核实：该值由模型会话直接写出，未在环境变量、1Password、旧仓库或本机文件中找到 | 2026-11-14 19:55 UTC：两个环境均返回 HTTP 401 / `error_code=401`，结论为无效 |
| `731fa294d036` | `test_telegram_utils.py`：`5cabac56`:16、`bcfa463d`:16 | 照抄自 Bot API 官方文档的示例 token：与 core.telegram.org/bots/api 页面上唯一的 token 形态值哈希一致 | 2026-11-14 20:00 UTC：两个环境均返回 HTTP 401 / `error_code=401`，结论为无效 |

验证方式：值只在验证进程内从 git 历史中读取，不出现在命令参数、日志或报告里；只调用官方文档中用于测试 token 的 `getMe`，不调用会产生副作用的方法；报告只记录时间、方法、HTTP 状态码、错误码和结论。网络失败、限流或本地格式检查失败都只能记为“未验证”。如果验证结果为有效，应立即停止调用，并请负责人吊销或轮换。“无效”只说明该值现在不能用，不等于证明它是人工构造的，所以来源证据与验证结果分开记录。

### 分支保护

master 由仓库 ruleset `master 保护`（id 27269655）保护，包含禁止删除、禁止非快进、要求 PR（仅 squash、评审线程须全部解决），以及必需状态检查。

- 必需检查：`secret-scan` 和 `ci-ok`，来源为 GitHub Actions（integration_id 15368），不要求分支保持最新（`strict_required_status_checks_policy: false`）。
- 变更记录：2026-11-14 核实时 ruleset 中没有任何必需检查。负责人在 BOT-24 中授权后，由 Engineer 在原有三条规则后追加了 `required_status_checks` 规则，其他字段不变，并通过 `gh api repos/AkkiaLab/AkkiaBot/rules/branches/master` 回读确认生效。
- 仓库已开启 GitHub secret scanning 和 push protection。
- 修改检查名称（job 的 `name`）前，要先同步修改 ruleset，否则所有 PR 都会因为等不到这两项检查而无法合并。
