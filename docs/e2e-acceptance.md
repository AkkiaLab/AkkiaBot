# 真实 Telegram 验收（1Password + 用户账号自动化）

本文说明如何在真实 Telegram 中对已实现的功能（目前为 M2、M3）做端到端验收：密钥由 1Password 统一管理，两个专用测试用户账号通过 MTProto（[Telethon](https://docs.telethon.dev/)）在专用测试群里发命令，工具检查 bot 的真实回复并生成报告。

工具代码位于 `tools/e2e`（Python 包 `akkiabot_e2e`，命令 `akkiabot-e2e`）。它只通过 Telegram 黑盒验证，不导入产品代码；产品代码也不依赖它（import-linter 契约 `e2e-black-box`、`product-without-e2e`）。

## 1. 能做什么、不能做什么

| 可以自动化 | 需要人工 / 不覆盖 |
| --- | --- |
| 建立或复用专用私有测试超级群，邀请测试 bot 并授予管理员权限 | Telegram 用户账号**不能自动注册**；每个测试账号的首次登录需要负责人输入验证码和两步验证密码 |
| 普通成员账号发送 `/help`、`/id`、回复群主消息的 `/id`、`/ip 1.1.1.1`、回复带标签 IP 消息的 `/ip` | Mini App 真实客户端交互、正式 Turnstile 成功路径：MTProto 用户账号或 Turnstile 测试站点 key 都不能冒充，后续里程碑需单独验收 |
| 检查回复文本、行内代码实体（即 MarkdownV2 已正确渲染、没有残留反斜杠） | Sentry 上报需在 Sentry 测试项目中人工确认（工具会把 `OBSERVABILITY_SENTRY_ENVIRONMENT` 设为 `staging`） |
| 成员退群 / 入群，检查系统消息被 bot 删除 | 删除测试群、清理历史消息：工具从不执行，需另行确认 |
| `--serve` 模式下检查 `message_log` 中有对应记录 | |

普通 CI 不会发送任何真实消息：单元测试全部使用内存中的模拟 transport（`akkiabot_e2e.testing`）。

## 2. 1Password 结构

专用 vault：`AkkiaBot-Staging`（只放测试环境配置；生产配置以后放另一个 vault）。每台运行机器使用**独立的只读服务账号**（如 `akkiabot-staging-runner-01`），只授予该 vault 的读取权限。

### 条目 `runtime`（被测 bot 的配置）

| 字段 | 类型 | 内容 |
| --- | --- | --- |
| `bot_token` | 密码 | 测试 bot 的 token（与生产 bot 分开） |
| `sentry_dsn` | 密码 | Sentry 测试项目 DSN；暂时没有时留空（空值即不启用 Sentry） |
| `ipinfo_token` | 密码 | ipinfo token（M3 `/ip` 使用） |

### 条目 `telegram-e2e`（用户账号自动化）

| 字段 | 类型 | 内容 | 何时填写 |
| --- | --- | --- | --- |
| `api_id` | 文本 | [my.telegram.org](https://my.telegram.org) → API development tools 中的 `api_id` | 一开始 |
| `api_hash` | 密码 | 同上的 `api_hash` | 一开始 |
| `bot_username` | 文本 | 测试 bot 的用户名（不带 `@` 也可以） | 一开始 |
| `admin_session` | 密码 | 群主/管理员测试账号的会话（第 4 节生成） | 首次登录后 |
| `member_session` | 密码 | 普通成员测试账号的会话（第 4 节生成） | 首次登录后 |
| `allowed_user_ids` | 文本 | 两个测试账号的 user id，逗号分隔（登录时会显示） | 首次登录后 |
| `allowed_chat_ids` | 文本 | 测试群 id（如 `-1001234567890`），逗号分隔；建群前先填 `0` | 首次建群后 |

`op run` 引用不存在的字段时会直接报错，所以请先把表中字段全部建好（尚无值的按“何时填写”留空或填 `0`）。如果 1Password 不允许保存空字段，请在 issue 里说明，我们再调整模板。

引用模板只含 `op://` 路径，已提交在仓库中：`tools/e2e/staging.env.tpl`（完整验收）、`tools/e2e/login.env.tpl`（首次登录只需 `api_id`、`api_hash`）。

## 3. 每台机器的首次初始化

1. 安装 [uv](https://docs.astral.sh/uv/)、[just](https://github.com/casey/just)，克隆仓库后执行 `just setup-py`。
2. 安装 1Password CLI（`op` 2.x），见[官方安装说明](https://developer.1password.com/docs/cli/get-started/)，执行 `op --version` 确认。
3. 在 1Password 网页为这台机器创建服务账号（只读 `AkkiaBot-Staging`），令牌只显示一次，先存到你自己的管理 vault。
4. 在这台机器上注入令牌（**只需一次**）：

   ```bash
   just e2e-token-init
   ```

   按提示粘贴令牌，输入不回显，不会进入 shell 历史或命令行参数。令牌保存在 `~/.config/akkiabot/op-service-account-token`（目录 700、文件 600；可用环境变量 `AKKIABOT_OP_TOKEN_FILE` 改位置）。工具读取时会检查属主和权限，权限过宽会拒绝使用。

   - **后续运行**：`just e2e` 等命令通过 `akkiabot-e2e op-exec` 读取该文件，以环境变量 `OP_SERVICE_ACCOUNT_TOKEN` 传给 `op run`（从不放在命令行参数里），`op run` 再把 `op://` 引用解析成实际值，只注入子进程环境，不会落盘成明文配置。
   - **轮换令牌**：在 1Password 新建或重新生成令牌后执行 `just e2e-token-init --force`。
   - **撤销机器**：在 1Password 中删除该机器的服务账号，并删除上述令牌文件。
   - 如果当前 shell 已导出 `OP_SERVICE_ACCOUNT_TOKEN`，优先使用它；既没有文件也没有环境变量时，`op` 会使用自身的登录状态（例如桌面应用集成）。

## 4. 测试账号首次登录（每个账号一次，不是每台机器一次）

准备两个由负责人控制的真实 Telegram 账号：一个作为群主/管理员（建群、授权、发锚点消息），一个作为普通成员（发命令）。以后入群验证、`/ban`、`/warn` 等场景也沿用这两个角色。

```bash
just e2e-login admin    # 群主/管理员账号
just e2e-login member   # 普通成员账号
```

按提示输入手机号、Telegram 发来的验证码和两步验证密码（密码不回显）。验证码和密码只在本机终端输入，**不要发到 issue 或评论里**。完成后终端显示账号名和 user id（不是密钥），会话写入 `~/.config/akkiabot/e2e/<role>.session`（权限 600，内容不会打印）。

会话等同于该账号的完整登录凭据，请立即存入 1Password 并删除本地文件。服务账号是只读的，所以这一步要用负责人自己的 1Password 登录（有写权限）：

```bash
op signin                        # 负责人自己的账号；已启用桌面应用集成时可省略
just e2e-session-store admin     # 写入 AkkiaBot-Staging/telegram-e2e/admin_session
just e2e-session-store member    # 写入 member_session
```

`session-store` 的流程：读取本地会话文件 → `op item get --reveal --format=json` 取出条目 → 在内存中替换对应字段 → 把 JSON **通过标准输入**交给 `op item edit`（op 2.x 支持管道输入，见 `op item edit --help`）→ 用 `op read` 回读并在内存中比对 → 成功后覆盖并删除本地文件。会话不会出现在命令行参数、终端输出或 shell 历史中；子进程环境中会去掉 `OP_SERVICE_ACCOUNT_TOKEN`，确保使用你自己的登录而不是只读服务账号。失败时本地文件保留，报错信息中的会话会被改写为 `[REDACTED]`。字段需提前建好（第 2 节），工具不会新建字段。

请不要改用 `op item edit ... "admin_session[password]=..."` 这类赋值参数：赋值会出现在进程参数里，可能被同机进程或进程采集看到；也不要把会话 `cat` 到终端再复制。

最后把两个 user id 以逗号分隔写入 `allowed_user_ids`（user id 不是密钥）。

会话失效（例如在 Telegram「设备」中被终止）时，运行会提示“会话已失效”，重新执行本节即可。

## 5. 首次建群与日常运行

```bash
just e2e --create-group   # 首次：没有可用测试群时创建
just e2e                  # 之后：复用测试群，执行全部场景
just e2e --scenarios m2   # 只跑 M2；也可写场景名，如 --scenarios m3.ip,m2.id
uv run akkiabot-e2e list  # 列出场景
```

`just e2e` 会：

1. 用当前检出的代码在本机启动被测 bot（`akkiabot migrate` + `akkiabot serve`，`--serve` 模式）：运行目录中的全新 SQLite、进程内缓存、HTTP 与指标端口只绑定 `127.0.0.1`，长轮询接收更新；bot 进程的环境中**不含**测试账号会话和服务账号令牌。请确认没有其他实例在用同一个测试 bot token 轮询，否则两边会互相抢更新。
2. 用两个测试账号登录，校验它们都在 `allowed_user_ids` 中、是两个不同的真实用户，且 `bot_username` 确实是 bot。
3. 选择测试群：只考虑**群主账号创建的、标题完全等于 `E2E_CHAT_TITLE`（默认“AkkiaBot E2E 自动验收群”）的超级群**，并且 id 必须在 `allowed_chat_ids` 中；同名但不在白名单的群会被拒绝。没有可用的群且带了 `--create-group` 时才创建（简介中注明测试用途）。首次建群后，把报告中的群 id 写入 `allowed_chat_ids`。
4. 准备群：邀请 bot，保证它有 `delete_messages`、`ban_users` 管理员权限（保留手动授予的其他权限）；普通成员通过一次性、1 小时有效的邀请链接入群。
5. 依次执行场景并收集证据，最后在 `message_log` 中核对群主发出的普通消息。

不加 `--serve` 时（`uv run akkiabot-e2e op-exec -- akkiabot-e2e run`），工具只负责发消息与断言，被测 bot 需要你自己另行启动。

### 场景

| 场景 | 内容 |
| --- | --- |
| `m2.help` | `/help` 返回命令列表（含 `/help`、`/id` 及用法） |
| `m2.id` | `/id` 返回“当前ChatId: 群名 (群 id)”“你的UserId: 名字 (user id)”，id 为行内代码，无残留反斜杠 |
| `m2.id-reply` | 回复群主消息的 `/id` 额外返回“回复目标的UserId” |
| `m3.ip` | `/ip 1.1.1.1` 返回 IP、Location、AS、Org、ISP，每个值为行内代码且不是“未知” |
| `m3.ip-reply` | 回复“服务器地址：1.1.1.1:443”这条消息执行 `/ip`，结果同上 |
| `m2.service-message` | 成员退群再经邀请链接入群，两条系统消息都被 bot 删除（最后执行）；Telegram 未返回任一系统消息 id 时记为失败（未验证），不会算通过 |
| `m2.message-log` | 仅 `--serve`：群主发出的普通消息出现在 `message_log` 中 |

M3 合并到 master 之前，在 master 上跑 `m3.*` 会失败（bot 还没有 `/ip`），请用 `--scenarios m2`，或在 M3 分支上运行。

### 结果

- 退出码：`0` 全部通过；`1` 有场景失败或准备阶段失败；`2` 参数或配置错误；`77` **缺少凭据，已跳过**（不发送任何消息，报告明确写“未执行（不是通过）”）。
- 报告：`~/.local/state/akkiabot-e2e/runs/<时间>-<随机>/report.md` 与 `report.json`（目录 700、文件 600），同目录有被测 bot 的 `server.log` 和 SQLite。`report.md` 可直接贴到对应 issue 作为验收记录。
- 终端输出经过 `op run` 的掩码处理，1Password 中保存的值（包括群 id、user id）在终端里会显示为 `<concealed by 1Password>`；报告文件中保留这些标识，便于核对。

## 6. 安全约束

- **密钥只在 1Password 中**：仓库只提交 `op://` 引用模板；不提交、不打印、不上传任何真实凭据、会话或验证码。
- **显式脱敏**：工具在输出和写报告前，把已知密钥（两个会话、`api_hash`、bot token、ipinfo token、Sentry DSN、服务账号令牌）替换为 `[REDACTED]`；`Secret` 类型的 `repr` 不显示原值。这符合仓库的日志约定：由调用方改写已知密钥，日志模块本身不做自动识别脱敏。
- **第三方日志**：Telethon 的日志固定在 WARNING（不随根日志级别输出连接细节）；被测 bot 沿用 M2 的做法（`httpx`、`httpcore` 等固定在 WARNING，见 README），其输出写入权限 600 的 `server.log`。
- **会话串行使用**：同一会话同时在两处连接，Telegram 可能判定 `AUTH_KEY_DUPLICATED` 并作废会话。工具在本机用文件锁（`~/.local/state/akkiabot-e2e/run.lock`）保证同一时间只有一个验收进程；**跨机器无法加锁，同一时间只在一台机器上运行验收**。
- **限流**：Telethon 自带的自动等待被关闭，由工具统一处理 `FLOOD_WAIT`：等待时长不超过 `E2E_FLOOD_MAX_WAIT`（默认 120 秒）时按要求等待后重试（最多 3 次），超过则停止本次验收并把剩余场景标记为未执行；连续发送之间至少间隔 `E2E_SEND_INTERVAL`（默认 2 秒）。不要用缩短等待的方式重试，否则可能导致测试账号被限制。
- **白名单**：只用 `allowed_user_ids` 中的账号发消息，只在 `allowed_chat_ids` 中（或本次 `--create-group` 新建）的群里操作；从不删除群、不清理历史。

## 7. 扩展场景

场景位于 `tools/e2e/src/akkiabot_e2e/scenarios.py`：每个场景是一个 `async def xxx(ctx: ScenarioContext)`，用 `ask()`（成员发送并等待 bot 回复）、`post_anchor()`（群主发送普通消息）、`check()`、`check_code()` 等组合，然后加入 `SCENARIOS`。需要新的 Telegram 操作（如封禁、限制）时，先在 `transport.Account` 协议中添加方法，同时实现 `telethon_transport.TelethonAccount` 与 `testing.FakeAccount`，并用模拟 transport 写单元测试覆盖通过和失败两种情况。
