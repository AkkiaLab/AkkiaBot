# 智能体共用 GitHub App `akkialab-agent`（运维说明）

工作区所有智能体在 GitHub 上只用一个 App 身份：`akkialab-agent`（App ID 5760780，bot 用户 ID 356702926，在 PR 上显示为 `akkialab-agent[bot]`；改名前叫 `akkiabot-reviewer`）。负责人于 2026-11-20 确认（AKKIA-23 / AKKIA-25），不再按项目或角色另建 App。

身份分工：

- 提交、推送、开 PR、实现者回复评审线程：`AkkiaS7`。GitHub 不允许作者批准自己的 PR，所以作者身份和评审身份不能是同一个。
- 评审结论（Approve / Request changes）、解决线程、合并：`akkialab-agent`，经本机 `reviewer-gh`。
- 发布流水线的写操作（上合推送、冲突 PR 快进落地、版本分支与 tag 创建、封版规则集、建冲突 issue、触发工作流）：同一个 `akkialab-agent`，经 `.github/actions/release-setup` 与 `scripts/release/as-app.sh`（`docs/release.md` §7）。

来源：BOT-3（旧工作区编号，已不可访问）、AKKIA-25。

## 组成

| 项 | 位置 | 说明 |
| --- | --- | --- |
| App | AkkiaLab 组织 → Settings → Developer settings → GitHub Apps → `akkialab-agent` | 私有 App，不开启 webhook |
| 安装 | All repositories（工作区共用，负责人 2026-11-20 确认） | 本项目工具只为 `AUTHORIZED_REPOS` 中的仓库签发 token，见下文「授权仓库范围」 |
| 安装权限 | Contents R/W、Pull requests R/W、Issues R/W、Actions R/W、Administration R/W、Checks R、Commit statuses R、Metadata R | 等于发布用途的权限（`PURPOSES["release"]`），见下文「按用途签发 token」 |
| 规则集绕过 | `master 保护`、`releases`、`版本 tag 创建` 的绕过者（`always`） | 流水线写入需要；带来的风险见下文「风险与补偿」 |
| App 配置 | 运行机 `~/.config/akkiabot/reviewer-app/app.json` | `app_id`、`client_id`、`slug`，不是密钥 |
| 私钥（Reviewer） | 运行机 `~/.config/akkiabot/reviewer-app/private-key.pem` | 目录 `0700`、文件 `0600`；**不进仓库、不进 issue / 评论 / 日志** |
| 私钥（发布流水线） | 仓库变量 `RELEASE_APP_ID`、secret `RELEASE_APP_PRIVATE_KEY`；运行机 1Password `AkkiaBot/release-app`（`app_id`、`private_key`） | 与 Reviewer 使用**不同的私钥**（同一 App 可有多把），可以单独吊销 |
| token 缓存 | 运行机 `~/.cache/akkiabot/reviewer-app/token.json` | `0600`，只缓存 review 用途的 token，有效期 1 小时 |
| 脚本 | 仓库 `scripts/reviewer_gh.py` → 安装为 `~/.local/bin/reviewer-gh` | 只依赖 python3 标准库和 openssl |

## 授权仓库范围

App 作为工作区共用身份安装在整个组织（All repositories）。`scripts/reviewer_gh.py` 中的 `AUTHORIZED_REPOS` 规定**本项目工具**可以为哪些仓库签发 token，每个仓库注明记录授权的 issue（`BOT-<编号>` 为旧工作区编号，已不可访问；新增条目使用 `AKKIA-<编号>`）：

| 仓库 | 来源 |
| --- | --- |
| `AkkiaLab/AkkiaBot` | BOT-3 |
| `AkkiaLab/akkiabot-release-drill` | BOT-40（发布流程演练仓库） |

- 只列公开仓库：本仓库是公开的，私有仓库的名称不写进代码、文档和测试（AKKIA-25 起移除了此前列出的已归档旧仓库；它们仍留在 git 历史中）。
- 每个 token 都只限一个目标仓库（签发请求带 `repositories`），并按用途缩权。所以安装覆盖全组织时，本项目工具也只能操作上表中的仓库。
- 增减授权仓库：先由负责人确认，再通过 PR 修改 `AUTHORIZED_REPOS`，经评审合并到 master 后按下文重新安装脚本。

## 按用途签发 token

每个 installation token 只为一种用途签发，并在签发请求中显式指定 `permissions`，只拿该用途需要的权限（`scripts/reviewer_gh.py` 的 `PURPOSES`）。GitHub 拒绝签发安装上没有的权限；返回的权限超出请求时，脚本立即吊销该 token 并报错。

| 用途 | 签发方 | 权限 | 不包含 |
| --- | --- | --- | --- |
| `review` | `reviewer-gh gh …`（可缓存） | Contents R、Pull requests R/W、Checks R、Commit statuses R、Actions R、Metadata R | Contents 写（不能合并、推送、改文件）、Administration、Actions 写、Issues 写 |
| `merge` | `reviewer-gh merge`（不缓存，用完即吊销） | Contents R/W、Pull requests R/W、Checks R、Commit statuses R、Metadata R | Administration、Actions、Issues |
| `release` | `release-setup`（`permission-*` 输入）、`as-app.sh`（请求体） | 安装上的全部权限 | — |

三处 release 权限的一致性由 `tests/release/test_app_permissions.py` 校验。

## 风险与补偿

**风险**：规则集的绕过只能按身份设置，不能限定写入方式。`akkialab-agent` 是三个规则集的绕过者，所以用它合并 PR 时，GitHub **不会**执行必需检查（`ci-ok`、`secret-scan`、`release-guard`）、必需批准和“线程须解决”这些门禁。负责人已知道这一风险并决定合并两个 App（AKKIA-23）。

**工具侧补偿**：

1. `reviewer-gh merge <PR 号>` 是唯一的合并入口。它签发一个 merge 用途的 token，逐项核对，任一项不满足就拒绝合并并以退出码 2 结束：
   - PR 是打开状态，不是草稿；
   - 目标分支的必需检查（`GET /rules/branches/<base>` 中 `required_status_checks` 的全部条目）在**当前 head** 上都已通过，判定比 GitHub 更保守：
     - 同名的每一项结果都必须通过：每个 App 的最新一次 check run（结论为 success / neutral / skipped）**和**同名 commit status（success）同时检查，一个通过不能掩盖另一个失败；
     - 规则指定了 App（`integration_id`）时，至少要有该 App 的一次通过结果；
     - 目标分支没有配置任何必需检查时按失败处理；
   - GitHub 的测试合并提交（`merge_commit_sha`）上如果也有同名结果，同样必须全部通过；GitHub 尚未算出测试合并提交（`mergeable` 未知）时拒绝并提示稍后重试，有冲突时拒绝；
   - 没有未解决的评审线程（逐页读全，读不全拒绝）；
   - 本 App（按 bot 用户 ID 356702926 识别，不按登录名）的最新评审是对当前 head 的 Approve；
   - 任何人的最新评审都不是 Request changes。

   通过后**只以 squash 方式**合并（没有选择合并方式的参数：规则集只允许 squash，而 App 会绕过规则集，不能依赖 GitHub 代为拦截），调用时带上 `sha=<当前 head>`：门禁核对之后 PR 有新推送，GitHub 会拒绝合并。最后吊销 merge token（包括失败时）。
   - `--delete-branch` 只在合并成功后执行，只删除同仓库、且不受删除保护的 head 分支：head 分支命中带 `deletion` 规则的规则集或经典分支保护时跳过并说明（App 有绕过权限，直接删 ref 会绕过保护）。删除出错不影响合并结果：输出“已合并”后另行报告“清理未完成”，退出码为 3。
2. `reviewer-gh gh …` 使用 review 用途的 token，没有 Contents 写权限：`gh pr merge`、`gh api …/merge`、GraphQL `mergePullRequest` / 自动合并、推送和改文件都会被 GitHub 拒绝。脚本对这类调用提前报错并提示改用 `reviewer-gh merge`；这个提前报错只是提示，真正的边界是 token 权限。
3. 发布流水线的冲突 PR 落地另有门禁（`docs/release.md` §3.3），同样按 bot 用户 ID 识别 Reviewer 的批准。

**门禁的边界（剩余风险）**：

- 门禁只约束经 `reviewer-gh` 和发布工具的调用。**持有 App 私钥的人或进程**可以自己签发全权限 token，直接合并或推送到受保护分支而不受任何门禁约束。由于安装覆盖全组织，这个能力不限于本项目：持有私钥即可读写、管理组织内的**所有仓库**（包括其他项目和私有仓库），`AUTHORIZED_REPOS` 和按用途缩权只约束本项目工具自身。合并前只有发布私钥能绕过；合并后运行机上 Reviewer 的私钥文件也具备这个能力。现有保护是私钥文件 `0600`、发布私钥只在 Actions secret 与 1Password 中、私钥按用途分开以便单独吊销。
- 门禁核对与合并之间的数秒内新增的评审或线程不在核对范围内（head 变化已由 `sha` 固定）。
- 必需检查清单在合并时从规则集读取。App 有 Administration 写权限，理论上可以改规则集；这只能经发布工具的封版路径或持有私钥者发生。
- 普通 `gh pr merge`（用 `AkkiaS7` 身份）不受影响：`AkkiaS7` 不是绕过者，平台门禁照常生效。

## 平台层面的隔离调研（AKKIA-25，2026-11-20）

目标是让 Reviewer 的合并仍受平台门禁约束，只让流水线的写入绕过。结论：**在“一个 App 同时承担评审合并和流水线写入”的前提下，平台没有办法做到**；有一种可行的替代结构，但需要新增一项凭据，需负责人另行决定。

| 方案 | 结论 |
| --- | --- |
| 绕过模式 `pull_request` / `exempt` | 不可行。`pull_request` 只允许经 PR 绕过，恰好放行合并、拦下流水线需要的直接推送（上合 merge commit、快进落地、建分支和 tag）；`exempt` 与 `always` 一样对合并生效。 |
| 拆分规则集（把必需检查、PR 规则放进 App 不绕过的规则集） | 不可行。流水线的直接推送同样受这些规则约束：上合产生的新 merge commit 没有检查结果、建分支时必需检查也生效、“需要 PR”规则会拒绝任何直接推送。 |
| 把 GitHub Actions（`GITHUB_TOKEN`）设为绕过者 | 不采用。仓库内任何拿到写权限 `GITHUB_TOKEN` 的工作流（包括分支上被改过的工作流）都能绕过，范围比现在更大。 |
| 流水线改用**部署密钥**推送，部署密钥作为规则集绕过者（仓库级规则集支持 `DeployKey` 类型的绕过者），App 从所有绕过名单中移除 | 可行，是唯一能让平台继续约束 Reviewer 合并的结构：需要绕过的写入全部是 git 推送（`push_ref`），封版改规则集、建 issue、触发工作流只需要 App 权限，不需要绕过。代价是新增一把有写权限的部署密钥（Actions secret + 1Password），改造工作流和 `as-app.sh` 的推送方式。**未实测**，属于新增权限，需负责人确认后另开任务。 |

在负责人决定之前，按现状（App 绕过 + 上文工具侧补偿）运行，剩余风险如上节所列。

## `check` 的判定规则

- App 当前 slug 对应的 bot 用户 ID 必须是 356702926（改名后用于确认登录名已变、ID 未变）；
- 目标仓库必须在 `AUTHORIZED_REPOS` 中，且在安装范围内；已授权但尚未安装的仓库只打印提示；
- 安装方式（selected / all）不作要求；安装覆盖的其他仓库只打印数量、**不打印名称**（运行日志和 issue 评论里不出现组织的其他仓库名），不算失败；
- 安装权限必须足以签发 review 与 merge 用途的 token；加 `--require-release` 时还必须足以签发 release 用途的 token（规则集与发布凭据切换到本 App 之后的回读用）；
- 安装权限超出三种用途的并集时失败（多余权限）；
- App 不得订阅 webhook 事件。

## 工作方式

`reviewer-gh gh …` 在每次调用时：

1. 若缓存的 review token 剩余有效期不少于 15 分钟，直接复用（旧版本留下的、没有用途标记的缓存不复用）。
2. 否则调用 `openssl` 用私钥签名 RS256 JWT（有效期 9 分钟），私钥不经过 Python 进程。然后查询 App 在目标仓库上的 installation，换取**只限目标仓库、只含 review 权限**的 installation token，写入缓存。目标仓库由 `AKKIABOT_REVIEWER_REPO` 指定，默认 `AkkiaLab/AkkiaBot`；不在 `AUTHORIZED_REPOS` 中的仓库直接拒绝，不发起任何请求。
3. 通过环境变量 `GH_TOKEN` 把 token 交给 `gh`（`exec`），同时清除 `GITHUB_TOKEN`。

token 只通过环境变量传给 `gh`。脚本**没有**任何输出 token 的命令，因为智能体运行时会把 stdout 记入日志。如果缓存目录不可写（例如受沙箱限制），脚本会在 stderr 中提示，然后照常工作，只是每次调用都会重新签发 token。

## 安装与更新

只从**已合并到 `master` 的代码**安装，不要用 PR 分支里的版本覆盖。否则，一个改动脚本的 PR 就能在评审期间拿到私钥：

```bash
git -C <AkkiaBot master 检出> pull --ff-only
install -m 0755 <AkkiaBot master 检出>/scripts/reviewer_gh.py ~/.local/bin/reviewer-gh
sha256sum ~/.local/bin/reviewer-gh <AkkiaBot master 检出>/scripts/reviewer_gh.py   # 两者应一致
~/.local/bin/reviewer-gh revoke   # 丢弃旧版本缓存的全权限 token
```

## 自检

```bash
~/.local/bin/reviewer-gh check
~/.local/bin/reviewer-gh check --require-release   # 发布权限补齐、凭据切换之后
```

该命令不输出任何凭据，会打印 App slug、bot 用户 ID、installation、安装方式与已安装的授权仓库（其他仓库只给数量）、已授权但未安装的仓库、权限、各用途能否签发和 token 过期时间，不符合上节规则时以非 0 退出码结束。核对其他授权仓库时加上 `AKKIABOT_REVIEWER_REPO=AkkiaLab/<仓库>`。为了核对真实的安装范围，`check` 会临时签发一个只有 metadata 读权限、不限仓库的 token，用完立即吊销。

其他命令：

```bash
~/.local/bin/reviewer-gh --refresh check   # 忽略缓存，强制签发新 token
~/.local/bin/reviewer-gh revoke            # 吊销缓存中的 token 并删除缓存
```

## Reviewer 的常用命令

评审、线程操作通过 `reviewer-gh gh ...` 执行，并显式指定 `-R AkkiaLab/AkkiaBot`；**合并只能用 `reviewer-gh merge`**。

操作 AkkiaBot 以外的授权仓库时，用 `AKKIABOT_REVIEWER_REPO` 指定目标仓库，使 token 只限该仓库，并让 `-R` 与之一致，例如 `AKKIABOT_REVIEWER_REPO=AkkiaLab/akkiabot-release-drill ~/.local/bin/reviewer-gh gh pr review 23 -R AkkiaLab/akkiabot-release-drill --approve`。两者不一致时 GitHub 会拒绝访问。

```bash
R=~/.local/bin/reviewer-gh
$R gh pr review <n> -R AkkiaLab/AkkiaBot --approve -b "准出说明"
$R gh pr review <n> -R AkkiaLab/AkkiaBot --request-changes -b "必须修改项"
$R gh pr review <n> -R AkkiaLab/AkkiaBot --comment -b "建议"

# 列出评审线程，然后解决 / 重新打开
$R gh api graphql -f query='query($n:Int!){repository(owner:"AkkiaLab",name:"AkkiaBot"){pullRequest(number:$n){reviewThreads(first:100){nodes{id isResolved path comments(first:1){nodes{author{login} body}}}}}}}' -F n=<n>
$R gh api graphql -f query='mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}' -f id=<thread-id>
$R gh api graphql -f query='mutation($id:ID!){unresolveReviewThread(input:{threadId:$id}){thread{isResolved}}}' -f id=<thread-id>

# 合并：先核对必需检查、线程与本 App 的批准，再固定 head 合并
$R merge <n> --delete-branch                 # 只做 squash；-R 指定其他授权仓库
```

`merge` 退出码：0 已合并（含按保护规则跳过删除）；2 门禁未通过或合并失败，未合并，会列出全部不满足的项，处理后重新执行即可；3 已合并，但 head 分支清理失败，按提示手动处理分支即可，不要重新合并。上合冲突 PR（`release-forward` 标签）**不要**用 `merge`：它们由发布流水线快进落地（`docs/release.md` §3.3）。

Reviewer 以 App 身份提交的评审不会触发 PR 评审通知（`docs/review-notify.md`），实现者仍由 Reviewer 在 issue 上 @ 交接；不要用 `AkkiaS7` 身份发带正文的评审，否则会被当作外部意见再提醒一次。

installation token 不能调用 `gh api user`、`gh auth status` 等用户级接口，这属于正常现象。验证身份请使用 `reviewer-gh check`。

## 轮换与应急

- **怀疑 token 泄露**：执行 `reviewer-gh revoke`。即使不处理，token 也会在 1 小时内过期。
- **Reviewer 私钥轮换 / 泄露**（需要组织管理员，在浏览器中操作）：
  1. 打开 App 设置页 → Private keys → Generate a private key。
  2. 在运行机上执行 `umask 077`，然后用 `mv` 把下载的 `.pem` 移到 `~/.config/akkiabot/reviewer-app/private-key.pem`，并执行 `chmod 600`。
  3. 执行 `reviewer-gh --refresh check`，确认通过。
  4. 在 App 设置页删除旧私钥（只删 Reviewer 那一把，发布私钥不受影响），再执行 `reviewer-gh revoke`。
- **发布私钥轮换**：见 `docs/release.md` §7。
- **停用 App 身份**：在 AkkiaLab → Settings → GitHub Apps 中 Suspend 或 Uninstall `akkialab-agent`，已签发的 token 会立即失效。注意这会同时停掉评审和发布流水线。
