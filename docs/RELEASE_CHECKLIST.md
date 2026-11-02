# 公网发布验收清单（阶段一）

**当前结论：未达到开放条件。** `PublicGatewayMiddleware` 对携带正确代理密钥的请求仍返回 503；`start_public_demo.sh` 拒绝启动；Compose 的 API 健康检查因此不会健康，依赖其健康状态的隧道不会启动。不得通过放宽健康检查、修改代码或直接运行隧道绕过这些保护。公开 PDF 入口继续返回 503。

## 已完成和已验证

- [x] 公开模式账号密码、Cookie、Session、Origin/CSRF、普通用户权限和资源归属已实现，并具有自动测试；具体证据参见 [阶段报告](PUBLIC_WEB_IMPLEMENTATION_REPORT.md)。
- [x] 工单提案确认使用数据库事务、归属校验和行锁；失败和重复提交有自动测试。配额、任务幂等、账户限流使用 PostgreSQL；供应商调用尝试、返回 usage 和费用分别统计，费用未知不猜测。
- [x] 文件大小与公开 TXT/Markdown 上传限制、删除日志和 Qdrant/清单/BM25 流程有自动测试；公开 PDF 未启用。
- [x] Node Cloud Function 的 catch-all 路由和 `cloudFunctions.nodejs.maxDuration` 配置与 [Makers 文档](https://pages.edgeone.ai/zh/document/node-functions)、[配置文档](https://pages.edgeone.ai/zh/document/edgeone-json) 的结构一致。本地代理测试不能证明云端运行时行为。
- [x] Cloud Functions 文档记载请求/响应 body 6 MB、最大 120 秒、默认 30 秒；本项目代理限制低于这些上限，且将长任务转为轮询。上线前必须在实际项目再次验证，见 [官方限制](https://pages.edgeone.ai/zh/document/cloud-functions)。
- [x] Tunnel 镜像由 `latest` 改为 [Cloudflare 2026.10.3 正式版本](https://github.com/cloudflare/cloudflared/releases/tag/2026.10.3)及固定的多架构摘要；未启动或验证该镜像的真实联网行为。
- [x] 常规 Compose 启动时 worker 和 cloudflared 均依赖 API 健康；当前 503 硬闸会阻止它们启动。依赖并非持续运行时的安全开关，见下方 P1。

## P0：开放前必须解决

- [ ] 制定并评审显式发布控制及失败关闭机制；独立验证后再修改硬闸。现有硬闸不可用配置开关直接开启。
- [ ] 在用户明确授权后，以独立环境验证真实 EdgeOne → Quick Tunnel → 后端的端到端链路，包括 Cookie、多 Set-Cookie、CSRF、代理共享密钥、请求大小、断线、超时、错误脱敏及真实 Agent/RAG/工单路径；目前均未在真实平台验收。
- [ ] 在公开入口实施可验证的外层防滥用和频率控制。应用内限额不能阻止全部网络请求及数据库锁压力；不能信任浏览器自带的转发 IP。
- [ ] 解决 Compose 项目名覆盖可能造成的卷名碰撞：当前 `postgres_data` / `qdrant_data` 等卷名依赖 Compose 项目前缀，`-p` 或 `COMPOSE_PROJECT_NAME` 若被覆盖成原项目名可能访问旧卷。上线前需先只读核对实际卷、制定迁移方案并验证隔离；不自行更换、删除或覆盖卷。
- [ ] 针对独立 PostgreSQL、Qdrant 和完成清单完成一致性备份、隔离恢复演练和回滚预案；不得覆盖任何原有数据或卷。

## P1：开放前需要解决或明确接受

- [ ] 明确已完成任务的问题/结果、未领取附件、审计、数据库 WAL、备份及临时文件的保存期限、访问权限、清理和恢复职责；当前并非所有数据都有保留期。
- [ ] 为已认证用户创建 Agent context 和预览提案确定用户/全站频率、活跃数量及历史保留策略，并补充持久化防滥用和并发测试；当前这两条写路径不受 AI 任务配额约束，不应擅自清理历史提案以免破坏幂等和审计。
- [ ] 明确 worker 运维门禁：Compose 的 `service_healthy` 只限制启动，API 后续变成不健康不会自动停止已有 worker，`run --no-deps` 等手工操作也可能绕过；关闭时应先停止入口，再按运行手册停止 worker 并检查状态。
- [ ] 确定 `running` / `unknown` 任务的人工调查与受控恢复流程：先确认 worker 和远端调用均已结束、核对业务写入，再决定处理；不能自动重试或直接清除占槽状态。
- [ ] 跨卷故障/恢复后验证 Qdrant points、成功清单和 BM25 快照的一致性，以及中断期间删除/入库的可恢复性；当前查询会在索引锁内回读完成片段并对比快照，缺失、原文篡改或存储故障时失败关闭，但每次非空查询增加分批读取，需验证大语料延迟，且不能单凭该检查证明向量数值未变或防止绕锁外部写入。
- [ ] 公开 PDF 需要真正的进程权限及网络隔离、资源上限与恶意样本测试；未通过前继续返回 503，本机原 PDF 不受影响。
- [ ] 完成镜像供应链复核、固定 digest 与平台兼容验证；Quick Tunnel 为临时测试用途，没有 SLA、不支持 SSE、重启换地址，见 [Cloudflare 官方限制](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)。
- [ ] 审核上线时账号注册策略、邀请码分发、管理员初始化和密钥轮换；确认免费额度、预览域名有效期及实际地区可达性。

## 执行者与后续

本阶段为本地审计与自动化验证，未登录平台、创建云资源、启动隧道或上线。随机独立 public-demo Compose 项目使用合成凭据和新卷验证真实 PostgreSQL：空库迁移、模型与表结构一致、CSRF、两用户及管理员归属、4 路并发确认只产生一张工单、10 路并发预留的配额约束均通过；数据库重启后合成工单和预留记录仍存在。路由通过 FastAPI TestClient 验证，有意未经过仍关闭的公网硬闸；没有调用模型。一次性合成断言脚本位于本机 `/tmp`，不随仓库交付；测试容器停止，新建卷保留。这不是用户手动验收，也不是实际公网测试。

在 Qdrant 内存测试替身上注入缺失、篡改和读取失败，验证完成清单与 BM25 快照不一致时检索失败关闭；原有 RAG HTTP 请求与最终检索专项回归 17 项通过。此项不是 HTTP Qdrant 故障恢复演练。Cloud Function 本地构建与回环代理测试 52 项通过，不代表 EdgeOne 云端测试。

本阶段代码稳定后的 `OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 .venv/bin/python -m pytest -q`：1403 项通过、126 项跳过、0 项失败；未启用真实 PostgreSQL pytest 集成，因此上面单独记录的隔离 Compose 数据库验证不可视为这些跳过项的全部替代。公网网关与部署脚本专项 34 项通过。`docker compose -f docker-compose.public-demo.yml config --no-interpolate --no-env-resolution -q` 仅证明配置可解析，不代表服务启动或健康。

具体控制台字段、变量位置、故障排查和仅在后续获授权才执行的候选命令见 [EdgeOne 部署准备](EDGEONE_DEPLOYMENT.md) 与 [运行手册](PUBLIC_DEMO_RUNBOOK.md)。完成上述清单并由用户明确授权前，不进入实际公网部署。
