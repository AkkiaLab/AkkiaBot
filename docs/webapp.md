## Mini App 验证页（apps/webapp）

`apps/webapp` 是纯静态的 Telegram Mini App（Vite + React + TypeScript + Tailwind），部署到 Cloudflare Pages（生产 `https://bot.akkia.moe`）。目前只有一个页面：`/captcha?challenge=<uuid>`。

### 页面流程

1. 机器人在私聊里给被验证成员发送 Mini App 按钮，URL 为 `<WEBAPP_URL>/captcha?challenge=<id>`（见 README“入群验证”）。
2. 页面用 `@telegram-apps/sdk-react` 初始化 SDK、绑定 Telegram 主题色并调用 `ready`，取原始 `initData`。
3. 渲染 Cloudflare Turnstile 组件；拿到 token 后自动提交 `POST <VITE_API_BASE_URL>/api/v1/captcha/verify`（`challenge_id`、`init_data`、`turnstile_token`）。请求客户端使用 openapi-fetch 和 `just gen-api` 生成的类型（`src/api/client.ts`）。
4. 结果展示：
   - 成功：显示后端返回的消息，1.5 秒后关闭 Mini App；
   - 已超时（`challenge_expired`）：显示超时说明和“关闭”按钮，不提供重试（成员保持禁言，需管理员处理或退群重进）；
   - 可重试失败（`turnstile_failed`、`captcha_unavailable`、`unmute_failed`、网络错误、非预期响应、Turnstile 组件报错/过期）：显示原因和“重试”按钮，重试会重新加载 Turnstile 组件以获取新 token；
   - 其他拒绝（身份无效/过期、不是本人、已完成、非 captcha 禁言等）：显示后端消息和“关闭”按钮。
5. 页面在提交前先检查：`challenge` 不是 UUID、不在 Telegram 中打开、构建时没有配置 Turnstile site key，这三种情况直接显示错误，不渲染 Turnstile。

其他路径返回“页面不存在”。Pages 项目不放 `404.html`，因此任意未知路径（包括 `/captcha`）都会回退到根 `index.html`，由前端按 `location.pathname` 渲染。

### 构建期配置

| 变量 | 说明 |
| --- | --- |
| `VITE_API_BASE_URL` | 后端地址，生产为 `https://bot-api.akkia.moe`。为空时请求同源相对路径。 |
| `VITE_TURNSTILE_SITE_KEY` | Turnstile 站点密钥（公开值）。生产构建为空时页面显示“配置不完整”。开发服务器为空时使用 Cloudflare 的测试 key `1x00000000000000000000AA`（总是通过），该回退只存在于开发构建。 |

两者都在 `vite build` 时写入产物，修改后需要重新构建发布。

### 本地开发

```bash
just dev   # 启动 postgres / redis 和 Vite 开发服务器
```

只有开发服务器（`import.meta.env.DEV`）会加载 `src/dev/mockTelegramEnv.ts`，在普通浏览器里模拟 Telegram 环境（未签名的 initData，后端会以 `init_data_invalid` 拒绝，用于调 UI）。生产构建不包含该模块：CI 的前端 job 和发布 workflow 都会在构建后执行 `apps/webapp/scripts/check-no-dev-mock.sh`，产物中出现 mock 标记即失败。本地可用 `just build-web` 复现。

### 部署（GitHub Actions + `wrangler pages deploy`）

选择在 GitHub Actions 里用 wrangler 直接上传（Direct Upload），不用 Pages 的 Git 集成，原因：构建沿用仓库锁定的 Node / pnpm 版本；发布前一定先跑 mock 检查；不需要在组织上安装 Cloudflare 的 GitHub App；部署地址作为 GitHub deployment 显示在 PR 上。

`.github/workflows/webapp-deploy.yml`：

- `master` 推送（改动涉及前端时）或手动触发：GitHub 环境 `webapp-production`，以 `--branch=master` 上传，即 Pages 生产部署，`bot.akkia.moe` 随之更新；
- PR：GitHub 环境 `webapp-preview`，以 PR 分支名上传，得到预览地址（`<分支>.<项目>.pages.dev`），在 PR 时间线的 “View deployment” 中打开；
- 当前环境没有 `CLOUDFLARE_API_TOKEN`（尚未配置，或来自 fork 的 PR）时只构建和检查，跳过上传；有 token 但缺少下列变量时失败。

GitHub 配置（已于 2026-11-14 完成；token 按环境区分，dev/staging 与 prod 各用一个，见 BOT-4 中负责人的决定）：

| 位置 | 名称 | 值 / 来源 |
| --- | --- | --- |
| 仓库变量 | `CLOUDFLARE_ACCOUNT_ID` | `op://AkkiaBot/staging-cloudflare/account_id`（prod 为同一账户） |
| 仓库变量（可选） | `CLOUDFLARE_PAGES_PROJECT` | Pages 项目名，默认 `akkiabot-webapp` |
| 环境 `webapp-preview` secret | `CLOUDFLARE_API_TOKEN` | `op://AkkiaBot/staging-cloudflare/api_token`（DNS、Tunnel、Pages、Turnstile 权限） |
| 环境 `webapp-preview` 变量 | `VITE_API_BASE_URL` | `https://bot-api-staging.akkia.moe`（预发布后端） |
| 环境 `webapp-preview` 变量 | `VITE_TURNSTILE_SITE_KEY` | `op://AkkiaBot/staging-turnstile/site_key` |
| 环境 `webapp-production` secret | `CLOUDFLARE_API_TOKEN` | `op://AkkiaBot/prod-cloudflare/api_token`（由负责人填写；**未设置前生产上传会被跳过**） |
| 环境 `webapp-production` 变量 | `VITE_API_BASE_URL` | `https://bot-api.akkia.moe` |
| 环境 `webapp-production` 变量 | `VITE_TURNSTILE_SITE_KEY` | `op://AkkiaBot/prod-turnstile/site_key` |

写入 GitHub 时值一律经 stdin 传递，例如：

```bash
op read op://AkkiaBot/prod-cloudflare/api_token | gh secret set CLOUDFLARE_API_TOKEN --env webapp-production -R AkkiaLab/AkkiaBot
```

Cloudflare 侧（已用 staging token 通过 API 完成）：

1. Pages 项目 `akkiabot-webapp`（Direct Upload），生产分支 `master`。
2. 自定义域名 `bot.akkia.moe`：Pages 项目域名 + `akkia.moe` 区内 CNAME `bot` → `akkiabot-webapp.pages.dev`（代理）。
3. Turnstile widget（Managed）：`akkiabot-staging`（主机名 `akkiabot-webapp.pages.dev`，覆盖所有预览子域）、`akkiabot-prod`（`bot.akkia.moe`）。site key / secret key 存于 1Password `AkkiaBot/staging-turnstile`、`AkkiaBot/prod-turnstile`。
4. 后端：secret key 配置为后端的 `TURNSTILE_SECRET_KEY`（预发布用 `staging-turnstile`，生产用 `prod-turnstile`）；`WEBAPP_URL` 为对应前端地址，`CORS_ALLOW_ORIGINS` 包含该来源。预发布环境使用预览地址时，把该预览来源加入预发布后端的 CORS 列表。

新建 Pages 项目后，`*.akkiabot-webapp.pages.dev` 的证书需要几分钟签发，期间预览地址会 TLS 握手失败。

缓存与响应头在 `apps/webapp/public/_headers`：HTML 等默认 `Cache-Control: no-cache`，带内容哈希的 `/assets/*` 长期缓存（`immutable`）；另加 `nosniff`、`Referrer-Policy`、`Permissions-Policy`。不设置 `X-Frame-Options`，因为 Telegram Web 版在 iframe 中加载 Mini App。

### 验收清单

- PR 预览地址能打开，直接访问 `<预览地址>/captcha?challenge=<任意 uuid>` 返回页面（不是 404），在浏览器中提示“请在 Telegram 中…打开”；
- 合并后 `https://bot.akkia.moe/captcha?challenge=…` 更新为新版本；
- 真实 Telegram 客户端：入群 → 私聊 `/start` → 打开 Mini App → 完成 Turnstile → 解除禁言（按 `AGENTS.md`，不能用用户账号自动化或 Turnstile 测试 key 代替）。
