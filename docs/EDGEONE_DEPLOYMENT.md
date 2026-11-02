# EdgeOne 公开演示部署准备

**当前状态：未发布。** 后端公网硬闸对通过代理密钥校验的请求仍返回 503。管理员异步上传及后台处理已有代码，但尚未对公网开放；EdgeOne → Cloudflare Tunnel 的真实连通性、Cookie、多 `Set-Cookie`、multipart、超时及长任务链路均未验收。`scripts/start_public_demo.sh` 有意拒绝启动。本文只描述准备条件，并不授权创建云资源、启动隧道或发布站点。

## 静态站与代理

前端仍是 `src/project_copilot/web/` 下的原生 HTML、CSS 和 JavaScript。仓库根目录的构建命令为 `node scripts/build_edgeone.mjs`，默认将允许的文件写到新的 `dist/`，目标若已存在则拒绝覆盖；现有本机 FastAPI 静态服务不会受到构建影响。`cloud-functions/api/[[default]].js` 是 Node.js Cloud Function 入口，浏览器仅访问同源 `/api/`，后端地址只存在于函数运行环境。`edgeone.json` 包含静态安全头、动态响应禁止缓存以及 120 秒函数上限配置。

后续取得用户明确授权并完成安全验收后，才能在 EdgeOne 使用 GitHub 仓库根目录、上述构建命令和 `dist` 输出目录配置站点，并在实际平台再次核对函数路由、免费额度和部署设置；本地测试不能证明平台行为。Cloud Function 需要两个**仅放在平台服务端环境变量**的值：

| 变量 | 用途 |
| --- | --- |
| `PROJECT_COPILOT_BACKEND_ORIGIN` | 实际 Quick Tunnel 的 HTTPS origin；当前代理只接受 `*.trycloudflare.com`，不含端口、路径或查询。隧道重启后地址可能变化。 |
| `PROJECT_COPILOT_PROXY_SECRET` | 随机 32–128 位 base64url 共享密钥；与本地后端 `PROJECT_COPILOT_PUBLIC_PROXY_SECRET` 的值完全一致。 |

不得将密钥、Tunnel 地址、`.env.public-demo` 或模型密钥写入前端代码、仓库、截图、日志或聊天。后端还需要 `PROJECT_COPILOT_AUTH_PUBLIC_ORIGIN` 指向实际 EdgeOne 站点 HTTPS origin，用于 Origin/CSRF 验证。默认邀请码注册；不要设置默认管理员密码。

Quick Tunnel 是临时演示方案，电脑、WSL 和 Docker 必须持续运行；其地址会变化，且官方注明不支持 SSE。当前项目没有已验收的 SSE 路由，不能将其声称为支持。公网可用性与中国大陆访问稳定性仍需真实测试；EdgeOne 默认域名在中国大陆可能需要时效为 3 小时的预览链接，具体以平台当时规则为准。参考 [Cloud Functions 限制](https://pages.edgeone.ai/zh/document/cloud-functions)、[EdgeOne 域名说明](https://pages.edgeone.ai/zh/document/domain-overview)和 [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)。

## 本地已实现的限制

公开模式将管理员 TXT、Markdown 文件提交为后台任务：文件最大 4 MiB，整个 multipart 请求最大 4 MiB + 64 KiB；默认文档上限为 64 个片段、32,000 字符。公开 PDF 上传返回 503，等待隔离解析的资源安全验收；本机原 PDF 上传能力保持。逻辑任务按用户每天 5 次、全站每天 30 次、每用户并发 1 次的默认配置预留配额；每个任务默认最多登记 16 次供应商请求发起。这些值可通过后端配置调整，且必须先通过整体安全验收才能公开使用。

供应商发起计数在外呼前写入，进程恰在发送前中断时可能高于供应商实际收到的次数；token usage 只记录实际返回且可解析的部分，真实费用以账单为准、当前为未知。已领取任务不自动重试；捕获到的不确定结果标为 `unknown` 并保留并发占用，进程被强制终止时也可能停留在 `running`。待领取的上传字节存入演示 PostgreSQL，领取事务提交后删除附件行；worker 运行时会清理默认超过 24 小时仍未领取的任务及附件，停止期间不清理。强制终止进程还可能残留临时文件，数据库日志和备份可能保留历史字节。发布前必须确定停止期间的保留与清理策略。

## 启用前必须通过

- 用户明确同意创建隧道、连接平台账号及发布；默认不自动执行。
- 审核身份、CSRF、资源归属、审批并发和 PostgreSQL 原子配额，使用独立数据库完成真实并发验收。
- 完成长请求的持久任务和超时恢复，验证被取消的等待不会误认为底层线程已停止；不得通过缩短现有 Agent 能力假装兼容网关。
- 管理员上传与删除、Qdrant/完成清单/BM25 一致性、安全恢复及 4 MiB 文件限制通过验证；公开 PDF 尚需隔离解析与资源限制验收。
- 真实验证浏览器 → EdgeOne → Tunnel → 后端的登录 Cookie、CSRF、Agent/RAG、工单、确认/拒绝、限制和异常；确认数据与密钥不泄露。
- 审核独立卷、备份、恢复与关闭流程，不能通过覆盖或删除旧数据库、Qdrant 和模型缓存解决部署问题。

任一关键项失败时保留 503 硬闸，记录限制与恢复步骤，不宣称公网可用。
