# Reviewer 的 GitHub App 身份（运维说明）

实现类智能体使用 `AkkiaS7` 提交 PR。Reviewer 智能体使用 GitHub App `akkiabot-reviewer`，在 PR 上显示为 `akkiabot-reviewer[bot]`。由于身份不同，Reviewer 可以对 AkkiaS7 的 PR 执行 Approve 或 Request changes、解决评审线程，并执行 squash merge。来源：BOT-3（旧工作区编号，已不可访问）。

## 组成

| 项 | 位置 | 说明 |
| --- | --- | --- |
| App | AkkiaLab 组织 → Settings → Developer settings → GitHub Apps → `akkiabot-reviewer` | 私有 App，不开启 webhook |
| 安装 | Only select repositories，范围不超出脚本中的 `AUTHORIZED_REPOS` | 见下文「授权仓库范围」 |
| 权限 | Contents R/W，Pull requests R/W，Checks / Commit statuses / Actions R，Metadata R | Contents 写权限用于合并和删除分支 |
| App 配置 | 运行机 `~/.config/akkiabot/reviewer-app/app.json` | `app_id`、`client_id`、`slug`，不是密钥 |
| 私钥 | 运行机 `~/.config/akkiabot/reviewer-app/private-key.pem` | 目录 `0700`、文件 `0600`；**不进仓库、不进 issue / 评论 / 日志** |
| token 缓存 | 运行机 `~/.cache/akkiabot/reviewer-app/token.json` | `0600`，installation token 有效期 1 小时 |
| 脚本 | 仓库 `scripts/reviewer_gh.py` → 安装为 `~/.local/bin/reviewer-gh` | 只依赖 python3 标准库和 openssl |

## 授权仓库范围

`scripts/reviewer_gh.py` 中的 `AUTHORIZED_REPOS` 是 App 安装范围的唯一依据，每个仓库都注明了记录授权的 issue（`BOT-<编号>` 为旧工作区编号，已不可访问；新增条目使用 `AKKIA-<编号>`）：

| 仓库 | 来源 |
| --- | --- |
| `AkkiaLab/AkkiaBot` | BOT-3 |
| `AkkiaLab/akkiabot-release-drill` | BOT-40（发布流程演练仓库） |
| `AkkiaLab/botlab-core`、`botlab-telegram`、`botlab-llm`、`botlab-fe`、`Toolbox-py` | BOT-16（旧仓库迁移提示 PR，随后归档） |

调整范围的顺序：先由负责人确认授权，再通过 PR 修改 `AUTHORIZED_REPOS` 并经评审合并到 master、按下文安装；然后由组织管理员修改 installation 的仓库选择。缩小范围时顺序相反：先移除安装，再删列表项。

`check` 的判定规则：

- 安装方式必须是 selected；
- 安装范围中出现任何不在 `AUTHORIZED_REPOS` 里的仓库时失败；
- 目标仓库不在安装范围时失败；
- 已授权但尚未安装的仓库只打印提示，不算失败（例如管理员尚未完成勾选，或仓库归档后已从安装中移除）。

## 工作方式

`reviewer-gh` 在每次调用时执行以下步骤：

1. 若缓存的 token 剩余有效期不少于 15 分钟，直接复用。
2. 否则调用 `openssl` 用私钥签名 RS256 JWT（有效期 9 分钟），私钥不经过 Python 进程。然后查询 App 在目标仓库上的 installation，换取**只限目标仓库**的 installation token，写入缓存。目标仓库由 `AKKIABOT_REVIEWER_REPO` 指定，默认 `AkkiaLab/AkkiaBot`；不在 `AUTHORIZED_REPOS` 中的仓库直接拒绝，不发起任何请求。
3. 通过环境变量 `GH_TOKEN` 把 token 交给 `gh`（`exec`），同时清除 `GITHUB_TOKEN`。

token 只通过环境变量传给 `gh`。脚本**没有**任何输出 token 的命令，因为智能体运行时会把 stdout 记入日志。如果缓存目录不可写（例如受沙箱限制），脚本会在 stderr 中提示，然后照常工作，只是每次调用都会重新签发 token。

## 安装与更新

只从**已合并到 `master` 的代码**安装，不要用 PR 分支里的版本覆盖。否则，一个改动脚本的 PR 就能在评审期间拿到私钥：

```bash
git -C <AkkiaBot master 检出> pull --ff-only
install -m 0755 <AkkiaBot master 检出>/scripts/reviewer_gh.py ~/.local/bin/reviewer-gh
sha256sum ~/.local/bin/reviewer-gh <AkkiaBot master 检出>/scripts/reviewer_gh.py   # 两者应一致
```

## 自检

```bash
~/.local/bin/reviewer-gh check
```

该命令不输出任何凭据，会打印 App slug、installation、安装范围、已授权但未安装的仓库、权限和 token 过期时间。安装范围不符合上文「授权仓库范围」的规则、权限与上表不一致或 App 订阅了 webhook 事件时，以非 0 退出码结束。核对其他授权仓库时加上 `AKKIABOT_REVIEWER_REPO=AkkiaLab/<仓库>`。为了核对真实的安装范围，`check` 会临时签发一个只有 metadata 读权限、不限仓库的 token，用完立即吊销。

其他命令：

```bash
~/.local/bin/reviewer-gh --refresh check   # 忽略缓存，强制签发新 token
~/.local/bin/reviewer-gh revoke            # 吊销缓存中的 token 并删除缓存
```

## Reviewer 的常用命令

所有写操作都要通过 `reviewer-gh gh ...` 执行，并显式指定 `-R AkkiaLab/AkkiaBot`。只读操作也可以这样执行。

操作 AkkiaBot 以外的授权仓库时，用 `AKKIABOT_REVIEWER_REPO` 指定目标仓库，使 token 只限该仓库，并让 `-R` 与之一致，例如 `AKKIABOT_REVIEWER_REPO=AkkiaLab/botlab-core ~/.local/bin/reviewer-gh gh pr merge 23 -R AkkiaLab/botlab-core --squash`。两者不一致时 GitHub 会拒绝访问。

```bash
R=~/.local/bin/reviewer-gh
$R gh pr review <n> -R AkkiaLab/AkkiaBot --approve -b "准出说明"
$R gh pr review <n> -R AkkiaLab/AkkiaBot --request-changes -b "必须修改项"
$R gh pr review <n> -R AkkiaLab/AkkiaBot --comment -b "建议"

# 列出评审线程，然后解决 / 重新打开
$R gh api graphql -f query='query($n:Int!){repository(owner:"AkkiaLab",name:"AkkiaBot"){pullRequest(number:$n){reviewThreads(first:100){nodes{id isResolved path comments(first:1){nodes{author{login} body}}}}}}}' -F n=<n>
$R gh api graphql -f query='mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}' -f id=<thread-id>
$R gh api graphql -f query='mutation($id:ID!){unresolveReviewThread(input:{threadId:$id}){thread{isResolved}}}' -f id=<thread-id>

$R gh pr merge <n> -R AkkiaLab/AkkiaBot --squash --delete-branch
```

Reviewer 以 App 身份提交的评审不会触发 PR 评审通知（`docs/review-notify.md`），实现者仍由 Reviewer 在 issue 上 @ 交接；不要用 `AkkiaS7` 身份发带正文的评审，否则会被当作外部意见再提醒一次。

installation token 不能调用 `gh api user`、`gh auth status` 等用户级接口，这属于正常现象。验证身份请使用 `reviewer-gh check`。

## 轮换与应急

- **怀疑 token 泄露**：执行 `reviewer-gh revoke`。即使不处理，token 也会在 1 小时内过期。
- **私钥轮换 / 泄露**（需要组织管理员，在浏览器中操作）：
  1. 打开 App 设置页 → Private keys → Generate a private key。
  2. 在运行机上执行 `umask 077`，然后用 `mv` 把下载的 `.pem` 移到 `~/.config/akkiabot/reviewer-app/private-key.pem`，并执行 `chmod 600`。
  3. 执行 `reviewer-gh --refresh check`，确认通过。
  4. 在 App 设置页删除旧私钥，再执行 `reviewer-gh revoke`。
- **停用 App 身份**：在 AkkiaLab → Settings → GitHub Apps 中 Suspend 或 Uninstall `akkiabot-reviewer`，已签发的 token 会立即失效。
