# PR 评审通知：评审回到原任务唤醒实现者（运维说明）

AkkiaBot 仓库的 PR 收到小队 Reviewer 以外的评审（Copilot、人类）时，自动回到 PR 关联的 Multica 任务，留一条记录并唤醒该任务的实现者处理。小队 Reviewer 的评审继续沿用它自己的交接（在 issue 上 @实现者），这里不重复提醒。来源：BOT-35（父任务 BOT-5）；本文中的 `BOT-<编号>` 均为旧工作区任务编号，已不可访问，只作历史出处。

> 状态：旧工作区的 autopilot 已停用，本机制尚待迁入 AkkiaLab 工作区（由 DevOps 主责，外部 webhook 切换需负责人确认）。迁入前外部评审不会自动通知，需要时在 issue 上 @ 实现者。

Multica 的 `issue wakeup` 没有 PR 评审事件，`--until-pr` 只支持 checks / merged / conflicts，所以采用“GitHub 仓库 webhook → Multica webhook autopilot（run_only）”路径，由分派智能体按下文规则判定后写入原任务。

## 组成

| 项 | 值 | 说明 |
| --- | --- | --- |
| Autopilot | `PR 评审通知分派（AkkiaLab/AkkiaBot）`，run_only，分派智能体在迁入时指定（下文提示词用运行时的 `MULTICA_AGENT_ID` 识别自身，不写死 id） | 提示词即下文“分派提示词”，以本文为准；改动先改本文再同步到 autopilot |
| Webhook trigger | provider generic，label `github-pr-review` | 事件筛选（网页设置）：event `pull_request_review`，action `submitted` |
| GitHub 仓库 webhook | `AkkiaLab/AkkiaBot`，只订阅 `Pull request reviews`，JSON，开启 TLS 校验 | URL 含 Multica webhook 令牌，只在 Multica 与 GitHub hook 配置之间传递，不进仓库、评论和日志 |
| 进度键 | 原任务 metadata `github_review_<review id>`，值 `commented:<评论 id>` / `done:<评论 id>:<唤醒 id 或 none>` | 记录每条评审的处理进度，只有 `done:` 才结束；见“分步状态与失败恢复” |

autopilot / trigger / hook 的实际 ID 以 Multica 中的 autopilot 配置和 GitHub 仓库 hook 设置为准，不写入仓库。

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

- 小队 Reviewer 在 Reviewer App 接入（PR #4，2026-11-14 17:36 UTC 合并）之前用 `AkkiaS7` 发过评审（PR #1–#3）；之后所有评审都来自 App，`AkkiaS7` 的评审只剩线程回复。若 Reviewer 再以 `AkkiaS7` 发带正文的评审，本机制无法与负责人本人的评审区分，会产生一次额外提醒；Reviewer 写操作必须走 `reviewer-gh`（见 `docs/reviewer-github-app.md`）。
- 负责人用 `AkkiaS7` 在已有线程里只做回复（不写评审正文）时，按上面的规则会被当成实现者回复跳过；需要实现者处理时请在 issue 上 @ 实现者，或提交带正文的评审。

## 路由、状态与去重

1. **范围**：只处理 `repository.id = 1457915068`（`AkkiaLab/AkkiaBot`）且 PR 为 open 的事件。已关闭 / 已合并 PR 上的评审只在运行输出说明，不写任何内容。
2. **关联**：从 PR 标题、head 分支、正文中 `Closes/Fixes/Resolves AKKIA-<n>` 提取候选编号，再用 `multica issue pull-requests` 核对该 issue 确实关联了这个 PR（仓库与编号一致、`link_source` 非空）。只认恰好一个已关联的 issue；没有关联或无法确定时不路由、不创建任务。
3. **原任务状态**：done / cancelled 不重开、不唤醒，只留一条记录评论；其他状态正常处理。
4. **实现者**：原任务负责人是智能体且不是 Reviewer 时唤醒它；负责人是成员、为空或为 Reviewer 时只留记录评论，不唤醒。
5. **分步状态与失败恢复**（每条评审独立推进，任何一步失败都可从断点续做）：
   - metadata `github_review_<review id>` 只记录进度，**不作为跳过的闸门**：值为 `done:…` 才表示通知已完成并结束；不存在或为 `commented:<评论 id>` 时，从头按下列幂等步骤重做。
   - 记录评论：先按首行标记 `[review-notify] PR #<N> review <review id>` 查找已有评论，没有才发；发完等 5 秒再查，分派智能体发的、标记相同的评论保留创建时间最早（同时间取 id 最小）的一条，**其余全部删除（不论是哪次运行发的）**。然后写 `commented:<评论 id>`。
   - 唤醒：见下一条“并发收敛”。确认存在一条有效唤醒（或按状态规则无须唤醒）后才写 `done:<评论 id>:<唤醒 id 或 none>`。
   - 任一步失败或运行中断：已发的评论 / 已建的唤醒会被下一次处理按标记找到并复用，不会漏通知也不会重复。
6. **并发收敛**（平台没有原子的“已存在则跳过”：实测两次并发 `wakeup create` 会生成两条规则）：
   - 本机制的资源范围：由分派智能体创建、启用中、`kind=at`、`fire_count=0`、目标为同一实现者、指令以 `[review-notify]` 开头的待触发唤醒。这些规则只由本机制创建，任何一次分派运行都可以清理。
   - 没有候选才新建（延迟 2 分钟）；新建后等 5 秒重新列出，只保留创建时间最早（同时间取 id 最小）的一条，**其余候选全部删除，不论是哪次运行创建的**。删除会撤回该规则尚未开始的运行。
   - 收敛不依赖创建者存活：全局最早的那条规则，任何看到它的运行都会保留，没有人会删它；比它晚的规则，只要后来有任何运行看到，就会被删除。所以某个创建者在建完规则后中断，它的遗留规则会被下一次分派运行（同 issue 的新评审、该评审的补偿重试、补偿扫描）删除。
   - 实现者兜底：唤醒指令要求实现者在开始处理前，删除本 issue 上其余指向自己的待触发 `[review-notify]` 规则（正在触发的那条 `fire_count>0`，不在其中）。这些规则对应的评审在本次运行开始前已经提交，会在本次运行中一并处理。即使中断遗留的规则在所有分派运行结束后才出现，也会在实现者开始处理前被清掉（同时触发的边界见第 7 条）。
   - 同一 review id 的多次投递、几乎同时到达的不同评审（如 Copilot 与人类相继提交）都收敛到这一条唤醒，实现者只运行一次。
   - 一条唤醒触发后（`fire_count>0`），之后到达的评审会新建下一条唤醒，实现者会再运行一次：已开始的运行不保证看到之后的新意见，这是有意的“新意见至少送达一次”。
7. **重投与恢复入口**：
   - 同一 `X-GitHub-Delivery` 重投由 Multica 平台去重，只记录一次运行；因此投递一旦被接收而分派运行失败，GitHub 重投不能补救。实测：GitHub Redeliver 沿用原 guid，Multica 返回 `duplicate`；autopilot 暂停期间被忽略（`autopilot_paused`）的投递，重投时同样返回 `duplicate`，只能用补偿扫描补回。
   - 平台对同一 issue 上同一智能体的运行串行执行：被唤醒的实现者运行若遇到该智能体在本 issue 上已有运行，会排队到前一个运行结束后再开始。
   - 同一 issue 后续任何评审的分派运行，会顺带检查该 issue 上未到 `done:` 的 `github_review_*` 并按上面的步骤补齐。
   - **补偿扫描**：手动执行 `multica autopilot trigger <autopilot id>`（无 GitHub 载荷）时，分派智能体扫描 `AkkiaLab/AkkiaBot` 所有 open PR 上最近 24 小时提交的评审，对每条按相同的判定和步骤处理；已 `done:` 的跳过，未完成的续做。发现分派运行 failed / skipped（`multica autopilot runs <autopilot id>`）或有人报告漏通知时，由 Engineer 或 Leader 执行。超过 24 小时的遗漏，在原 issue 上直接 @ 实现者。
   - 残余边界：实现者兜底清理之前，若遗留规则恰好与有效规则在同一时刻触发（两者创建间隔不超过分派运行的数秒），实现者可能各启动一次。平台会把排队中的唤醒并入同一运行，但无法保证一定合并；即使不合并，第二次运行也会发现已无未处理意见，简短说明后结束，不会漏通知。记录评论在中断时可能多出一条，下一次处理同一评审时会删除。
8. **不会形成循环**：实现者处理后在线程里的回复属于 `AkkiaS7` 纯回复评审，被跳过；分派智能体本身不在 GitHub 上写任何内容。

## 成本

GitHub 和 Multica 的事件筛选都无法按评审者过滤，小队 Reviewer 的每次评审（含线程回复）仍会启动一次分派运行，分派智能体不调用任何工具即判定结束，耗时数秒，**不是零消耗**。如需零消耗，可改为 GitHub Actions 先按评审者过滤再转发到 webhook（需要把 webhook URL 存为仓库 secret，参照 `MULTICA_PRUNE_WEBHOOK_URL` 的做法），目前未采用。

## 分派提示词

以下内容原样作为 autopilot 描述（run_only 运行提示词）。

```text
PR 评审通知分派（规则见 AkkiaBot 仓库 docs/review-notify.md）。本运行由 GitHub 仓库 webhook 推送触发时，Trigger payload 即 GitHub 原始 pull_request_review 载荷；手动触发时没有该载荷，执行“补偿扫描”。你只做判定和通知，不改代码、不在 GitHub 上写任何内容、不创建 issue，不在输出或评论中复述 webhook URL 或令牌。下文 $M 指 "$MULTICA_CLI_PATH"。

## 0. 入口
- 有 pull_request_review 载荷：按第 1 步判定；通过后对这一条评审执行“处理流程 P”。
- 没有载荷（手动触发）：补偿扫描。gh pr list -R AkkiaLab/AkkiaBot --state open --json number --limit 100 得到 open PR；对每个 PR：gh api --paginate repos/AkkiaLab/AkkiaBot/pulls/N/reviews，取 submitted_at 在最近 24 小时内、state 不是 PENDING 的评审，逐条按第 1 步的身份规则（第 1 步其余条件视为满足）判定后执行 P。输出每条评审的结果。
- 其他事件或 action：立即结束，不写任何东西。

## 1. 判定（只看载荷，不调用工具），任一不满足立即结束，不写任何东西
- X-GitHub-Event = pull_request_review，action = submitted；
- repository.id = 1457915068 且 repository.full_name = AkkiaLab/AkkiaBot；
- review.user.id ≠ 356702926 且 sender.id ≠ 356702926（akkiabot-reviewer[bot]，小队 Reviewer，沿用其自身交接）；
- pull_request.state = open（closed / 已合并 PR 只在输出说明，不通知）。
记下 N = pull_request.number，RID = review.id，T = review.submitted_at，评审者 review.user（login、id、type）、review.state、review.html_url、review.body。

## 处理流程 P（每条评审独立执行；每一步都可安全重做）

### P1. 评审内容（只读）
gh api --paginate repos/AkkiaLab/AkkiaBot/pulls/N/reviews/RID/comments --jq '.[] | {id, in_reply_to_id}'
- C = 行级评论数，R = 其中 in_reply_to_id 非空的数量；B = review.body 去掉空白。
- B 为空且 C = 0：结束（无须处理）。
- 评审者 id = 68485070（AkkiaS7）且 B 为空且 C = R：结束（实现者在线程中的回复）。

### P2. 找到关联任务
- 候选编号：PR 标题中的 AKKIA-<n>（不区分大小写）、head 分支中的 akkia-<n>、PR 正文中紧跟 Closes/Fixes/Resolves 的 AKKIA-<n>，去重（补偿扫描时用 gh pr view N -R AkkiaLab/AkkiaBot --json title,headRefName,body 取得）。
- 对每个候选：$M issue get AKKIA-<n> --output json；$M issue pull-requests AKKIA-<n> --output json。pull_requests 中存在 repo_owner = AkkiaLab、repo_name = AkkiaBot、number = N 且 link_source 非空的条目才算已关联。
- 恰好一个已关联任务才继续（记为 ISSUE，取 id、identifier、status、assignee_type、assignee_id）；零个或多个：结束，输出原因。

### P3. 进度状态（不是闸门）
$M issue metadata get <ISSUE id> --key github_review_RID --output json
- 值以 done: 开头：本评审已完成，跳到 P6。
- 报 not found（退出码 1）、或值以 commented: 开头：继续 P4（commented 表示上次中断，按下文查找复用，不要重复发）。
- 其他错误：结束并输出错误，不写入。

### P4. 记录评论（不 @ 任何人；先查后发、发后收敛）
- MARK = 首行文本 “[review-notify] PR #N review RID”。
- 查找：$M issue comment list <ISSUE id> --roots-only --summary --since <T 减 1 分钟，RFC3339> --output json，取 content 以 MARK 开头的评论。
- 没有：在工作目录写文件，首行为 MARK，其后写：评审者 login（type）、状态、review.html_url、行级评论 C 条（其中线程回复 R 条）、有正文时附前 300 字摘录（代码块包裹）、处理方式（已唤醒 / 原任务已关闭未唤醒 / 负责人不是实现智能体未唤醒，按 P5 的判断填写）。然后 $M issue comment add <ISSUE id> --content-file <文件> --output json，记下自己发的评论 id。
- 收敛：等待 5 秒后按同样方式重新查找；只看 author_type = agent 且 author_id = 本运行智能体自身（环境变量 MULTICA_AGENT_ID）、content 以 MARK 开头的评论，按 (created_at, id) 最小者为 CID，其余这类评论全部 $M issue comment delete <评论 id>（不论是哪次运行发的；不删其他作者的评论）。
- $M issue metadata set <ISSUE id> --key github_review_RID --value commented:CID --type string --output json

### P5. 唤醒实现者（先查后建、建后收敛）
- ISSUE.status 为 done 或 cancelled：不唤醒、不改状态，W = none。
- assignee_type ≠ agent，或 assignee_id = 60f2a233-d86f-4c9a-aa0e-63e2592433bd（小队 Reviewer Ginoza）：不唤醒，W = none。
- 否则 TARGET = assignee_id。候选 = $M issue wakeup list <ISSUE id> --output json 中 enabled = true、kind = at、fire_count = 0、agent_id = TARGET、source_agent_id = 本运行智能体自身（MULTICA_AGENT_ID）且 instruction 以 “[review-notify]” 开头的规则。
- 没有候选：在工作目录写指令文件，内容：
    [review-notify] PR #N 收到新的评审意见（见本 issue 上 [review-notify] 记录评论）。开始前先执行 "$MULTICA_CLI_PATH" issue wakeup list <ISSUE id> --output json，把其中 enabled、kind=at、fire_count=0、agent_id 为你自己且 instruction 以 [review-notify] 开头的其他规则逐条 issue wakeup delete（它们对应的评审已提交，由本次一并处理）。然后按“处理评审意见”流程处理该 PR 上全部未解决的评审线程（含 Copilot 和人类）：修复的回复 commit 并标记解决，不采纳的回复理由；处理完按规则请 Reviewer 复审。若已无未处理意见，简短说明后结束。
  然后 $M issue wakeup create <ISSUE id> --agent-id TARGET --kind at --after 2m --instruction-file <文件> --output json，
- 收敛（有候选或刚新建后都执行）：等待 5 秒后重新列出候选；按 (created_at, id) 最小者为 W，其余候选全部 $M issue wakeup delete <ISSUE id> <规则 id> --output json（不论是哪次运行创建的；不删不在候选范围内的规则）。
- 若此时没有任何候选（例如刚好已触发），且本次没有新建过：重做一次本步骤。

### P6. 完成与顺带补齐
- 若 P3 判定已 done 且需要唤醒：只执行一次 P5 的“收敛”（不新建），清理可能的中断遗留。
- 未到 done 时：$M issue metadata set <ISSUE id> --key github_review_RID --value done:CID:W --type string --output json
- 然后 $M issue metadata list <ISSUE id> --output json：其他 github_review_<id> 的值不以 done: 开头的，对该评审从 P1 开始补齐：PR 编号取自其 commented: 记录评论首行，评审内容用 gh api repos/AkkiaLab/AkkiaBot/pulls/<编号>/reviews/<id> 取得；该 PR 已关闭时只写 done:<评论 id>:none。

## 结束
不修改 issue 状态和负责人，不修改本自动化配置。输出只写：入口、每条评审的判定结果与原因、ISSUE 编号（如有）、CID 与 W（是否唤醒）。
```

## 并发与恢复验证（2026-11-15，旧工作区 BOT-35，已不可访问）

在 BOT-35 上用真实 Multica CLI 按 P3–P6 的同一套步骤模拟（复审补充的中断场景按“其余候选全部删除”的版本重跑；唤醒延迟改为 12 小时、目标为 Engineer，测完全部删除；记录评论改发在一个评论线程内，因为评论触发的运行不能发顶层评论，webhook 运行不受此限）：

| 场景 | 结果 |
| --- | --- |
| 预检：两次并发 `wakeup create` | 生成两条独立规则，平台不做合并，证实需要收敛步骤 |
| 同一 review id 三路并发（模拟重投） | 三条评论收敛为 1 条，唤醒 1 条，三方都写入相同的 `done:` |
| 三条不同评审同时从零开始 | 并发建出 3 条唤醒，收敛后剩最早 1 条；3 条记录评论各 1 |
| 已有待触发唤醒时又来两条评审 | 复用该唤醒，不新建 |
| 已 `done:` 的评审再次送达 | 直接结束，无写入 |
| 发评论后中断（停在 `commented:`） | 重试复用原评论，补建唤醒后写 `done:` |
| 建唤醒后、写 `done:` 前中断 | 重试复用原评论与原唤醒，无新增 |
| 三路并发，一路建完唤醒后、复查前中断，另一路发完评论后中断（复审补充） | 存活运行删掉中断者的唤醒；两次补偿重试复用中断者的评论；最终唤醒 1 条、每条评审评论 1 条 |
| A 完成后才出现中断者遗留的唤醒（A 看不到），再来一次分派运行 | 新运行删除遗留规则，保留最早的一条 |
| 中断遗留后没有任何后续分派 | 实现者开始前的清理删除其余待触发规则 |

真实 webhook 运行中的失败恢复、补偿扫描和端到端通知，见下一节。

## 端到端验收（2026-11-15，旧工作区 BOT-35，已不可访问）

GitHub hook 与 autopilot 启用后，在 PR #37（关联 BOT-35）和临时的未关联草稿 PR #38 上实测：外部意见路由与唤醒、未关联 PR 不路由、重投去重、补偿扫描补回接入前遗漏的评审、Engineer 删除分派智能体创建的唤醒规则。上述项目全部按预期通过；逐项证据（运行 id、delivery、规则 id）记录在旧工作区，已不可访问。迁入 AkkiaLab 后需重新做一次端到端验收。

已知：Copilot 因额度不足无法评审时，也会提交一条带正文的 review，按现行规则会通知实现者，实现者确认无意见后结束。

## 验收与变更

- 改提示词：先改本文并经 PR 评审，再用 `multica autopilot update <id> --description "<提示词>"` 同步，回读确认一致。
- 轮换 webhook URL：`multica autopilot trigger-rotate-url`，随后用 `gh api -X PATCH repos/AkkiaLab/AkkiaBot/hooks/<hook id>` 更新 hook 的 `config.url`，URL 只经变量传递，不回显。
- 停用：`multica autopilot update <id> --status paused`（GitHub 推送会返回 `autopilot_paused` 并被忽略），或在 GitHub 停用 hook。
