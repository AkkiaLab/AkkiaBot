# PR 评审通知：评审回到原任务唤醒实现者（运维说明）

AkkiaBot 仓库的 PR 收到小队 Reviewer 以外的评审（Copilot、人类）时，自动回到 PR 关联的 Multica 任务，留一条记录并唤醒该任务的实现者处理。小队 Reviewer 的评审继续沿用它自己的交接（在 issue 上 @实现者），这里不重复提醒。来源：BOT-35（父任务 BOT-5）。

Multica 的 `issue wakeup` 没有 PR 评审事件，`--until-pr` 只支持 checks / merged / conflicts，所以采用与 BOT-25 相同的“GitHub 仓库 webhook → Multica webhook autopilot（run_only）”路径，由分派智能体按下文规则判定后写入原任务。

## 组成

| 项 | 值 | 说明 |
| --- | --- | --- |
| Autopilot | `PR 评审通知分派（AkkiaLab/AkkiaBot）`，run_only，分派智能体 Mika | 提示词即下文“分派提示词”，以本文为准；改动先改本文再同步到 autopilot |
| Webhook trigger | provider generic，label `github-pr-review` | 事件筛选（网页设置）：event `pull_request_review`，action `submitted` |
| GitHub 仓库 webhook | `AkkiaLab/AkkiaBot`，只订阅 `Pull request reviews`，JSON，开启 TLS 校验 | URL 含 Multica webhook 令牌，只在 Multica 与 GitHub hook 配置之间传递，不进仓库、评论和日志 |
| 业务去重键 | 原任务 metadata `github_review_<review id>` | 同一评审只通知一次 |

autopilot / trigger / hook 的实际 ID 记录在 BOT-35 中。

## 为什么只订阅 `pull_request_review` / `submitted`

- GitHub 上每条行级评论都属于某个 review：一次性提交的多条行级评论、单条“Add single comment”、在已有线程中的回复，都会产生一个新的 review 并触发一次 `pull_request_review` `submitted`。只订阅这一个事件，一次评审只会触发一次分派，不会再因为 `pull_request_review_comment` 的逐条事件重复提醒。
- 同一评审之后新增的意见（新的评审、线程回复）是新的 review id，会作为新的事件送达；已通知过的 review id 不会再次通知。
- `edited`、`dismissed` 以及 `pull_request_review_comment` 的 `created`/`edited`/`deleted` 不订阅：编辑不算新意见，实现者处理时会读取 PR 的最新内容。
- 不订阅 PR 对话区的 `issue_comment`（普通 PR 评论）。Copilot 与本仓库评审都走 review；只在对话区留言的意见不会自动通知，需要时在 issue 上 @ 实现者。

## 身份与排除规则（2026-11-15 用 GitHub API 只读核实）

| 身份 | GitHub user id | 处理 |
| --- | --- | --- |
| `akkiabot-reviewer[bot]`（App `akkiabot-reviewer`，App ID 5760780） | 356702926 | **排除**：小队 Reviewer，沿用其 issue 交接 |
| `copilot-pull-request-reviewer[bot]` | 175728472 | 通知 |
| `AkkiaS7`（实现类智能体提交身份，也是负责人账号） | 68485070 | 正文为空且行级评论**全部是线程回复**的评审视为实现者回复评审线程，跳过；其余（有正文或新开的行级评论）通知 |
| 其他人类 / bot | — | 通知 |

按 user id 判断，不按显示名。已知事实：

- 小队 Reviewer 在 BOT-3（PR #4，2026-11-14 17:36 UTC 合并）之前用 `AkkiaS7` 发过评审（PR #1–#3）；之后所有评审都来自 App，`AkkiaS7` 的评审只剩线程回复。若 Reviewer 再以 `AkkiaS7` 发带正文的评审，本机制无法与负责人本人的评审区分，会产生一次额外提醒；Reviewer 写操作必须走 `reviewer-gh`（见 `docs/reviewer-github-app.md`）。
- 负责人用 `AkkiaS7` 在已有线程里只做回复（不写评审正文）时，按上面的规则会被当成实现者回复跳过；需要实现者处理时请在 issue 上 @ 实现者，或提交带正文的评审。

## 路由、状态与去重

1. **范围**：只处理 `repository.id = 1457915068`（`AkkiaLab/AkkiaBot`）且 PR 为 open 的事件。已关闭 / 已合并 PR 上的评审只在运行输出说明，不写任何内容。
2. **关联**：从 PR 标题、head 分支、正文中 `Closes/Fixes/Resolves BOT-<n>` 提取候选编号，再用 `multica issue pull-requests` 核对该 issue 确实关联了这个 PR（仓库与编号一致、`link_source` 非空）。只认恰好一个已关联的 issue；没有关联或无法确定时不路由、不创建任务。
3. **原任务状态**：done / cancelled 不重开、不唤醒，只留一条记录评论；其他状态正常处理。
4. **实现者**：原任务负责人是智能体且不是 Reviewer 时唤醒它；负责人是成员、为空或为 Reviewer 时只留记录评论，不唤醒。
5. **去重**：
   - 同一 `X-GitHub-Delivery` 重投由 Multica 平台去重，只记录一次运行。
   - 业务去重：写入前查 metadata `github_review_<review id>`，已存在则结束；写入顺序为 metadata → 记录评论 → 唤醒。
   - 并发合并：已有本机制创建、尚未触发的同一实现者唤醒时（指令带 `[review-notify]` 标记），只追加记录评论，不再新建唤醒；唤醒延迟 2 分钟，短时间内的多次评审（如 Copilot 与人类相继提交）合并为一次实现者运行。平台在实现者已有排队运行时也会把唤醒并入该运行。
   - 残余风险：同一评审的两次不同 delivery 几乎同时到达时，可能都通过 metadata 检查，最多产生一条重复的记录评论；唤醒仍会被合并。
6. **不会形成循环**：实现者处理后在线程里的回复属于 `AkkiaS7` 纯回复评审，被跳过；分派智能体本身不在 GitHub 上写任何内容。

## 成本

GitHub 和 Multica 的事件筛选都无法按评审者过滤，小队 Reviewer 的每次评审（含线程回复）仍会启动一次分派运行，分派智能体不调用任何工具即判定结束，耗时数秒，**不是零消耗**。如需零消耗，可改为 GitHub Actions 先按评审者过滤再转发到 webhook（需要把 webhook URL 存为仓库 secret，参照 BOT-30 的 `MULTICA_PRUNE_WEBHOOK_URL`），目前未采用。

## 分派提示词

以下内容原样作为 autopilot 描述（run_only 运行提示词）。

```text
PR 评审通知分派（BOT-35 配置，规则见 AkkiaBot 仓库 docs/review-notify.md）。本运行由 GitHub 仓库 webhook 推送触发，Trigger payload 即 GitHub 原始 pull_request_review 载荷。你只做判定和通知，不改代码、不在 GitHub 上写任何内容、不创建 issue，不在输出或评论中复述 webhook URL 或令牌。

## 1. 先判定（只看载荷，不调用工具），任一不满足立即结束，不写任何东西
- 事件为 pull_request_review（X-GitHub-Event = pull_request_review），action = submitted；
- repository.id = 1457915068 且 repository.full_name = AkkiaLab/AkkiaBot；
- review.user.id ≠ 356702926 且 sender.id ≠ 356702926（akkiabot-reviewer[bot]，小队 Reviewer，沿用其自身交接）；
- pull_request.state = open（closed / 已合并 PR 只在输出说明，不通知）。
记下 N = pull_request.number，RID = review.id。

## 2. 评审内容（只读）
gh api --paginate repos/AkkiaLab/AkkiaBot/pulls/N/reviews/RID/comments --jq '.[] | {id, in_reply_to_id, path, line}'
- C = 行级评论数，R = 其中 in_reply_to_id 非空的数量；B = review.body 去掉空白。
- B 为空且 C = 0：结束（无须处理）。
- review.user.id = 68485070（AkkiaS7）且 B 为空且 C = R：结束（实现者在线程中的回复）。

## 3. 找到关联任务
- 候选编号：pull_request.title 中的 BOT-<n>（不区分大小写）、pull_request.head.ref 中的 bot-<n>、pull_request.body 中紧跟 Closes/Fixes/Resolves 的 BOT-<n>，去重。
- 对每个候选：
  "$MULTICA_CLI_PATH" issue get BOT-<n> --output json
  "$MULTICA_CLI_PATH" issue pull-requests BOT-<n> --output json
  pull_requests 中存在 repo_owner = AkkiaLab、repo_name = AkkiaBot、number = N 且 link_source 非空的条目才算已关联。
- 恰好一个已关联任务才继续（记为 ISSUE，取其 id、identifier、status、assignee_type、assignee_id）；零个或多个：结束，输出原因。

## 4. 业务去重
"$MULTICA_CLI_PATH" issue metadata get <ISSUE id> --key github_review_RID --output json
返回值存在：结束（重复通知）。报 not found（退出码 1）视为不存在；其他错误：结束并输出错误，不写入。不存在时：
"$MULTICA_CLI_PATH" issue metadata set <ISSUE id> --key github_review_RID --value notified --type string

## 5. 记录评论（不 @ 任何人）
在工作目录写文件后用 --content-file 发到 ISSUE（顶层评论），内容：
- 首行：[review-notify] PR #N 收到新的评审
- 评审者：review.user.login（review.user.type），状态 review.state，链接 review.html_url
- 行级评论 C 条（其中线程回复 R 条）；有正文时附前 300 字摘录（代码块包裹）
- 处理方式：见下一步结果（已唤醒 / 原任务已关闭未唤醒 / 负责人不是实现智能体未唤醒）
"$MULTICA_CLI_PATH" issue comment add <ISSUE id> --content-file <文件> --output json

## 6. 唤醒实现者
- ISSUE.status 为 done 或 cancelled：不唤醒、不改状态（评论中写“原任务已关闭，未唤醒”）。
- assignee_type ≠ agent，或 assignee_id = 77ea0846-9b13-4d08-ab12-a884e536a484（Reviewer）：不唤醒（评论中写明）。
- 否则 TARGET = assignee_id。先查：
  "$MULTICA_CLI_PATH" issue wakeup list <ISSUE id> --output json
  已有启用中、kind=at、尚未触发、目标为 TARGET 且指令以 [review-notify] 开头的规则：不新建（评论已记录，会在那次运行中一并处理）。
  否则在工作目录写指令文件，内容：
    [review-notify] PR #N 收到新的评审意见（见本 issue 上 [review-notify] 记录评论）。请按“处理评审意见”流程处理该 PR 上全部未解决的评审线程（含 Copilot 和人类）：修复的回复 commit 并标记解决，不采纳的回复理由；处理完按规则请 Reviewer 复审。若已无未处理意见，简短说明后结束。
  然后：
  "$MULTICA_CLI_PATH" issue wakeup create <ISSUE id> --agent-id TARGET --kind at --after 2m --instruction-file <文件> --output json

## 7. 结束
不修改 issue 状态和负责人，不修改本自动化配置。输出只写：判定结果与原因、ISSUE 编号（如有）、是否唤醒。
```

## 验收与变更

- 改提示词：先改本文并经 PR 评审，再用 `multica autopilot update <id> --description "<提示词>"` 同步，回读确认一致。
- 轮换 webhook URL：`multica autopilot trigger-rotate-url`，随后用 `gh api -X PATCH repos/AkkiaLab/AkkiaBot/hooks/<hook id>` 更新 hook 的 `config.url`，URL 只经变量传递，不回显。
- 停用：`multica autopilot update <id> --status paused`（GitHub 推送会返回 `autopilot_paused` 并被忽略），或在 GitHub 停用 hook。
