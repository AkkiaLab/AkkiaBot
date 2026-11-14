# 真实 Telegram 验收（Telegram 测试环境 + 1Password + 用户账号自动化）

本文说明如何对已实现的功能（目前为 M2、M3）做端到端验收：两个测试用户账号通过 MTProto（[Telethon](https://docs.telethon.dev/)）在专用测试群里发命令，工具检查 bot 的真实回复并生成报告。所有 Telegram 交互都在 **Telegram 测试环境**中进行，密钥由 1Password 统一管理。

工具代码位于 `tools/e2e`（Python 包 `akkiabot_e2e`，命令 `akkiabot-e2e`）。它只通过 Telegram 黑盒验证，不导入产品代码；产品代码也不依赖它（import-linter 契约 `e2e-black-box`、`product-without-e2e`）。

## 1. 环境隔离：只使用 Telegram 测试环境

Telegram 测试环境与生产环境是两套独立的系统，账号、群、bot、BotFather 和 token 互不相通。负责人于 2026-11-14 决定：验收一律在测试环境进行，不连接生产环境的账号、群或 BotFather。

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
| 测试账号登录后，通过测试环境的 BotFather 创建测试 bot，并把 token 和用户名写回 1Password | 第三方客户端**不能注册新账号**（Telethon 的 `sign_up` 已被 Telegram 禁止）。测试环境账号需要负责人用官方客户端的测试模式注册，首次登录时由负责人输入验证码和两步验证密码 |
| 建立或复用专用的私有测试超级群，邀请 bot 并授予管理员权限；新群的 id 写回 1Password | Mini App 真实客户端交互、正式 Turnstile 的成功路径：不能用 MTProto 用户账号或 Turnstile 测试站点 key 代替，后续里程碑需单独验收 |
| 普通成员账号发送 `/help`、`/id`、回复群主消息的 `/id`、`/ip 1.1.1.1`，以及回复带标签 IP 的消息执行 `/ip` | 删除测试群、清理历史消息：工具从不执行这些操作，需要时另行确认 |
| 检查回复文本和行内代码实体（确认 MarkdownV2 已正确渲染，没有残留反斜杠） | |
| 成员退群后再入群，检查两条系统消息都被 bot 删除 | |
| `--serve` 模式下检查 `message_log` 中有对应记录 | |

普通 CI 不会连接 Telegram：单元测试全部使用内存中的模拟 transport（`akkiabot_e2e.testing`）。

## 3. 1Password：权限边界与字段

- 专用 vault `AkkiaBot-Staging` 只存放测试环境的配置。生产配置以后放在另一个 vault，本工具不涉及生产。
- **权限边界**（负责人于 2026-11-14 决定，取代此前的“只读”要求）：
  - 测试 vault 的服务账号**可以读写**，工具会自动写回会话、测试 bot token、群 id 等配置。
  - 测试密钥**可以在 Multica 内部共享**（智能体之间、issue 交接）。
  - **仓库是公开的**：仓库、PR、CI 日志和工具输出中都不能出现任何凭据，凭据值也不能出现在命令行参数中。
  - 生产环境的授权范围不变。
- 工具读写 vault 的流程：
  1. 把条目 JSON 读进内存，替换要更新的字段；
  2. 把 JSON **通过标准输入**交给 `op item create -` 或 `op item edit`；
  3. 用 `op read` 回读，并在内存中比对。

  凭据值不会进入命令行参数、终端输出或报告；`op` 的错误输出会先脱敏再显示。这一流程已在 1Password CLI 2.42 上实际验证。

### 条目 `runtime`（被测 bot）

| 字段 | 类型 | 内容 | 由谁写入 |
| --- | --- | --- | --- |
| `bot_token` | 密码 | 测试环境 bot 的 token | `just e2e-bootstrap-bot` |
| `sentry_dsn` | 密码 | Sentry 测试项目的 DSN；为空时不启用 Sentry | Sentry handoff 完成后写入 |
| `ipinfo_token` | 密码 | ipinfo token（M3 的 `/ip` 使用，对应配置 `WHOIS_IPINFO_TOKEN`） | 已写入 |

### 条目 `telegram-e2e`（用户账号自动化）

| 字段 | 类型 | 内容 | 由谁写入 |
| --- | --- | --- | --- |
| `api_id`、`api_hash` | 文本 / 密码 | [my.telegram.org](https://my.telegram.org) 中的 API 凭据（同一组凭据也可用于测试环境） | 已写入 |
| `environment` | 文本 | 固定为 `test`，用于标识本条目 | 已写入 |
| `admin_session`、`member_session` | 密码 | 两个测试环境账号的会话 | `just e2e-login admin\|member` |
| `allowed_user_ids` | 文本 | 两个测试账号的 user id，逗号分隔 | `just e2e-login` 自动合并 |
| `bot_username` | 文本 | 测试 bot 的用户名 | `just e2e-bootstrap-bot` |
| `allowed_chat_ids` | 文本 | 测试群的 id，逗号分隔 | `just e2e --create-group` 首次建群后自动合并 |

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

## 5. 首次准备（需要负责人配合一次）

### 5.1 用官方客户端在测试环境注册两个账号

测试环境的账号与生产账号相互独立，可以用负责人自己的手机号注册。需要两个账号：
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

- 会话直接写入 `telegram-e2e/<role>_session`，并经过回读校验；终端不会显示会话内容。
- 账号的 user id 自动合并到 `allowed_user_ids`。
- 如果写入 1Password 失败，会话会暂存到 `~/.config/akkiabot/e2e/<role>.session`（权限 600）。排除故障后执行 `just e2e-session-store <role>`，写入成功后该文件会被覆盖并删除。

如果会话失效（例如在官方客户端的“设备”中被终止），运行时会提示“会话已失效”，重新执行本节即可。

### 5.3 自动创建测试 bot 和测试群

```bash
just e2e-bootstrap-bot      # 群主账号在测试环境的 @BotFather 创建 akkiabot_e2e_xxxxxx_bot，并写回 bot_token / bot_username
just e2e --create-group     # 首次建群：邀请 bot、授予权限、让成员入群，然后执行全部场景；群 id 写回 allowed_chat_ids
```

`bootstrap-bot` 可以重复执行：如果 1Password 中的 token 已能通过测试端点 `getMe` 的校验，就不会再创建新 bot；确实需要新建时加 `--force`。BotFather 的回复中含有 token，它只保存在群主测试账号与 BotFather 的私聊里，工具不会打印。

## 6. 日常运行

```bash
just e2e                    # 复用测试群，执行全部场景
just e2e --scenarios m2     # 只跑 M2；也可以写场景名，如 --scenarios m3.ip,m2.id
uv run akkiabot-e2e list    # 列出所有场景
```

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
4. 选择测试群。只考虑**群主账号创建的、标题与 `E2E_CHAT_TITLE`（默认“AkkiaBot E2E 自动验收群”）完全一致的超级群**，并且群 id 必须在 `allowed_chat_ids` 中。同名但不在白名单中的群会被拒绝。只有带 `--create-group` 时才会新建群。
5. 准备测试群：
   - 邀请 bot，确保它有 `delete_messages`、`ban_users` 管理员权限（手动授予的其他权限会保留）；
   - 普通成员通过一次性、1 小时内有效的邀请链接入群。
6. 依次执行各场景并收集证据，最后核对 `message_log`。

| 场景 | 内容 |
| --- | --- |
| `m2.help` | `/help` 返回命令列表（包括 `/help`、`/id` 及其用法） |
| `m2.id` | `/id` 返回“当前ChatId: 群名 (群 id)”和“你的UserId: 名字 (user id)”；id 显示为行内代码，没有残留反斜杠 |
| `m2.id-reply` | 回复群主的消息执行 `/id`，额外返回“回复目标的UserId” |
| `m3.ip` | `/ip 1.1.1.1` 返回 IP、Location、AS、Org、ISP 五个字段，每个值都显示为行内代码，且都不是“未知” |
| `m3.ip-reply` | 回复“服务器地址：1.1.1.1:443”这条消息执行 `/ip`，要求同上 |
| `m2.service-message` | 成员退群后通过邀请链接重新入群，两条系统消息都被 bot 删除（此场景最后执行）。如果 Telegram 没有返回其中任一条系统消息的 id，记为失败（未验证），不算通过 |
| `m2.message-log` | 仅 `--serve` 模式：群主发出的普通消息出现在 `message_log` 中 |

M3 合并到 master 之前，在 master 上运行 `m3.*` 会失败（bot 还没有 `/ip` 命令）。这时请用 `--scenarios m2`，或在 M3 的分支上运行。

不加 `--serve` 时（执行 `uv run akkiabot-e2e op-exec -- akkiabot-e2e run`），工具只负责发消息和检查结果，被测 bot 需要你另外启动，并且必须设置 `TELEGRAM_TEST_ENVIRONMENT=true`。

### 结果

- 退出码：
  - `0`：全部通过；
  - `1`：有场景失败，或准备阶段失败；
  - `2`：参数、配置或环境检查失败；
  - `77`：**缺少凭据，已跳过**。此时不发送任何消息，报告中写明“未执行（不是通过）”。
- 报告：`~/.local/state/akkiabot-e2e/runs/<时间>-<随机串>/report.md` 和 `report.json`（目录权限 700，文件权限 600）。同一目录下还有被测 bot 的 `server.log` 和 SQLite 数据库。`report.md` 可以直接贴到对应 issue，作为验收记录。
- 终端输出经过 `op run` 的掩码处理：从 1Password 引用的值（包括群 id、user id）在终端里显示为 `<concealed by 1Password>`；报告文件中保留了这些标识，便于核对。

## 7. 安全约束

- **公开仓库中不出现任何凭据**：仓库只提交 `op://` 引用模板。真实的凭据、会话和验证码不提交到仓库，也不出现在 PR、CI 日志或工具输出中；测试凭据只在 Multica 内部共享。
- **命令行参数中不出现任何凭据**：令牌通过环境变量传给 `op`，写入 vault 的值通过标准输入传递。不要使用 `op item edit ... "field[password]=值"` 这类赋值参数。
- **显式脱敏**：工具在输出和写入报告前，会把已知密钥（会话、`api_hash`、bot token、ipinfo token、Sentry DSN、服务账号令牌）替换为 `[REDACTED]`；`Secret` 类型的 `repr` 不显示原值。这符合仓库的日志约定：由调用方改写已知密钥，日志模块本身不做自动识别或脱敏。
- **第三方日志**：Telethon、`httpx`、`httpcore` 的日志级别固定为 WARNING（Bot API 的 URL 中含有 token）。被测 bot 沿用 M2 的做法（见 README），它的输出写入权限为 600 的 `server.log`。
- **会话只能串行使用**：同一会话如果同时从两个地方连接，Telegram 可能判定为 `AUTH_KEY_DUPLICATED` 并作废会话。工具在本机用同一把文件锁（`~/.local/state/akkiabot-e2e/run.lock`）保护所有复用已有会话的入口（`run`、`bootstrap-bot`、`session-store`），覆盖从连接到断开的全过程，保证同一时间只有一个进程使用测试账号会话。**文件锁无法跨机器生效，同一时间只能在一台机器上运行验收。**
- **限流**：工具关闭了 Telethon 自带的自动等待，自行处理 `FLOOD_WAIT`（`run` 与 `bootstrap-bot` 的 BotFather 对话使用同一策略）：
  - 要求的等待时间不超过 `E2E_FLOOD_MAX_WAIT`（默认 120 秒）时，等待后重试，最多 3 次；
  - 超过上限时，停止本次验收，剩余场景标记为未执行；
  - 两次发送之间至少间隔 `E2E_SEND_INTERVAL`（默认 2 秒）。
- **白名单**：只用 `allowed_user_ids` 中的账号发消息，只在 `allowed_chat_ids` 中的群（或本次用 `--create-group` 新建的群）里操作；从不删除群，也不清理历史消息。

## 8. Sentry

Sentry 测试项目通过 Sentry 的 agent handoff 接入：

1. 负责人在 Sentry 中生成一次性 handoff code。
2. 智能体按 <https://sentry.io/agent-handoff/> 的说明，把 code 放在请求体中，向该端点 POST 一次。
3. 按返回的指引完成配置。
4. 把 DSN 写入 `runtime/sentry_dsn`，并发送一条测试事件进行验证。

handoff code 只能使用一次，而且很快就会过期；请求和返回内容中的凭据都不回显。

## 9. 扩展场景

场景位于 `tools/e2e/src/akkiabot_e2e/scenarios.py`。每个场景是一个 `async def xxx(ctx: ScenarioContext)` 函数，由以下辅助函数组合而成，写好后加入 `SCENARIOS`：
- `ask()`：成员发送消息并等待 bot 回复；
- `post_anchor()`：群主发送一条普通消息；
- `check()`、`check_code()` 等：检查结果。

如果需要新的 Telegram 操作（如封禁、限制），先在 `transport.Account` 协议中添加方法，并同时在 `telethon_transport.TelethonAccount` 和 `testing.FakeAccount` 中实现；再用模拟 transport 编写单元测试，覆盖通过和失败两种情况。
