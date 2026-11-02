# 公网 Web 改造：Phase 0 审计与实施方案

审计日期：2026-11-01。本地 `main`、`origin/main` 和实际远端 `main` 均为 `dfc8610b2b3fef11a5398f8c79d7d492e9db8cae`，开始时工作区干净。审计仅检查现有代码、测试、部署配置与官方文档；未连接生产数据库、创建云资源、启动隧道、调用模型或发布网站。本文是改造前事实和方案，不是公开部署或安全验收通过的证明。

## 现有架构和入口

浏览器工作台由 FastAPI 同域提供，原生 HTML/CSS/JavaScript Modules 位于 `src/project_copilot/web/`，入口及静态挂载见 `src/project_copilot/api/web.py`。页面支持 Agent/知识问答、证据、上传、工单查询以及提案预览/确认/拒绝；当前上传记录仅存在页面内存，并非完整资料列表。页面内 `ContextSession` 也仅为临时能力凭据，并非登录用户身份。

后端通过 LangGraph 调用工单和知识工具；知识查询复用 Dense + BM25 → RRF → 固定 BGE Rerank，不自动回退 Dense。PostgreSQL 保存工单、上下文与提案；上下文 token 仅保存哈希。确认流程已有行锁、payload 指纹、唯一创建键与事务原子性。Phoenix 接收 Trace，Compose 将 API、PostgreSQL、Qdrant 和 Phoenix 宿主端口绑定到本机回环地址。模型缓存为只读挂载，已有命名卷和向量数据必须保留。

| 现有 API | 作用与当前边界 |
| --- | --- |
| `GET /health`、`GET /ui/config` | 服务健康、上传大小配置；健康状态不等于模型链路就绪 |
| `POST /documents/upload` | 文档入库；当前没有管理员身份检查 |
| `POST /rag/ask`、`POST /agent/ask` | 有限 JSON 问答；目前没有已验证的 SSE 路由 |
| `POST /tickets`、`GET /tickets`、`GET /tickets/{id}` | 创建、筛选和查看真实工单；当前未做用户身份认证和归属过滤 |
| `POST /agent/contexts` | 创建能力上下文；当前不绑定真实用户 |
| `POST /actions/preview`、`GET /actions/{id}`、`POST /actions/{id}/confirm`、`POST /actions/{id}/reject` | 上下文能力、指纹和事务控制提案，但当前不是多用户身份与授权 |
| `GET /`、`/ui-assets/*`、默认 `/docs` / OpenAPI | 本机页面和调试入口；公开模式需单独设置静态安全头并限制调试接口 |

相关源码：`src/project_copilot/api/{tickets,actions,agent,documents,rag,web}.py`、`services/{tickets,approvals}.py`、`agent/{backend,graph,tools,proposals}.py`、`db/models.py`、`rag/{ingestion,final_retrieval,index_lock}.py`、`infrastructure/tracing.py`。已有真实 PostgreSQL 并发、API/Agent、RAG、前端与追踪测试位于 `tests/`，迁移为 `migrations/versions/0001_create_tickets.py` 和 `0002_approvals.py`。

## 公网发布前的重大架构问题

1. **身份和数据隔离缺失。** `Ticket`、`AgentContext` 和 `PendingAction` 没有用户归属；工单列表/详情及 Agent 工具使用的 `TicketService` 不按用户过滤。直接暴露现有接口会让调用方访问或创建工单。必须在 API、Service、Agent 工具和数据库访问层共同执行服务端身份授权；不能信任模型或浏览器传入的用户 ID。
2. **认证和调用费用控制缺失。** 没有用户/角色、登录 Session、CSRF、邀请制注册、原子化跨进程配额；上传没有管理员检查，默认上限为 5 MiB。现有单次工具/模型调用预算不能替代每日用户和全站费用上限。
3. **120 秒网关与现有预算不兼容。** 现有 Agent 默认总超时 300 秒、工具 240 秒；浏览器 Agent 和上传等待分别为 330 秒和 300 秒。EdgeOne Cloud Functions 官方上限 120 秒，请求和响应 body 均为 6 MB，默认执行时限 30 秒。同步请求可能在业务仍执行时断开；仅缩短 Agent 或上传时限会改变现有能力，不能当作兼容通过。[Cloud Functions 限制](https://pages.edgeone.ai/zh/document/cloud-functions)
4. **资料列表/删除尚不存在。** 需要新增真实后端能力，并让 Qdrant points、完成清单、BM25 快照与共享锁保持一致；删除过程必须有失败恢复，不能只删除页面记录。
5. **静态部署和追踪边界。** HTML 引用 `/ui-assets/*`，源文件实际位于 `web/assets/`；直接原样复制会造成 404。浏览器当前调用根路径 API；托管后需切换到同源 `/api/*` 并保留本机兼容。当前 CSP 由 FastAPI 返回，静态托管须另配。追踪中间件会缓冲响应；当前未发现已验证的 SSE 端点，不能将它描述成已有实时流。

## EdgeOne 与隧道可行性

- 官方文档支持将 `cloud-functions/api/[[default]].js` 作为多级 `/api/...` Node Functions 路由，并从服务端 `context.env` 读取变量；函数默认 Node.js 20。静态与函数路由冲突时静态资源优先。建议仓库根目录为构建根，独立脚本输出 `dist/`，将静态资源映射为 `dist/ui-assets/`；实际发布配置仍需在平台验证。[Node Functions](https://pages.edgeone.ai/zh/document/node-functions)、[构建指南](https://pages.edgeone.ai/zh/document/build-guide)
- 官方免费版当前列出 Cloud Functions 100 万次/月、body 6 MB、最大时长 120 秒；具体配额可能变化。4 MiB 文件还需计算 multipart 边界及其他字段，必须对无 Content-Length 的实际读取量设上限。[免费版配额](https://pages.edgeone.ai/zh/document/limits-and-quotas)
- EdgeOne 官方说明：**中国大陆网络使用项目/部署默认域名时需系统预览链接，有效期 3 小时**；过期返回 401。稳定访问建议自定义域名；含中国大陆加速区域的自定义域名需备案。因此“零购域名、默认域名、面向大陆长期稳定访问”目前不能保证。[域名管理](https://pages.edgeone.ai/zh/document/domain-overview)
- Cloudflare Quick Tunnel 不需要账户或域名，适合临时测试，但地址会变化、无可用性保证、不支持 SSE，且最多 200 个进行中的请求。正式命名隧道发布应用的官方前提包括 Cloudflare 账户与域名。[Quick Tunnel](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)、[命名隧道](https://developers.cloudflare.com/tunnel/get-started/)
- **未实测：** EdgeOne 函数能否稳定访问 Quick Tunnel、跨境连通、多条 `Set-Cookie`、multipart、Cookie/CSRF、上游断连、实际超时及流式转发。官方文档和本地模拟都不能证明这些项目通过；未经用户授权不创建云资源或公网入口。

## 推荐技术方案与关键约束

1. 使用独立 `public-demo` 模式，默认关闭；原本机 API 和 Hybrid + Rerank 保持。所有公网请求必须先通过严格的代理共享密钥校验，空密钥或错误密钥拒绝。代理只接受预设 HTTPS 上游和明确路径/方法白名单，丢弃客户端伪造的内部密钥和转发头、过滤 hop-by-hop 头、禁止缓存动态 API，处理多条 Cookie 与安全错误。公开健康检查也须设计受控的内部探针，不设置可绕过鉴权的业务例外。
2. 采用服务端 Session、Argon2id、随机 session token 的数据库哈希、生产 `Secure`/`HttpOnly`/`SameSite=Lax` Cookie 与 CSRF 校验；邀请码默认开启，管理员通过显式 CLI 初始化，不存在默认管理员密码。登录/注册与业务操作还需可信的速率限制，不能信任客户端的 `X-Forwarded-For`。
3. 增量迁移新增用户、session、归属和必要索引，保留历史行；旧工单 `owner=NULL` 仅管理员可见。Agent Context、提案和工单绑定服务端认证主体，确认和拒绝检查同一主体；维持原行锁、唯一键和事务行为。使用隔离的真实 PostgreSQL 验证两用户越权、竞争及失败回滚，不迁移或修改现有生产库。
4. 为长时间 Agent 与上传设计有归属的持久任务、短请求提交和可恢复轮询；保留本机同步接口。任务提交幂等、状态持久化、崩溃恢复和结果查询须明确；确认/拒绝必须维持独立操作，不能因超时自动重放。实际实现与平台上限需要端到端验证，方案本身不证明可行。
5. 每用户与全站每日配额、每用户并发上限和注册/登录限流使用 PostgreSQL 原子操作，记录尝试、成功、失败和可用 token usage；对进程崩溃、超时、跨日及未完成任务设计可回收占用，不虚构账单。
6. 管理员上传上限 4 MiB，公开知识库为全局公共集合，普通用户只读。管理员列表/删除须基于真实已完成清单与 Qdrant，锁住多步变更并规划补偿/重试，不能展示虚构状态。文件解析限制与 Function 6 MB 上限分别测试。
7. 保留当前有限 JSON 回答；若后续新增 SSE，Quick Tunnel 官方不支持，需更换已验证链路或另做产品方案。即便 Cloud Functions 本身支持流式示例，也不能推断整条代理/隧道链不缓冲。

## 按用户要求的执行顺序和文件计划

| 阶段 | 主要改造范围 | 必要验证 |
| --- | --- | --- |
| Phase 1 | `web/index.html`、`assets/styles.css`、本地图像/SVG，必要的交互无障碍调整 | 等待参考截图；保持现有 DOM 功能；桌面、移动端、键盘及原前端测试 |
| Phase 2 | `scripts/build_edgeone.mjs`、`cloud-functions/api/[[default]].js`、`edgeone.json`、前端 API 前缀与本地兼容 | 静态资源、严格白名单、GET/POST、Cookie、多 Set-Cookie、CSRF、multipart、超时/断连和可用的流式行为；本地模拟与平台实测分开 |
| Phase 3–4 | 新认证/用户 Schema、服务、路由和 Alembic 迁移；`db/models.py`、`api/`、`services/`、`agent/` | 真实隔离 PostgreSQL：两用户、管理员、旧无主数据、上下文/提案隔离、并发确认/拒绝及回滚 |
| Phase 5 | 公网密钥中间件、限流、原子配额、上传与错误控制 | 直接绕过代理、CSRF、伪造头、未登录/越权、跨进程配额和失败路径 |
| Phase 6–7 | 独立 Compose override、启动/停止脚本、部署和安全文档 | 复用原卷和模型缓存；经用户授权后才能创建隧道/云资源、连接账号和发布；必须实测 EdgeOne → Tunnel 及完整链路 |

用户随后提供参考截图，Phase 1 已完成本地视觉改造；受控 Chromium 16 项、前端客户端 25 项通过，没有调用真实业务数据库或模型。桌面及移动端截图分别保存于 `docs/evidence/PUBLIC_WEB_DESKTOP.png` 和 `docs/evidence/PUBLIC_WEB_MOBILE.png`。公网部署依赖后续明确授权及平台账户操作。即使本地开发、代理模拟和自动测试完成，也不能标记为实际公网部署成功。
