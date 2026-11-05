# Web 聊天体验升级

## 1. 目标与实际范围

保留米白、竹青、水墨布局及现有任务/审批架构，增加安全 Markdown、真实执行进度、持久化 SSE、停止等待、刷新恢复和 RAG 真实供应商流式适配。Agent 先展示真实阶段，最终回答仍由原契约和来源权限校验后返回。

基于 `origin/main b811280`（包含 PR #6–#8 代理修复），仅重放 `feat/personal-demo` 的独立提交 `ca50d97`，再开发本次功能。原工作树与运行环境不变；新分支 `feat/chat-stream-experience` 只提交 PR，不自动合并、部署或开放发布状态。未调用真实付费模型。

## 2. 文件与职责

| 文件 | 职责 |
| --- | --- |
| `web/assets/markdown.js`、`styles.css` | DOM 白名单渲染、标题/列表/代码/表格/引用排版；不解析原始 HTML，不加载模型给出的图片 |
| `web/assets/api.js`、`app.js`、`web/index.html` | SSE 读取、去重、轮询降级、停止等待、仅编号恢复、独立提案卡片 |
| `scripts/build_edgeone.mjs` | 显式收录 Markdown 模块，继续拒绝复制私密文件 |
| `db/jobs.py`、`migrations/versions/0013_job_events.py` | 事件表及每任务序号，原任务、配额、幂等身份不变 |
| `services/job_events.py`、`infrastructure/job_progress.py` | 跨进程事件持久化、合并真实 delta、固定进度标签 |
| `api/job_events.py`、`api/jobs.py` | 登录/Owner/发布门禁后的只读 SSE；原任务详情增加 `progress`、`event_cursor` |
| `infrastructure/models.py`、`rag/streaming.py`、`rag/answering.py` | DeepSeek `stream=true` 原始 SSE 读取、JSON 中 answer 的临时草稿提取、原最终校验 |
| `agent/graph.py`、`rag/final_retrieval.py` | 在实际模型/工具/重排执行位置发出阶段事件，保留 Hybrid + Rerank |
| `services/ai_worker.py`、`infrastructure/tracing.py` | Worker 独立 TraceRuntime、脱敏失败类型；SSE 不被追踪中间件缓存 |
| `services/public_maintenance.py` | 显式清理过期终态事件；保留 unknown/running/人工恢复证据 |
| `cloud-functions/proxy.mjs`、`deploy/nginx/public-demo.conf` | 精确 SSE 路由、游标转发、25 秒代理期限、禁止缓冲与重试 |
| `tests/test_*stream*.py`、`test_job_events_postgres.py`、`test_worker_job_progress.py`、`tests/*sse*.mjs`、`frontend_stream.test.mjs` | 流式契约、真实 PG、代理 HTTP、浏览器安全与停止/恢复回归 |

旧 unknown 任务的只读调查独立记录在 [UNKNOWN_JOB_3f72446c.md](UNKNOWN_JOB_3f72446c.md)，没有改写历史结果。

## 3. 执行流程与事件协议

`POST /jobs`（一次幂等提交）→ PostgreSQL 任务 → 独立 Worker 领取一次 → 真实执行位置发事件 → PostgreSQL journal → `GET /jobs/{job_id}/events` → 浏览器。模型执行和事件读取分离；断线、刷新、停止、恢复都不会再次执行任务。

每条持久事件包含 `id: <任务内单调递增整数>`、`event: <类型>`、`data: <JSON>`，以空行分帧。示例：

```text
id: 3
event: progress
data: {"stage":"search_knowledge","label":"正在检索知识库"}

id: 4
event: delta
data: {"text":"回答的实际新增文本"}

id: 6
event: terminal
data: {"status":"succeeded"}
```

| 事件 | 含义 |
| --- | --- |
| `progress` | 后端固定标签，对应实际模型、知识/工单工具、重排、提案或校验阶段；无参数/工具原文 |
| `delta` | RAG 供应商实际产生的新增 answer 文本，**未完成最终校验** |
| `diagnostic` | 允许列表内失败类型，不含原始异常、密码或供应商响应 |
| `terminal` | 与最终任务状态/结果同事务提交；`unknown` 不等于 `failed` |
| `reset` | 保留期/容量使历史不完整，或游标超过当前序号；丢弃草稿，转向正式状态，不能拼接不完整历史 |
| `snapshot` | 已终止任务状态提示，兼容没有事件的旧任务；仍读取正式详情 |
| `reconnect` | 只读连接达到 20 秒，继续用原 job_id/cursor 连接 |

重连参数 `after` 或 `Last-Event-ID`，只接受 0–2147483647 的整数；客户端按序号去重。心跳为 `: heartbeat` 注释，不是假进度。每秒短事务读取；每批重新检查 Session、用户归属和发布有效期，不长时间占用数据库 Session。管理员也不能通过事件接口读取别人的草稿。

SSE 提示终态后，浏览器必须再次 `GET /jobs/{id}`，以其中已验证结果替换草稿。SSE 两次故障后自动使用原详情接口轮询；403/401不绕过身份错误。等待上限约8分钟，保留恢复入口。

## 4. 设计取舍与边界

- 每任务最多512条持久事件；单条JSON最多8KiB。RAG草稿最多4000字符，与回答Schema一致，按约192字符或实际到达的200ms间隔合并，最大512字符一批。没有前端逐字定时器，也没有每token一笔写入。
- 展示事件有效期7天。现有管理员 `public_maintenance cleanup` 在 CLOSED/Worker静止条件下，按批清理旧 succeeded/failed 事件；unknown/running/人工恢复关联证据不自动删除，仍受每任务512条上限。幂等身份、配额和业务记录不删除。
- Markdown仅创建DOM元素：原始HTML作为文字，禁图片、可执行链接、内嵌样式和脚本；最终渲染一次。临时草稿用 `textContent`，不反复解析不完整Markdown；用户向上阅读时不强行拉到底部。
- “停止等待”立即终止当前读取与展示，明确后台可能继续。**没有后台取消/退款/强杀Worker功能**。初始POST不因停止而重试；若其响应稍后到达，仍保留job_id。
- 本地仅保存按账号隔离的最近8个任务UUID/类型/时间，7天过期；不存问题、回答、Cookie、CSRF或Context bearer。相同页面可复用内存中的原Context显示提案卡片；刷新后不自动恢复确认权限，不重新生成提案。原确认接口和指纹校验保持。
- 只有公开异步任务 Worker 开启 RAG 真实流式适配；普通本机 `/rag/ask`、`/agent/ask` 维持原响应契约。Agent 只发阶段事件，未验证的 JSON/source_ids 不流式当成正式答案。
- Worker后续执行建立独立Trace，以job_id作run_id；导出失败脱敏记录，不能宣称Phoenix收到。SSE响应开始时 `X-Trace-Exported:false`，因真正导出发生在流关闭后；不能为回填此header缓存SSE。
- 每条SSE连接20秒，代理最多25秒，低于Makers默认30秒及最大120秒；Node官方支持SSE，但不替代实际公网验证。[官方Cloud Functions说明](https://pages.edgeone.ai/document/cloud-functions)、[官方SSE示例](https://pages.edgeone.ai/resources/node-functions)。

## 5. 自动验证结果

Codex执行，非用户亲自手测。最终结果如下；测试报告不合并重复执行的样本。测试使用受控模型/OTLP传输，不发送真实DeepSeek或智谱请求。

- 默认Python全套回归：**1518通过、0失败、231跳过**（真实服务/硬件等显式开关未开启）。运行 `PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q --disable-warnings`。需要现有本地虚拟环境；隔离worktree只复用同一虚拟环境，不修改依赖。
- 真实独立PostgreSQL：**88通过、0失败、0跳过**。`PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 .venv/bin/python -m pytest -q tests/test_job_events_postgres.py tests/test_worker_job_progress.py tests/test_public_jobs.py tests/test_ai_jobs_security_postgres.py tests/test_public_operations_postgres.py tests/test_personal_demo.py tests/test_personal_demo_jobs.py tests/test_personal_demo_flow.py`。每个测试创建随机空库，验证迁移/ORM、并发序号、跨OS进程读取、Owner、撤销、容量、清理、回滚、配额、不重放及真实编译Agent；发布准入/模型/OTLP为明确替身。
- 全部Node：**200通过、0失败、0跳过**（含6项离线浏览器验收）。`node --test tests/*.test.mjs`，浏览器测试使用既有Playwright路径并拦截业务网络；包含真实localhost HTTP分帧、无重POST、取消、Origin、请求体、Cookie和Markdown/XSS。
- 真实隔离Nginx：**9通过、0失败、0跳过**。`PROJECT_COPILOT_RUN_INGRESS_DOCKER=1 .venv/bin/python -m pytest -q tests/test_public_ingress_docker.py`；全新短生命周期容器/网络，无现有数据卷。
- 静态构建：`node scripts/build_edgeone.mjs --output-dir /tmp/pc-chat-static-final-20261105` 实际生成8个静态文件，未覆盖原产物。
- `node --check cloud-functions/proxy.mjs`、静态构建与`git diff --check`。

桌面1440及移动390宽度的离线浏览器检查未见整页横向溢出，代码块独立滚动；测试Chromium缺少中文字体，实际中文字形效果尚需用户浏览器核对。

未执行：真实供应商流式兼容、真实EdgeOne＋Cloudflare全链路即时透传、用户学习后手测。旧unknown任务已通过原卷只读冷副本核实（3次供应商发起计数、1491 total tokens、无关联提案）；细分failure_type和实际供应商结果没有历史证据，不补造。

## 6. GitHub 与部署状态

独立功能分支/PR，未合并main，未修改线上环境变量，未重建实际演示容器，未启动Tunnel/Worker或开放PERSONAL_DEMO。真实提交号与PR链接由交接摘要提供，不在报告内自引用。

## 7. 最少本机验证与公网操作

合并/上线必须由用户审核后自行执行；本次不自动执行下列部署操作。

1. **离线验证**：在新代码工作目录执行 `node --test tests/frontend_stream.test.mjs tests/edgeone_sse.test.mjs`；执行 `PYTHONPATH="$PWD/src:$PWD" .venv/bin/python -m pytest -q tests/test_rag_streaming.py tests/test_sse_tracing.py`。这些使用模型替身，应无付费、无真实工单创建。
2. **完整更新（仍关闭）**：先按现有流程显式CLOSED并停止领取，等待已运行任务结束；不要用重启丢弃不确定执行。按 [PERSONAL_DEMO原部署步骤](PERSONAL_DEMO.md) 从本PR新源码构建镜像，运行 `migrate` 升级到 `0013_job_events`，再更新 `api`、`worker`、`ingress`，不能只更新前端/旧Worker。保留所有卷、缓存和账号，不更改PUBLIC门禁。源码变化会使旧发布授权失效，需用户按原流程主动重新授权；不自动续期。
3. **公网一次体验**：用户决定合并PR后，EdgeOne按原 `node scripts/build_edgeone.mjs`、`dist`及Node函数一起部署；无新增云变量，保留已审SITE_ORIGIN、代理密钥和本人Tunnel目的地。登录唯一测试账号，提交一次RAG/Agent；浏览器Network中查看 `/api/jobs/<id>/events?after=...` 的 `text/event-stream` 是否在任务完成前收到多个独立帧。点击停止、刷新后从原任务入口恢复；Network应只有一次POST。RAG最终显示Markdown/原文来源；Agent提案仍是独立确认卡片。

只在用户主动授权并预算允许时做第3项真实模型体验。响应头200或本地分帧不能证明公网即时透传；若全部帧直到20秒结束才出现，记录为平台/链路缓冲，使用自动轮询保底，不称公网流式通过。若事件404先核对EdgeOne和Nginx是否都更新；503先看发布期限/源代码指纹/迁移，401看Session，unknown保留任务编号只读排查。检查Worker是否确为新镜像，别为获得出字效果重跑任务。

## 8. 后续计划

等待用户审核PR并决定部署；部署后只验证最小一次真实链路，不自动增加供应商测试、发布状态或额外功能。集中学习可从任务提交→Worker观察器→PostgreSQL journal→SSE代理→草稿/最终校验→停止/恢复链开始。
