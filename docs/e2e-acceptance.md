# 真实 Telegram 验收（Telegram 测试环境 + 1Password + 用户账号自动化）

本文说明如何对已实现的功能（目前为 M2、M3、M4、M5）做端到端验收：两个测试用户账号通过 MTProto（[Telethon](https://docs.telethon.dev/)）在专用测试群里发命令，工具检查 bot 的真实回复并生成报告。所有 Telegram 交互都在 **Telegram 测试环境**中进行，密钥由 1Password 统一管理。

> 文中括注的 `BOT-<编号>` 是旧工作区的任务编号，已不可访问，只作历史出处；相关结论已写在本文中。

工具代码位于 `tools/e2e`（Python 包 `akkiabot_e2e`，命令 `akkiabot-e2e`）。它只通过 Telegram 黑盒验证，不导入产品代码；产品代码也不依赖它（import-linter 契约 `e2e-black-box`、`product-without-e2e`）。

## 1. 环境隔离：只使用 Telegram 测试环境

Telegram 测试环境与生产环境是两套独立的系统，账号、群、bot、BotFather 和 token 互不相通。验收一律在测试环境进行，不连接生产环境的账号、群或 BotFather。

| 层 | 测试环境端点 | 检查方式 | 无法使用时 |
| --- | --- | --- | --- |
| MTProto（测试账号） | 测试数据中心：DC1 `149.154.175.10` / `149.154.175.40`、DC2 `149.154.167.40`、DC3 `149.154.175.117`（DC1 的第二个地址来自测试服务器 `help.getConfig` 的公告，Telethon 在 `PHONE_MIGRATE` 后使用它，例如 +1 号码的账号） | 新会话固定从测试 DC2 开始。已有会话的服务器地址必须是测试 DC，否则拒绝使用。每次连接后调用 `help.getConfig`，要求服务器返回 `test_mode=true`；登录时如果因 `PHONE_MIGRATE` 切换了 DC，会再检查一次 | 断开连接并报错，**不回退到生产环境** |
| Bot API（被测 bot） | `https://api.telegram.org/bot<token>/test/<方法>` | 产品配置 `TELEGRAM_TEST_ENVIRONMENT=true` 时，PTB 的 `base_url` 和 `base_file_url` 都使用 `/test` 端点；`run --serve` 启动的 bot 强制开启该配置。运行前，工具先在测试端点调用 `getMe`，确认 token 有效且用户名与 `bot_username` 一致 | 生产 token 在测试端点上无效，工具会报错退出，**不会改用生产端点** |
| bot 来源 | 测试环境的 @BotFather | `bootstrap-bot` 只通过已确认处于测试环境的账号与 BotFather 对话 | 同上 |

回归测试覆盖以下情况：
- 生产会话或无效会话会被拒绝；
- 服务器不处于测试模式或地址不对时，会断开连接；
- Bot API 只请求 `/test/` 端点，生产 token 只会报错，不会转而请求生产端点；
- 被测 bot 的运行环境强制为测试环境；
- 产品在默认配置下仍使用生产端点。

相关测试见 `tools/e2e/tests/test_test_environment.py` 和 `apps/server/tests/test_test_environment.py`。

`just e2e-check-env` 不需要账号，只连接测试环境并确认 `test_mode=true`，可用来检查网络和 api_id 配置。

## 2. 能做什么、不能做什么

| 可以自动化 | 需要人工 / 不覆盖 |
| --- | --- |
| 测试账号登录后，通过测试环境的 BotFather 创建测试 bot，并把 token 和用户名写回 1Password | 第三方客户端**不能注册新账号**（Telethon 的 `sign_up` 已被 Telegram 禁止）。测试环境账号需要维护者用官方客户端的测试模式注册，首次登录时由维护者输入验证码和两步验证密码 |
| 建立或复用专用的私有测试超级群，邀请 bot 并授予管理员权限；新群的 id 写回 1Password | 真实 Turnstile 挑战的完成及其后的解除禁言：需要真人在浏览器中完成。工具不求解真实挑战；`m7.captcha`（staging 的真实 widget）把它列为“未自动化”，不计入通过。本地 `m7.captcha-test-keys` 用 Cloudflare 官方测试 key 跑通的通过/拒绝链路是另一类证据（6.4 节），不能替代它 |
| 普通成员账号发送 `/help`、`/id`、回复群主消息的 `/id`、`/ip 1.1.1.1`，以及回复带标签 IP 的消息执行 `/ip` | 清理历史消息、删除常驻测试群：工具从不执行。唯一例外是 6.3 节按授权范围每周清理旧的 staging 发版验收群 |
| 群主在群内发 `/config`，用深链在与 bot 的私聊中打开配置菜单并点击开关按钮；群主回复成员消息执行 `/warn`、`/ban`，检查封禁状态和历史消息删除，然后解除封禁、由群主把成员拉回群 | 开关回调被越权调用（例如伪造按钮数据）：用户账号只能点击自己收到的按钮，由单元测试覆盖 |
| 检查回复文本和行内代码实体（确认 MarkdownV2 已正确渲染，没有残留反斜杠） | |
| 成员退群后再入群，检查两条系统消息都被 bot 删除 | |
| `--serve` 模式下检查 `message_log` 中有对应记录 | |
| （M7，仅本地 `--serve`，官方 Turnstile 测试 key）同一次入群中：固定失败的 widget 不调用接口、Siteverify 拒绝时返回 `turnstile_failed`，两者都保持禁言；固定通过时解除禁言并关闭 Mini App（6.4 节） | |
| （M7，staging 发版）成员入群被禁言 → 群内提示 → 私聊深链 → 以成员账号打开 Mini App（Telegram 签发的真实 initData）→ 打开页面 → 带 Mini App 来源调用验证接口，检查 CORS、未通过 Turnstile 时返回 `turnstile_failed` 且成员保持禁言；结束后关闭入群验证并恢复成员 | |

普通 CI 不会连接 Telegram：单元测试全部使用内存中的模拟 transport（`akkiabot_e2e.testing`）。

## 3. 1Password：权限边界与字段

- 所有环境共用一个 vault `AkkiaBot`（取代此前的独立测试 vault `AkkiaBot-Staging`）。条目按 `<环境>-<用途>` 命名：`e2e-*` 是 Telegram 测试环境验收专用，`staging-*` 同时供本地开发（dev）和预发布环境使用，`prod-*` 是生产环境专用。
- Cloudflare：dev/staging 共用一个 token（`staging-cloudflare/api_token`，包含 DNS、Tunnel、Pages、Turnstile 权限），prod 使用另一个 token（`prod-cloudflare/api_token`）。
- **权限边界**：
  - 服务账号对 vault **可以读写**，工具会自动写回会话、测试 bot token、群 id 等配置。
  - 测试和预发布密钥只在维护者之间经受控渠道共享。
  - **仓库是公开的**：仓库、PR、CI 日志和工具输出中都不能出现任何凭据，凭据值也不能出现在命令行参数中。
  - 本工具只读写 `e2e-*` 条目，引用 `staging-services`，从不涉及 `prod-*`。
- 工具读写 vault 的流程：
  1. 把条目 JSON 读进内存，替换要更新的字段；
  2. 把 JSON **通过标准输入**交给 `op item create -` 或 `op item edit`；
  3. 用 `op read` 回读，并在内存中比对（1Password 是最终一致的，回读最多重试 5 次）。

  凭据值不会进入命令行参数、终端输出或报告；`op` 的错误输出会先脱敏再显示。这一流程已在 1Password CLI 2.42 上实际验证。

### 条目一览

| 条目 | 用途 |
| --- | --- |
| `e2e-telegram` | 用户账号自动化（见下表） |
| `e2e-bot` | 验收用测试环境 bot：`bot_token`（`just e2e-bootstrap-bot` 写入）、`bot_username` |
| `staging-services` | dev/staging 共用的第三方服务配置（见下表） |
| `staging-bot` | 预发布环境的 bot：`bot_token`、`bot_username`、`environment`（与 `e2e-bot` 不是同一个 bot） |
| `staging-cloudflare` | dev/staging 共用的 Cloudflare：`api_token`、`account_id`，以及预发布 tunnel 的 `cloudflared_token`、`tunnel_id` |
| `prod-cloudflare` | 生产环境 Cloudflare：`api_token`（维护者填写）、`account_id` |
| `prod-bot` | 生产 bot：`bot_token`（维护者填写） |

### 条目 `staging-services`（dev/staging 共用的第三方服务）

| 字段 | 类型 | 内容 | 由谁写入 |
| --- | --- | --- | --- |
| `sentry_dsn` | 密码 | Sentry 测试项目 `akkia/akkiabot-staging` 的 DSN；为空时不启用 Sentry | 已写入 |
| `ipinfo_token` | 密码 | ipinfo token（M3 的 `/ip` 使用，对应配置 `WHOIS_IPINFO_TOKEN`） | 已写入 |
| `llm_base_url`、`llm_model` | 文本 | M5 反垃圾使用的 OpenAI 兼容接口地址与模型（`LLM_BASE_URL`、`LLM_MODEL`） | 已写入 |
| `llm_api_key` | 密码 | 上述接口的 API key（`LLM_API_KEY`）；为空时 M5 场景记为跳过（不计为通过） | 已写入 |
| `langfuse_public_key`、`langfuse_secret_key` | 密码 | Langfuse 测试项目的密钥（`LANGFUSE_*`）：bot 用于上报，验收工具用同一组密钥只读查询 `antispam.classify` 记录 | 已写入 |
| `langfuse_host` | 文本 | Langfuse 地址；为空时为 Langfuse Cloud（`https://cloud.langfuse.com`） | 已写入 |

### 条目 `e2e-telegram`（用户账号自动化）

| 字段 | 类型 | 内容 | 由谁写入 |
| --- | --- | --- | --- |
| `api_id`、`api_hash` | 文本 / 密码 | [my.telegram.org](https://my.telegram.org) 中的 API 凭据（同一组凭据也可用于测试环境） | 已写入 |
| `environment` | 文本 | 固定为 `test`，用于标识本条目 | 已写入 |
| `admin_session`、`member_session` | 密码 | 两个测试环境账号的会话 | `just e2e-login admin\|member` |
| `allowed_user_ids` | 文本 | 两个测试账号的 user id，逗号分隔 | `just e2e-login` 自动合并 |
| `allowed_chat_ids` | 文本 | 测试群的 id，逗号分隔 | `just e2e --create-group` 建群后立即自动合并 |
| `pending_group_operation` | 文本 | 未完成的建群操作标识；平时为空 | `just e2e --create-group` 建群前写入、完成后清空（见 6.1 节）；字段不存在时自动创建，`op run` 模板不引用它 |

所有字段都已预先建好，还没有值的字段为空。`op run` 引用不存在的字段时会直接失败，但空字段可以正常注入（已验证）。缺少必需的值时，`run` 以退出码 77 跳过，不发送任何消息。

引用模板只包含 `op://` 路径，已提交在仓库中：
- `tools/e2e/staging.env.tpl`：完整验收和创建 bot 时使用；
- `tools/e2e/login.env.tpl`：只需要 `api_id`、`api_hash` 的命令使用。

## 4. 每台机器的首次初始化

1. 安装 [uv](https://docs.astral.sh/uv/) 和 [just](https://github.com/casey/just)，克隆仓库后执行 `just setup-py`。
2. 安装 1Password CLI（`op` 2.x，见[安装说明](https://developer.1password.com/docs/cli/get-started/)），执行 `op --version` 确认安装成功。
3. 注入服务账号令牌（**每台机器只需一次**）：执行 `just e2e-token-init`，按提示粘贴令牌。输入不回显，也不会进入 shell 历史或命令行参数。
   - 令牌保存在 `~/.config/akkiabot/op-service-account-token`（目录权限 700，文件权限 600）；可以用环境变量 `AKKIABOT_OP_TOKEN_FILE` 修改路径。
   - 读取时会检查文件属主和权限，权限过宽时拒绝使用。
   - 之后的 `just e2e*` 命令会读取该文件，通过环境变量 `OP_SERVICE_ACCOUNT_TOKEN` 传给 `op run`，令牌不会出现在命令行参数中。`op run` 只把解析出的值注入子进程环境，不会写成明文配置文件。
   - 轮换令牌后执行 `just e2e-token-init --force`。撤销某台机器的访问权限时，先在 1Password 中删除对应的服务账号，再删除该文件。
4. 执行 `just e2e-check-env`，确认能连上测试环境。

## 5. 首次准备（需要维护者配合一次）

### 5.1 用官方客户端在测试环境注册两个账号

测试环境的账号与生产账号相互独立，可以用维护者自己的手机号注册。需要两个账号：
- 群主/管理员：建群、授权、创建 bot、发锚点消息；
- 普通成员：发命令。

以后的入群验证、`/ban`、`/warn` 等场景也沿用这两个角色。

第三方客户端不能注册新账号，所以要先用官方客户端的测试模式注册。常见入口如下（以 Telegram 官方客户端的实际界面为准）：

- Telegram Desktop：打开设置，按住 `Shift` + `Alt` 并右键点击“添加账号”，选择 Test Server，再用手机号注册。
- iOS：连续点击“设置”标签约 10 次，打开调试菜单，依次选择 Accounts → Login to another account → Test。

注册完成后，请保持该官方客户端处于登录状态，测试环境的验证码会发到这里。

### 5.2 让工具登录这两个账号

```bash
just e2e-login admin    # 群主/管理员账号
just e2e-login member   # 普通成员账号
```

按提示输入手机号、官方客户端（测试模式）收到的验证码，以及两步验证密码（密码输入不回显）。验证码和密码只在终端里输入，**不要发到 issue 或评论里**。登录成功后：

- 会话直接写入 `e2e-telegram/<role>_session`，并经过回读校验；终端不会显示会话内容。
- 账号的 user id 自动合并到 `allowed_user_ids`。
- 如果写入 1Password 失败，会话会暂存到 `~/.config/akkiabot/e2e/<role>.session`（权限 600）。排除故障后执行 `just e2e-session-store <role>`，写入成功后该文件会被覆盖并删除。

如果会话失效（例如在官方客户端的“设备”中被终止），运行时会提示“会话已失效”，重新执行本节即可。

### 5.3 自动创建测试 bot 和测试群

```bash
just e2e-bootstrap-bot      # 群主账号在测试环境的 @BotFather 创建 akkiabot_e2e_xxxxxx_bot，并写回 bot_token / bot_username
just e2e --create-group     # 首次建群：群 id 立即写回 allowed_chat_ids，再邀请 bot、授予权限、由群主把成员拉入群，然后执行全部场景
```

建群可能在中途被打断，工具按 6.1 节的顺序记录和恢复，重跑不会建出第二个群。

`bootstrap-bot` 可以重复执行：如果 1Password 中的 token 已能通过测试端点 `getMe` 的校验，就不会再创建新 bot；确实需要新建时加 `--force`。BotFather 的回复中含有 token，它只保存在群主测试账号与 BotFather 的私聊里，工具不会打印。

## 6. 日常运行

```bash
just e2e --scenarios m4.ban # PR 级：只跑改动明确涉及的场景（可写里程碑或场景名，逗号分隔）
just e2e                    # 复用测试群，执行全部场景（一般不需要，全量留给 staging 发版）
uv run akkiabot-e2e list    # 列出所有场景
```

#### PR 级验收的场景选择

按分层验收（`AGENTS.md`），单个需求 / 修复的 PR 只验证**改动明确涉及**的场景，规则适用于所有场景：

- 只执行改动明确涉及的场景，例如改了 `/ban` 的处理逻辑就跑 `m4.ban`。
- 无法判断是否受影响时，默认跳过。共享配置、handler 注册、依赖升级等，也按不受影响处理，除非确实改到了相关场景的代码。
- PR 级不为“可能有影响”去补跑。staging 发版同样只按差异验收（6.2 节），间接影响不会被自动补跑；需要时由发版者执行全量（6.2 节）。
- PR 描述中列出“已验证的场景”（附报告）和“未验证、留给 staging”的部分。
- 报告中未选中的场景记为跳过（`skipped`，原因“未选中（本次改动未涉及）”），不计为通过。

PR 级运行（`just e2e`，即 `--serve`）**不新建群**：只复用固定测试群（`E2E_CHAT_TITLE`，默认“AkkiaBot E2E 自动验收群”），普通成员常驻群内；`--serve` 与 `--fresh-group` 同用时直接报错退出。`--create-group` 只用于首次配置固定测试群（5.3 节）。在固定群里，只有选中 `m7.captcha` 或 `m2.service-message` 时，成员账号才会通过邀请链接入群（`m7.captcha-test-keys` 由群主拉人触发入群验证，不占用链接入群）；选中 `m2.join-request` 时，成员用需审批的链接提交一次入群申请，申请被拒绝后由群主拉回，不入群。

`just e2e` 的执行步骤：

1. 在测试端点上用 `getMe` 校验 bot token。
2. 用当前检出的代码在本机启动被测 bot（执行 `akkiabot migrate` 和 `akkiabot serve`，即 `--serve` 模式）：
   - 强制设置 `TELEGRAM_TEST_ENVIRONMENT=true`；
   - 在运行目录中使用全新的 SQLite 数据库和进程内缓存；
   - HTTP 和指标端口只绑定 `127.0.0.1`，通过长轮询接收更新；
   - bot 进程的环境中**不包含**测试账号会话和 `OP_*` 变量。

   请确认没有其他实例在用同一个测试 bot token 轮询，否则会互相抢更新。
3. 登录两个测试账号（必须连接测试环境），并检查：
   - 两个账号都在 `allowed_user_ids` 中；
   - 两个账号是不同的真实用户；
   - `bot_username` 对应的确实是 bot。
4. 选择测试群。只考虑**群主账号创建的、标题与 `E2E_CHAT_TITLE`（默认“AkkiaBot E2E 自动验收群”）完全一致的超级群**，并且群 id 必须在 `allowed_chat_ids` 中。同名但不在白名单中的群不会被使用，也不会因为同名而被认领。只有带 `--create-group`、且不存在白名单外的同名群时才会新建群（见 6.1 节）。
5. 准备测试群：
   - 邀请 bot，确保它有 `delete_messages`、`ban_users`、`invite_users` 管理员权限（手动授予的其他权限会保留；`invite_users` 是收到入群申请所必需的，BOT-33 起已有的固定测试群会在准备阶段自动补上）；
   - 普通成员如果在封禁列表中（上次 M4 / M5 运行中断遗留），先解除封禁；
   - 两个测试账号互加联系人（已是联系人时跳过）。Telegram 默认只允许联系人把用户拉进群，而且把退出或被移出超级群的用户再拉回来，要求双方是双向联系人（否则报 `USER_NOT_MUTUAL_CONTACT`）。联系人关系长期有效；首次加联系人时两个账号需要同在一个群里（例如固定测试群），否则账号拿不到对方的信息，工具会报错说明；
   - 普通成员不在群里时（包括新建的群），由群主账号直接把成员拉入群（`channels.inviteToChannel`），不使用邀请链接。拉人失败（例如 `USER_PRIVACY_RESTRICTED`、`PEER_FLOOD`，或 Telegram 在 `missing_invitees` 中返回该成员）时如实报错并停止，**不回退为链接入群**。
6. 依次执行各场景并收集证据，最后核对 `message_log`。

### 6.1 建群的中断恢复

建群涉及 Telegram 和 1Password 两个系统，不能原子完成。`--create-group` 按以下顺序执行，任何一步中断后都可以安全重跑：

1. 生成一个建群操作标识（12 位十六进制），写入 1Password 的 `e2e-telegram/pending_group_operation` 并回读。**写入失败时直接停止，不创建任何群。**
2. 创建超级群，把 `[akkiabot-e2e op:<操作标识>]` 写进群简介（about），作为该群属于这次操作的凭证。
3. 群创建后立即把群 id 合并进 `allowed_chat_ids`，然后才邀请 bot、授权和拉成员入群。
4. 清空 `pending_group_operation`。清空失败不影响本次运行，报告中会注明；残留的标识只会匹配带有同一标识的群，下次建群时沿用它即可。

部分成功时的表现与恢复：

| 中断位置 | 留下的状态 | 重跑 `just e2e --create-group` 的行为 |
| --- | --- | --- |
| 第 1 步之后、建群之前 | 有待恢复标识，没有新群 | 沿用同一个标识新建群 |
| 建群之后、写入白名单之前（进程被杀或 1Password 写入失败） | 有待恢复标识，有一个不在白名单中的同名群 | 在白名单外的同名群里查找简介带有该标识的群：**恰好一个**时写入白名单并继续，不再新建 |
| 写入白名单之后 | 群已在白名单中 | 按正常流程复用 |

1Password 写入失败时，运行以失败结束，错误信息中写明新群的 id 和操作标识，并提示不要删除该群。不加 `--create-group` 重跑时，工具不会写白名单，只提示加 `--create-group` 恢复。

无法唯一确认时一律停止，不认领、不新建、不删除：

- 白名单外存在同名群，但 1Password 中没有待恢复标识；
- 白名单外的同名群中，没有一个带有待恢复标识，或有多个带有该标识。

这时请用群主测试账号的官方客户端确认哪个是本工具创建的测试群，把它的 id 写入 `allowed_chat_ids`，再清空 `pending_group_operation` 后重跑。多余的群是否删除需另行确认，工具不会自动删除。

本节之前创建的测试群简介中没有操作标识，它们已在白名单中，仍按正常流程复用。

故障注入测试见 `tools/e2e/tests/test_group_recovery.py`（使用模拟 transport 和内存白名单存储，不连接 Telegram 和 1Password）。

| 场景 | 内容 |
| --- | --- |
| `m5.spam` | 成员先发一条普通消息（清理探针），群主用 `/config` 开启“反垃圾”；成员再发一条明显的博彩广告：`E2E_ANTISPAM_TIMEOUT`（默认 60）秒内广告被删除，成员被禁言，bot 发出带成员提及的“内容命中反垃圾策略”通知，提及只显示名字片段（BOT-37）；随后 `E2E_ANTISPAM_PURGE_DELAY + E2E_REPLY_TIMEOUT` 秒内探针作为成员历史被清理。报告记录广告从发出到删除的耗时。结束时（无论成败）先确认反垃圾的异步处理已经结束（见下文“反垃圾的异步副作用”），再解除禁言 |
| `m5.disabled` | 关闭“反垃圾”后，成员再发同样的广告：等待 `E2E_ANTISPAM_WAIT`（默认 20）秒后消息仍在、成员未被禁言 |
| `m5.exempt` | 重新开启“反垃圾”，成员先发三条普通消息，再发广告：作为发言超过 3 条的老成员被豁免（同样等待后检查）。结束时关闭“反垃圾” |
| `m5.langfuse` | 用 Langfuse 公共 API 查询本次运行之后的 `antispam.classify` generation，`m5.spam` 的广告（按消息中的运行标记匹配）必须有记录。未配置 `LANGFUSE_*` 时记为跳过（不计为通过） |
| `m2.help` | `/help` 返回命令列表（包括 `/help`、`/id` 及其用法） |
| `m2.id` | `/id` 返回“当前ChatId: 群名 (群 id)”和“你的UserId: 名字 (user id)”；id 显示为行内代码，没有残留反斜杠 |
| `m2.id-reply` | 回复群主的消息执行 `/id`，额外返回“回复目标的UserId” |
| `m3.ip` | `/ip 1.1.1.1` 返回 IP、Location、AS、Org、ISP 五个字段，每个值都显示为行内代码，且都不是“未知” |
| `m3.ip-reply` | 回复“服务器地址：1.1.1.1:443”这条消息执行 `/ip`，要求同上 |
| `m4.permissions` | 普通成员发 `/config` 被拒绝；普通成员回复群主消息发 `/ban` 被拒绝；群主对自己的消息发 `/ban` 得到“无法封禁管理员或群主。”且消息未被删除 |
| `m4.config` | 群主在群内发 `/config`，回复带深链按钮；用私聊发送 `/start <token>` 打开菜单（含反垃圾、入群验证、警告、删除系统消息四个开关）；点击“警告”，回调提示“设置成功”且菜单刷新为新状态。若初始为开启则先关再开，最终保持开启 |
| `m4.warn` | 成员发两条消息，群主回复其中一条连续 `/warn`：回复依次显示 `1/3`、`2/3`，第 3 次显示已封禁，每次都只显示名字片段；检查成员在封禁列表中、两条消息已被删除。开关未开启时先走一遍 `m4.config` 的流程。结束时（无论成败）解除封禁并由群主把成员拉回群 |
| `m4.ban` | 成员发三条消息，群主回复最后一条 `/ban`：回复显示删除条数不少于 3、名字渲染为提及链接且只显示名字片段（较长名字不出现全名，也不出现 `@username`，BOT-37）；检查成员在封禁列表中、三条消息已被删除。结束时同样恢复成员 |
| `m5.spam-command` | 成员回复群主消息发 `/spam` 得到“无法举报管理员或群主。”；群主回复成员消息发 `/spam`，回复“已被封禁（垃圾信息）”，成员进入封禁列表、消息被删除。结束时恢复成员 |
| `m2.join-request` | 入群**申请**阶段（不是入群后的验证）：成员退群，群主导出一个需要管理员审批的邀请链接，成员用它申请入群（Telegram 返回“申请已发送”，成员不入群）。在管理员处理之前，成员必须在与 bot 的私聊中收到与旧版一致的等待提示 `Hello, please wait for the admin to approve your request.`（只认申请之后的新消息），并且成员仍不在群里（bot 不得自行批准）。结束时（无论成败）群主拒绝该申请，再把成员直接拉回群。申请不算链接入群，报告中单独记为“成员通过需审批的链接申请入群” |
| `m2.service-message` | 成员退群后通过邀请链接重新入群（要验证的就是真实入群，所以保留链接入群），两条系统消息都被 bot 删除（此场景最后执行）。如果 Telegram 没有返回其中任一条系统消息的 id，记为失败（未验证），不算通过 |
| `m2.message-log` | 仅 `--serve` 模式：群主发出的普通消息出现在 `message_log` 中 |

M3 合并到 master 之前，在 master 上运行 `m3.*` 会失败（bot 还没有 `/ip` 命令）。这时请用 `--scenarios m2`，或在 M3 的分支上运行。同理，M4 合并前在 master 上运行 `m4.*` 会失败。

M5 场景排在最前面：反垃圾只检查已记录发言不超过 3 条的成员，而其他场景都会让成员发言。`--serve` 每次使用新的数据库，所以完整运行时成员在 `m5.spam` 时一定是“新成员”；不加 `--serve`、复用数据库长期保留的外部 bot 时，加 `--fresh-group` 让每次运行使用新建的专用群（`just e2e-staging` 已默认加上），否则成员在旧群中的发言数会累计，`m5.spam` 会因豁免而失败（见 6.2 节“持久化被测 bot 的数据准备”）。被测 bot 没有配置 LLM（`LLM_API_KEY`、`LLM_MODEL`）时，M5 的反垃圾场景记为跳过（⏭️，原因“缺少凭据或配置”），整次运行不算通过；只验收其他里程碑时用 `--scenarios m2,m3,m4`。普通成员举报普通成员需要第三个账号，由单元测试覆盖（`apps/server/tests/test_antispam.py`）。

反垃圾的异步副作用（BOT-47）：bot 在后台处理群消息，LLM 判定最长可用 `ANTISPAM_TIMEOUT`（默认 45 秒，超时则放行），命中后删除广告、禁言并发通知，再过 `ANTISPAM_PURGE_DELAY`（默认 5 秒）清理该成员已记录的全部历史消息（包括这期间新发的消息），通知在 `ANTISPAM_NOTICE_TTL` 后自删。`m5.spam` 因此不以 `E2E_REPLY_TIMEOUT` 判定广告处理，而是等到 `E2E_ANTISPAM_TIMEOUT`（被测 bot 的判定预算加 Telegram 调用余量，改动 bot 的 `ANTISPAM_TIMEOUT` 时须同步调大）；并以探针被清理作为“这次处理已全部结束”的信号。场景失败时同样先确认结束：预算内 bot 一直没有处理广告，视为已放行、不会再有迟到动作，后续场景照常执行；bot 处理了广告但清理没有在窗口内出现，则无法确认副作用已结束，该场景记为失败（同时保留原失败原因），其余场景以“运行中止”跳过，本次运行不会写 `success`。确认过程本身出错（例如读取消息或成员状态时网络、RPC 失败），或结束时解除禁言失败，同样按“未确认结束”中止，报告保留原失败原因。不要用加长固定等待或调换场景顺序来规避：判定慢于 `E2E_REPLY_TIMEOUT` 却仍在预算内时，过早放弃会让迟到的禁言和清理落进后续场景；即使 `m5.spam` 通过，延迟的历史清理也会删掉后续场景刚发的消息。被禁言的成员仍在群里，恢复成员时先解除禁言，不会把它当作已退群再拉人。广告处理耗时超过 `E2E_REPLY_TIMEOUT` 时只在报告中记录，是否需要调整 LLM 延迟由反垃圾的指标（`antispam_llm_*`）和维护者判断，验收工具不改被测 bot 的配置。

`m4.warn`、`m4.ban`、`m5.spam-command` 会真实封禁普通成员账号，并在场景结束时（包括失败时）解除封禁、由群主账号直接把成员拉回群，后续场景不受影响。不用邀请链接的原因：Telegram 按账号限制短时间内通过链接入群的次数（BOT-27、BOT-28），而群主拉人不占用成员账号的入群额度。只有 `m7.captcha`、`m2.service-message` 保留成员通过邀请链接入群，因为这两项要验证的就是真实入群。`m2.join-request` 只用需审批的链接提交申请（申请随后被拒绝，成员由群主拉回），不算链接入群，但同样是成员账号的一次链接操作，报告中单独计数。群主拉人失败时，该场景记为失败，其余场景以“运行中止”跳过，不回退为链接入群。如果运行被强行中断，成员可能停留在封禁状态；下次运行的准备阶段会先解除封禁再由群主拉成员入群，并在报告的准备步骤中注明。

bot 在私聊中回复时不会引用原消息（PTB 的 `reply_text` 在私聊中默认不引用），所以场景在私聊里把 bot 发来的下一条消息当作回复；群内仍要求回复引用了请求消息。

不加 `--serve` 时（执行 `uv run akkiabot-e2e op-exec -- akkiabot-e2e run`），工具只负责发消息和检查结果，被测 bot 需要你另外启动，并且必须设置 `TELEGRAM_TEST_ENVIRONMENT=true`。

### 结果

- 退出码：
  - `0`：全部通过；
  - `1`：有场景失败，或准备阶段失败；
  - `2`：参数、配置或环境检查失败；
  - `77`：**缺少凭据，已跳过**。此时不发送任何消息，报告中写明“未执行（不是通过）”。
- 场景状态：
  - `passed` 实际执行并通过；`failed` / `error` 失败；
  - `skipped` 跳过，并注明原因：未选中（本次改动未涉及）、缺少凭据或配置、限流中止、运行中止（例如群主拉人失败）。跳过**不计为通过**。
  - 报告开头的汇总分开写“实际通过 / 失败 / 跳过（不计为通过）”。准备阶段就失败时，所有场景同样逐项列出：选中的按实际原因（限流中止或运行中止）记为跳过，其余记为未选中；准备阶段已完成的群主拉人也计入入群次数。整次运行判为通过，需要至少一个场景实际通过、没有失败，且跳过的都只是“未选中”；因缺少凭据或限流而跳过的场景会让运行不通过。`--record-status` 更严格：全量时所有场景、差异验收时所有选中的场景都必须实际执行并通过（6.2 节）。
- 入群与限流明细：
  - 报告写明本轮成员通过链接入群的次数和群主拉人的次数（`report.json` 的 `joins`）；
  - 每次 FloodWait 都单独记录时间（UTC）、账号角色、方法、要求等待的秒数、第几次重试，以及是等待后重试还是超过上限中止（`report.json` 的 `flood_waits`，`report.md` 中为表格）。
- 报告：`~/.local/state/akkiabot-e2e/runs/<时间>-<随机串>/report.md` 和 `report.json`（目录权限 700，文件权限 600）。同一目录下还有被测 bot 的 `server.log` 和 SQLite 数据库。`report.md` 可以直接贴到对应 issue，作为验收记录。
- 终端输出经过 `op run` 的掩码处理：从 1Password 引用的值（包括群 id、user id）在终端里显示为 `<concealed by 1Password>`；报告文件中保留了这些标识，便于核对。

### 6.2 staging 发版验收（差异）

按分层验收（`AGENTS.md`），单个任务只跑受影响的场景（`just e2e --scenarios …`）。staging 发版验收针对**已部署的 staging 栈**执行，默认只验收差异（BOT-16 决定，BOT-34 实现）：被测对象是 staging bot（`staging-bot` 条目）、`bot-api-staging.akkia.moe` 和 `staging.akkiabot-webapp.pages.dev`，本机不启动 bot。环境模板为 `tools/e2e/staging-release.env.tpl`。

版本流程（`docs/release.md` §4.1）下，第 1–3 步和第 5 步由 `just release-staging <RC tag> <场景>` 在同一把 staging 锁内完成；以下是它执行的内容和手动执行时的步骤。

1. 被验收的 `SHA` 是当前有效 RC tag `vX.Y.Z-rc.N` 指向的 commit（完整 40 位），由 `release-rc.yml` 创建。
2. 前端：`just release-staging` 在持有 staging 租约时触发 `webapp-deploy.yml`，把该 commit 发布到 `staging.akkiabot-webapp.pages.dev`；租约持续到验收结束，其他 RC 或手动运行在此期间不能覆盖前端（master 推送不再发布 staging）。用 `curl -s https://staging.akkiabot-webapp.pages.dev/version.json` 确认 `sha`。
3. 后端：`just release-staging` 用该 commit 的 `deploy/upgrade.sh` 把预发布栈升级到同一个 `SHA` 的镜像（`docs/operations.md` §8.1）。
4. 确定差异场景：
   - **基线** `BASE`：`SHA` 的祖先中最近一个 `staging-acceptance=success` 的 commit（完整 40 位，`just release baseline SHA`）。只认祖先：补丁版本的 RC 不会以较新常规版本的验收为基线，反之亦然；
   - 查看 `git log BASE..SHA` 和 `git diff BASE SHA` 中的**产品改动**，按 §6“PR 级验收的场景选择”的同一规则，只选改动明确涉及的场景；也可以直接汇总这段区间内各 PR 描述中“已验证的场景”；
   - 没有改加群流程（群主拉人、链接入群、入群申请、入群验证）时，不选 `m7.captcha`、`m2.service-message`、`m2.join-request` 等入群相关场景；
   - 无法判断影响时默认跳过，并在发版记录中如实列出跳过的场景和原因；
   - 至少要有一个场景实际通过才能写 `success`。区间内没有任何场景明确涉及时（例如只改文档、CI），选 `m2.help` 作为最小冒烟（确认部署的 bot 能响应）。
5. 运行：

   ```bash
   just e2e-staging --record-status SHA --baseline BASE --scenarios m2.join-request   # 差异验收
   just e2e-staging --record-status SHA                                               # 全量验收（不加 --scenarios / --baseline）
   ```

   `--record-status` 带 `--scenarios` 时必须同时给出 `--baseline`，反之亦然。工具在连接 Telegram 之前，先用 `gh` 核对基线最新的 `staging-acceptance` 是 `success`，且 `SHA` 相对基线为 `ahead`（基线是祖先、两者不同）；不满足就直接退出。这样通过验收的版本连成一条链：每个 `success` 都表示“相对上一个通过的版本，改动涉及的场景都已在 staging 实际通过”。全量验收仍可随时执行（例如首次发版、依赖大版本升级等发版者判断需要的情况），不需要基线。

   `just e2e-staging` 每次都带 `--fresh-group`（`--record-status` 也会自动启用，差异验收同样如此）：本次运行新建一个标题为“AkkiaBot Staging 发版验收群 <运行标识>”的专用测试群，按 6.1 节的可恢复流程写入白名单，不复用旧群，再由群主把成员拉入新群。判断依据是“是否需要新成员状态”：staging bot 的数据库长期保留，按群累计的成员发言数、各项开关和警告次数会影响 `m5.*`（新成员才会被检查）、`m4.warn`（计数）、`m4.config`（开关初始状态）等多数场景，逐个判断哪些场景不依赖群状态容易出错；而新建群只由群主拉人，不占用成员账号的链接入群额度，所以保持默认开启。原因见下方“持久化被测 bot 的数据准备”。成员账号的链接入群只发生在选中的 `m7.captcha`、`m2.service-message` 中，`m2.join-request` 另有一次入群申请（不入群）；差异验收没有选中它们时，一次链接入群都没有。

   staging 别名可能被另一次 RC 部署或手动运行覆盖，所以工具在取得验收锁**之后**、发送任何消息之前，先核对 staging Mini App 的 `version.json` 就是 `SHA`，不一致就直接退出。然后执行选中的场景（全量时为全部场景，含 M5 反垃圾、M7 Mini App 链路），结束后再核对一次。报告中记录开始和结束时实际看到的版本。结束时版本变了或无法读取，报告判为未通过。结果写为 GitHub commit status `staging-acceptance`，只有满足以下条件才写 `success`，否则写 `failure`：
   - 全量：全部场景实际执行并通过（任何跳过都不算）；
   - 差异：选中的场景全部实际执行并通过（因限流、缺少凭据、运行中止而跳过都不算），未选中的场景记为跳过（原因“未选中（本次改动未涉及）”），同样不计为通过；
   - 两者都要求开始和结束时都是 `SHA`。

   差异验收的报告写明基线 `BASE`、执行的场景和未执行的场景数；status 描述形如“差异验收（基线 2814ea1）：1/1 个选中场景通过（运行 …）；15 个未涉及差异、未执行；场景 m2.join-request”（超过 GitHub 的 140 字上限时截断场景列表，完整列表见报告）。`--record-status` 不能与 `--serve` 同时使用。
6. 把 `report.md` 贴到发版记录（对应的 issue）中，写明 `SHA`、基线、后端镜像、结果，以及按规则跳过的场景和原因。报告中的“未自动化”项要原样保留。
7. 生产：RC 通过后封版并打正式 tag，`release-production.yml` 等待维护者审批，后端由 `just release-deploy production <tag>` 部署，随后自动触发 `webapp-promote.yml -f sha=SHA`，由维护者在 GitHub 中审批 `webapp-production` 部署（`docs/release.md` §4.2–4.3）。只有 `staging-acceptance=success` 的 commit 能通过检查；`success` 可能来自全量或差异验收，差异验收的含义见第 5 步。

写 commit status 需要本机 `gh` 已登录，且对仓库有写权限。

#### 持久化被测 bot 的数据准备

staging bot 的 PostgreSQL 在多次验收之间保留，按群保存的状态会累计：成员发言数（`chat_member.message_count`）、各项开关、警告次数。反垃圾只检查发言不超过 3 条的成员（`ANTISPAM_EXEMPT_MESSAGE_COUNT`），而一次全量运行会让普通成员发言十几条，所以第二次在同一个群里运行时，`m5.spam` 的广告会被豁免：消息不删除、成员不禁言，指标只出现 `antispam_verdict_total{verdict="skipped",action="exempt"}`，`m5.langfuse` 也因没有判定而失败（BOT-27 在 staging 上复现：发版验收群里两个测试账号的计数已达 40 和 42）。

约束：

- 针对持久化被测 bot 的运行一律使用 `--fresh-group`，让每次运行的群内状态与首次运行一致。不要通过清库、删除或重置成员计数等方式“恢复新成员”，也不要为了验收调高豁免阈值或把 `skipped`/`exempt` 当作通过。
- 只验收部分场景时同样适用，例如连续复验反垃圾：`just e2e-staging --scenarios m5`。
- 每次运行留下一个测试群，两个测试账号的已加入群数随之增长（Telegram 对单个账号可加入的群数有上限，普通账号约 500 个）。旧群按 6.3 节每周清理，只保留最近 2 个。
- 建群中断后遗留的、不在白名单中的同名群不会被后续运行认领（每次运行的标题都不同）；1Password 中残留的待恢复标识会被下一次建群沿用，不影响运行。
- 本地 `just e2e --serve` 每次使用新数据库，不需要 `--fresh-group`。

### 6.3 每周清理旧的 staging 发版验收群

经维护者授权（BOT-28/BOT-30），每周清理一次 6.2 节留下的旧验收群；一周内没有 staging 发版就不运行。

**触发**：`.github/workflows/e2e-prune-trigger.yml` 每周一 03:17 UTC 运行（也可在 Actions 页面手动运行）。它只用 GitHub GraphQL API 逐页检查 master 的全部提交，看过去 7 天内是否有 commit 被写入 `staging-acceptance` 状态（不按提交时间截断，重新验收旧提交也算）。API 出错或提交数超出检查上限时，workflow 直接失败，不会当作“无发版”：

- 有，就 POST 调用可选的清理 webhook，由接收方在持有测试账号会话的机器上执行清理。URL 存在 Actions secret `E2E_PRUNE_WEBHOOK_URL` 中，不写入仓库，也不出现在日志里；请求带 `Idempotency-Key: akkiabot-e2e-prune-<ISO 周>`，同一周重跑 workflow 不会重复触发；未配置该 secret 时只输出 notice 并跳过，需要手动清理；
- 没有，就直接结束，不调用 webhook；
- workflow 不接触 Telegram 会话。没有用 `--record-status` 的手动 staging 运行不会被检测到，它们留下的群会在下一次有发版的那周一并清理。

**清理**（只能在持有测试账号会话的机器上执行）：

```bash
just e2e-prune-groups            # dry-run：只列出，不删除
just e2e-prune-groups --apply    # 真正删除
```

- 范围：群主测试账号创建、标题严格为“AkkiaBot Staging 发版验收群 <运行标识>”（运行标识形如 `20261115T012000Z-1a2b3c`）、并且 id 在 `allowed_chat_ids` 中的超级群；
- 按运行标识中的 UTC 时间（即建群时间）保留最近 2 个，作为最近发版的证据；
- 不动常驻测试群（“AkkiaBot E2E 自动验收群”）和其他标题不符合上述格式的群；标题符合但不在白名单中的群（例如建群中断遗留的）不删除，只在输出中列出，按 6.1 节人工处理；
- 删除后从 1Password `e2e-telegram/allowed_chat_ids` 中移除对应 id，并回读校验。中途失败（如限流超过 `E2E_FLOOD_MAX_WAIT`）时立即停止，已删除的群仍会从白名单中移除，退出码为 1；
- 输出只包含数量和运行标识，不包含群 id；
- 与 `run` 使用同一把验收锁，不会与正在进行的验收并发。

执行时先 dry-run，确认结果只包含上述范围内的群，再执行 `--apply`，两次输出（数量和运行标识）记录到受控的内部记录中。

### 6.4 本地官方测试 key 入群验证（`m7.captcha-test-keys`）

入群验证有三类证据，互相不能替代，报告和 PR 中分开记录：

| 证据 | 入口 | 证明什么 | 不证明什么 |
| --- | --- | --- | --- |
| mock 单测 | `just test`（`apps/server/tests/test_captcha.py`、`test_turnstile.py`，respx 模拟 Siteverify；`apps/webapp/tests`） | 接口校验顺序、各种拒绝原因、解除禁言的业务规则、页面各状态 | 真实 widget、真实 Siteverify、Telegram 中的链路 |
| 官方测试 key 端到端 | `just e2e-turnstile-test-keys`（本节） | 真实 Turnstile widget（无头 Chromium）→ 本地构建的 Mini App → 本机被测 bot 的接口 → Cloudflare 线上 Siteverify → Telegram 测试环境中解除或保持禁言 | 真实挑战能被真人通过；staging/prod 的 site key、secret key、域名配置是否正确 |
| staging 真实 widget | `just e2e-staging`（`m7.captcha`，6.2 节）及按需的人工点击 | 部署环境的 Mini App、CORS、真实 key；拒绝路径自动化，真实挑战的通过需要真人 | — |

**运行**（每台机器先执行一次 `just e2e-browser-setup`：下载 Playwright 的 chromium-headless-shell，并用 sudo 安装其系统库）：

```bash
just e2e-turnstile-test-keys    # = just e2e --scenarios m7.captcha-test-keys
```

- 只能与 `--serve` 一起使用，只能按场景名选中：默认全量、`--scenarios m7` 和 staging 发版验收（`just e2e-staging`、`--record-status`）都不包含它（差异验收带 `--scenarios` 选中它时直接报错），因此不影响 `staging-acceptance` 的“选中场景全部实际执行”要求。
- 工具用当前检出的 `apps/webapp` 构建两份 Mini App（`vite build`，构建环境只含 `PATH`、`HOME` 等和两个 `VITE_*` 变量，不含运行密钥）：固定通过的 site key `1x00000000000000000000AA`（作为 bot 的 `WEBAPP_URL`）和固定失败的 `2x00000000000000000000AB`，分别在 `127.0.0.1` 的随机端口上提供；`CORS_ALLOW_ORIGINS` 包含这两个来源。Telegram 测试环境允许 Mini App 使用不带 TLS 的 HTTP 地址。
- 被测 bot 先用固定失败的 secret key `2x0000000000000000000000000000000AA` 启动。成员由群主拉回群（触发入群验证，不使用邀请链接），在私聊中 `/start`，工具以成员账号取得 Telegram 签发的启动地址（真实 initData），然后在无头 Chromium 中按 Telegram 客户端的方式打开（补上客户端附加的 `tgWebAppThemeParams`，注入记录事件的 `TelegramWebviewProxy`）：
  1. 固定失败的 site key：widget 报错，页面显示“人机验证组件加载失败”，**没有**调用验证接口，成员保持禁言；
  2. 固定通过的 site key + 固定失败的 secret key：widget 给出 dummy token，Siteverify 拒绝，接口返回一次 `400 turnstile_failed`，页面显示“人机验证未通过”，成员保持禁言；
  3. 以固定通过的 secret key `1x0000000000000000000000000000000AA` 重启被测 bot（同一数据库，challenge 仍有效），再打开页面：接口返回 200，页面显示成功并发出 `web_app_close`，成员被解除禁言。
- 报告中以“官方测试 key（不代表真实挑战通过）”单独列出所用 key、本地来源和浏览器版本；`m7.captcha` 的“未自动化”条目不受影响。
- 这些 key 是 Cloudflare 公开的测试值，只在本地运行的环境变量和构建产物中出现；不写入 1Password，也不改动 staging/prod 的 Turnstile 配置、Pages 构建变量或发布门禁。

**清理**：场景结束时关闭入群验证、解除禁言并确保成员仍在群内（与 `m7.captcha` 相同）；本地 bot、静态站点和浏览器随运行退出。构建产物、`webapp-build.log`、`server.log` 留在运行目录（`~/.local/state/akkiabot-e2e/runs/<运行 ID>/`，权限 700），与其他报告一样可以直接删除。

## 7. 安全约束

- **公开仓库中不出现任何凭据**：仓库只提交 `op://` 引用模板。真实的凭据、会话和验证码不提交到仓库，也不出现在 PR、CI 日志或工具输出中；测试凭据只在维护者之间经受控渠道共享。
- **命令行参数中不出现任何凭据**：令牌通过环境变量传给 `op`，写入 vault 的值通过标准输入传递。不要使用 `op item edit ... "field[password]=值"` 这类赋值参数。
- **显式脱敏**：工具在输出和写入报告前，会把已知密钥（会话、`api_hash`、bot token、ipinfo token、Sentry DSN、服务账号令牌）替换为 `[REDACTED]`；`Secret` 类型的 `repr` 不显示原值。这符合仓库的日志约定：由调用方改写已知密钥，日志模块本身不做自动识别或脱敏。
- **第三方日志**：Telethon、`httpx`、`httpcore` 的日志级别固定为 WARNING（Bot API 的 URL 中含有 token）。被测 bot 沿用 M2 的做法（见 README），它的输出写入权限为 600 的 `server.log`。
- **会话只能串行使用**：同一会话如果同时从两个地方连接，Telegram 可能判定为 `AUTH_KEY_DUPLICATED` 并作废会话。工具在本机用同一把文件锁（`~/.local/state/akkiabot-e2e/run.lock`）保护所有复用已有会话的入口（`run`、`bootstrap-bot`、`session-store`、`prune-groups`），覆盖从连接到断开的全过程，保证同一时间只有一个进程使用测试账号会话。**文件锁无法跨机器生效，同一时间只能在一台机器上运行验收。**
- **限流**：工具关闭了 Telethon 自带的自动等待，自行处理 `FLOOD_WAIT`（`run` 与 `bootstrap-bot` 的 BotFather 对话使用同一策略）：
  - 要求的等待时间不超过 `E2E_FLOOD_MAX_WAIT`（默认 120 秒）时，等待后重试，最多 3 次；
  - 超过上限时，停止本次验收，剩余场景标记为跳过（原因“限流中止”）；
  - 两次发送之间至少间隔 `E2E_SEND_INTERVAL`（默认 2 秒）；
  - 每次 FloodWait 的账号、方法、秒数和重试次数都写入报告（6 节“结果”），用于判断是哪类操作触发了限流。
- **入群限流**：Telegram 按成员账号统计短时间内的入群次数（BOT-27：1 小时内 4 次全量运行后，每次链接入群需等待约 170 秒）。所以成员恢复和新群准备都改由群主拉人，只有 `m7.captcha`、`m2.service-message` 通过链接入群（`m2.join-request` 只提交一次需审批的申请，Telegram 是否把它计入同一额度未经验证）；PR 级和 staging 差异验收都只跑明确涉及的场景，通常不产生链接入群。入群次数减少后，两次验收之间不再要求固定间隔（维护者于 BOT-16 决定，取消此前“两次全量验收至少间隔约 1 小时”）。失败后先查清原因再重跑，不盲目连续重试；遇到 FloodWait 超过上限或群主拉人被限流（`PEER_FLOOD`）时如实报错停止，不回退为链接入群。
- **白名单**：只用 `allowed_user_ids` 中的账号发消息，只在 `allowed_chat_ids` 中的群（或本次用 `--create-group` 新建或按操作标识找回的群）里操作；不清理历史消息。删除群只有 `prune-groups --apply` 一个入口，范围见 6.3 节。

## 8. Sentry

Sentry 测试项目通过 Sentry 的 agent handoff 接入：

1. 维护者在 Sentry 中生成一次性 handoff code。
2. 智能体按 <https://sentry.io/agent-handoff/> 的说明，把 code 放在请求体中，向该端点 POST 一次。
3. 按返回的指引完成配置。
4. 把 DSN 写入 `staging-services/sentry_dsn`，并发送一条测试事件进行验证。

handoff code 只能使用一次，而且很快就会过期；请求和返回内容中的凭据都不回显。

## 9. 扩展场景

场景位于 `tools/e2e/src/akkiabot_e2e/scenarios.py`。每个场景是一个 `async def xxx(ctx: ScenarioContext)` 函数，由以下辅助函数组合而成，写好后加入 `SCENARIOS`：
- `ask()`：成员发送消息并等待 bot 回复；
- `post_anchor()`：群主发送一条普通消息；
- `check()`、`check_code()` 等：检查结果。

如果需要新的 Telegram 操作（如封禁、限制），先在 `transport.Account` 协议中添加方法，并同时在 `telethon_transport.TelethonAccount` 和 `testing.FakeAccount` 中实现；再用模拟 transport 编写单元测试，覆盖通过和失败两种情况。
