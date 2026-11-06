# Project Copilot

基于RAG与LangGraph的研发知识及工单协作智能体。面向“查询研发资料、参考真实工单、分析问题、提出后续工单”的场景，将可核对的知识引用与PostgreSQL业务结果连接起来。模型提出工具调用，后端控制参数、权限、执行预算和事务；创建工单必须经过独立确认。

**普通用户入口：[研发协作工作台](http://127.0.0.1:8002/)**。服务启动后直接在浏览器上传资料、提问、查看工单及确认提案，无需手工填写API请求头。开发接口文档继续保留在[Swagger](http://127.0.0.1:8002/docs)。

项目包含浏览器工作台、容器化部署与可视化追踪。可查看[工作台截图](docs/evidence/FRONTEND_DESKTOP_20261031.png)、[前端验收记录](docs/evidence/FRONTEND_VERIFICATION_20261031.json)和[端到端验收结果](docs/FINAL_ACCEPTANCE.md)。

公网演示改造正在进行：新界面参考[桌面预览](docs/evidence/PUBLIC_WEB_DESKTOP.png)和[移动端预览](docs/evidence/PUBLIC_WEB_MOBILE.png)。EdgeOne前端已部署，完整公网安全验收仍待完成，后端默认CLOSED。个人体验可单独启用 [PERSONAL_DEMO](docs/PERSONAL_DEMO.md)：一个已有账号、最长24小时、每次授权最多500个AI任务，三任务占槽、单Worker顺序执行，不冒充STAGING/PUBLIC。[高额度、0015迁移与升级说明](docs/PERSONAL_DEMO_CAPACITY.md)包含模式隔离、预算及失败边界。正式准备工作和限制见[部署准备](docs/EDGEONE_DEPLOYMENT.md)、[安全模型](docs/SECURITY_MODEL.md)与[运行手册](docs/PUBLIC_DEMO_RUNBOOK.md)。

阶段1.5已增加持久发布状态、Worker门禁、数据库创建限额、异常任务调查及新资源冷备恢复；当前默认CLOSED。测试边界和下一步受控STAGING条件见[阶段报告](docs/STAGE15_REPORT.md)与[发布清单](docs/RELEASE_CHECKLIST.md)。

聊天体验支持安全 Markdown、真实任务阶段、RAG 流式草稿、停止等待和任务编号恢复，架构、事件协议及部署边界见[聊天体验说明](docs/CHAT_EXPERIENCE.md)。停止等待不取消后台任务或退款；刷新不会重新提交。

聊天界面使用紧凑顶栏、可展开额度和默认收起的任务中心，长回答与工单提案拥有更大的阅读空间。用户头像采用本地原创剑客绘像；历史失败/待核实任务可在本浏览器隐藏并恢复，不改变后台结果或额度。[布局、桌面/手机预览及测试说明](docs/CHAT_SPACE_AVATAR.md)包含静态部署影响；EdgeOne前端更新无需重建后端镜像。

[历史对话](docs/CHAT_HISTORY.md)支持返回、搜索、重命名、删除和主动开启IndexedDB保存；默认仅本页内存。旧提案只读，继续对话建立新的安全Context。任务中心可经GET核实后删除failed/unknown的本地恢复索引，不取消后台任务或退还额度。

## 在浏览器中使用

| 页面 | 操作 |
| --- | --- |
| 智能协作 | 输入研发问题并发送；“智能协作”可联合查询知识与真实工单，“知识问答”只查询资料。点击回答依据可查看原文或工单详情；运行详情提供实际请求编号和可用usage |
| 知识资料 | 选择或拖入TXT、Markdown、PDF，点击“上传并加入知识库”；成功后回到智能协作提问。文件大小依据当前后端配置显示 |
| 工单 | 按关键词和状态查询、翻页及查看真实详情。“新建工单”先生成待确认提案；核对标题/描述后，点击确认创建或拒绝 |

Agent也可在对话中提出工单提案。浏览器会从后端重新读取提案内容，并带对应上下文凭据及指纹调用独立确认接口；聊天中的“确认”不会替代按钮操作。确认结果不明确时，界面提供查询状态，不自动重发创建操作。

上下文凭据和本次上传记录仅保存在当前页面内存；对话默认也只保留本页，用户主动开启后可将展示正文保存至本浏览器IndexedDB，按认证账号隔离且不恢复执行权限。公开聊天任务的localStorage仅保存账号隔离的任务编号等非凭据元数据，可查询原任务；成功入库的资料和已提交工单仍由服务端保存。工单页显示实际本页条数，后端没有提供全局总数。本机模式不要求登录，历史仅内存；面向公网的账户、多用户隔离和资料管理仍须完成发布前安全与部署验收。当前不支持工单编辑或删除。

## 技术栈和主要职责

| 组件 | 作用 |
| --- | --- |
| HTML / CSS / JavaScript Modules | 中文响应式工作台、同域API交互、依据查看和明确确认；由FastAPI直接提供，无Node运行时依赖 |
| FastAPI / Pydantic | HTTP请求、上传及结构化输出校验，真实来源与业务DTO |
| LangGraph / DeepSeek | State、节点、条件路由、模型Tool Call与ToolMessage循环 |
| 智谱embedding-3 / Qdrant | 2048维文档与问题向量、Dense候选及原文metadata |
| jieba / BM25 / RRF | 中文与技术词分词、词法检索、按名次融合 |
| 本地BGE Cross-Encoder | 固定revision对融合候选重新排序，保留原文和来源身份 |
| SQLAlchemy / Psycopg / Alembic / PostgreSQL | 工单、不可变提案、行锁、唯一创建键、事务与迁移 |
| OpenTelemetry / Phoenix | HTTP、模型、工具、检索、重排与事务的真实Trace |
| Docker Compose / uv | 锁定依赖、API镜像、容器网络、健康及持久化卷 |

完整模块、数据流及权限图见[架构说明](docs/ARCHITECTURE.md)。

## 核心业务流程

用户描述研发问题 → Agent查询工单和知识 → 综合真实来源 → 后端生成pending提案 → 用户预览 → 独立确认接口 → PostgreSQL事务创建 → 返回真实ticket_id，并按run_id/Trace ID观察执行。

知识检索固定为 **Dense 10＋BM25 10 → RRF c=60取10 → BGE重排取5**。切分750/150，模型`BAAI/bge-reranker-v2-m3`固定revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`、batch2、max_length512。GPU为cuda/float16，CPU为显式cpu/float32；检索异常明确失败，不静默回退纯Dense。

上传成功前同步匹配完成清单的BM25快照；跨进程文件锁、临时文件校验和原子发布保护一致性。重复上传及失败恢复沿用稳定ID与D18成功清单，模型缓存只从本地读取。

## Docker运行

本机已有GPU镜像和模型缓存时，日常启动直接执行`bash start.sh`。脚本会启动依赖并重新建立API挂载，自动沿用现有本机验收配置（若存在`data/auto-final/deploy.env`），保留数据卷；不构建镜像、不调用模型。首次部署或CPU模式仍按下面步骤及部署指南操作。

需要Docker Desktop/Engine、本项目`.env`、固定模型缓存；GPU部署还需要兼容CUDA 13的NVIDIA驱动及Docker GPU支持。保留已有`.env`，按[配置模板](.env.example)补齐本项目智谱/DeepSeek密钥，不要用模板覆盖真实文件。API镜像不包含密钥或模型权重。

```bash
cd project-copilot
export PROJECT_COPILOT_CONTAINER_UID="$(id -u)"
export PROJECT_COPILOT_CONTAINER_GID="$(id -g)"
mkdir -p data/manifests

docker compose --env-file .env.example \
  -f docker-compose.yml -f docker-compose.deploy.yml -f docker-compose.gpu.yml build api
docker compose --env-file .env.example \
  -f docker-compose.yml -f docker-compose.deploy.yml -f docker-compose.gpu.yml up -d --wait api
```

完整栈为API、一次性迁移、PostgreSQL、Qdrant、Phoenix。API等待存储/追踪健康及Alembic成功退出；PostgreSQL/Qdrant复用既有项目卷，Phoenix有独立卷。CPU部署去掉GPU overlay并构建CPU镜像，详细条件、代理排查、模型挂载和停止/重建方法见[部署指南](docs/DEPLOYMENT.md)。

| 入口 | 本机地址 |
| --- | --- |
| 普通用户工作台 | `http://127.0.0.1:8002/` |
| 开发接口 / Swagger | `http://127.0.0.1:8002/docs` |
| API存活检查 | `http://127.0.0.1:8002/health` |
| PostgreSQL | `127.0.0.1:5433`，容器内`postgres:5432` |
| Qdrant | `http://127.0.0.1:6433`，容器内`qdrant:6333` |
| Phoenix UI | `http://127.0.0.1:6007`，容器内OTLP为`phoenix:6006/v1/traces` |

部署使用`.env.example`作Compose插值，真实`.env`只读挂到API；容器环境覆盖数据库/Qdrant URL为服务名。模型目录只读，清单目录读写。当前仅绑定localhost，无完整身份认证或Phoenix登录，不直接对外暴露。原数据卷和缓存保留，停止服务不删除数据。

## HTTP功能和确认边界

| 接口 | 行为 |
| --- | --- |
| `POST /documents/upload` | multipart文件字段`file`，TXT/MD/PDF；成功返回HYBRID_RERANK及真实文档/片段ID |
| `POST /rag/ask` | `question`，返回结构化回答及原文citations |
| `POST /tickets`、`GET /tickets`、`GET /tickets/{id}` | 普通工单创建、状态/关键词筛选、真实详情 |
| `POST /agent/ask` | 问题及可选有界历史；只读工具get_ticket、search_tickets、search_knowledge |
| `POST /agent/contexts` | 返回局部上下文凭据；令牌只首次返回、数据库仅存哈希 |
| `POST /actions/preview`、`GET /actions/{id}` | 持久化或查看不可变待确认内容、指纹和有效期 |
| `POST /actions/{id}/confirm`、`reject` | 同上下文＋payload_fingerprint，明确确认或拒绝 |

Agent带`X-Copilot-Context-ID`和`X-Copilot-Context-Token`且认证有效时，可使用propose_ticket；普通聊天里的“确认”不授权写入。后端持行锁检查有效期，以action ID唯一创建键把工单与completed/ticket_id一并提交；重复确认同action返回相同ID。不同action不保证语义去重，历史文本不是服务端持久会话。

响应X-Run-ID与X-Trace-ID可关联Phoenix。只有当前请求OTLP导出收到接受回执才显示X-Trace-Exported=true；仍需从Phoenix回读证明存储。导出失败保留业务响应并记录明确诊断，关追踪不影响业务。Span仅含安全元数据，不记录原文、请求头、令牌或异常原文。

## 七项能力和真实证据

最终逐项结果、样本量、成功/失败Trace及费用边界见[最终验收](docs/FINAL_ACCEPTANCE.md)，原始记录见[最终审计](docs/evidence/D32_FINAL_ACCEPTANCE.json)。

| 能力 | 演示入口 |
| --- | --- |
| 上传中文研发资料 | documents/upload；新文档返回稳定片段ID |
| 知识问答和证据 | rag/ask；答案与citations原文核对 |
| PostgreSQL真实工单 | tickets列表、详情及数据库独立核对 |
| 知识/工单联合分析 | agent/ask；两类实际来源和工具事件 |
| 提案与独立确认 | pending预览→错误指纹拒绝→确认/重复确认→实际ID |
| Phoenix查看执行 | UI＋实际REST回读的成功、业务失败与上游失败Trace |
| RAG优化实验 | [D17保留集](evals/reports/FINAL_HOLDOUT.md)、[D16重排](evals/reports/RERANK_V3.md)及公开原始产物 |

开发集26道可答题的Hybrid→Rerank证据MRR@5为0.897436→0.971154，Recall@5均1；Recall@3从1降至0.935897，保留4题改善/2题退化。冻结后的保留集9道可答题，V0→Final的MRR@5为0.944444→1，Recall@5均1；另1道无答案排除于这些均值。自建小数据集不证明普遍提升或回答准确率，保留集未用于重新调参。

## 测试与复现

后端开发验收完整回归为 **1386 passed、0 failed、0 skipped**，启用真实PG、Qdrant及GPU；另有最终指标边界定向测试。Docker CPU/GPU镜像实际构建、CPU和GPU模型实际推断、存储容器重建以及Phoenix真实收存分别留证。前端扩展的自动检查与浏览器验证记录在[前端验收记录](docs/evidence/FRONTEND_VERIFICATION_20261031.json)；Mock、受控模型、真实服务和真实模型按证据区分。

前端本轮默认Python回归1336 passed、63 skipped、0失败；客户端25项及受控Chromium15项通过。真实Docker浏览器闭环、PostgreSQL独立核对与Phoenix7条Trace回读另行通过；门禁跳过的旧真实集成测试未重跑。只增加HTML/CSS/JS与静态路由，无新增生产依赖。

```bash
OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 \
PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 PROJECT_COPILOT_RUN_QDRANT_INTEGRATION=1 \
PROJECT_COPILOT_RUN_FINAL_QDRANT=1 PROJECT_COPILOT_RUN_FINAL_GPU=1 HF_HUB_OFFLINE=1 \
.venv/bin/python -m pytest -q -p scripts.retain_test_qdrant
```

真实PG测试需先启动独立postgres-test服务；测试保留远端Qdrant集合。最终演示脚本为[scripts/final_acceptance.py](scripts/final_acceptance.py)，新输出目录与显式`--live`才允许模型请求；Phoenix回读和指标脚本为[scripts/final_metrics.py](scripts/final_metrics.py)。历史实验verify只读原档案，不重新调用模型或重签历史SHA。

## 已知限制

原D20 `POST /tickets`仍是本地未鉴权直建接口；D29 bearer上下文不替代登录、租户和完整授权。GPU模型按进程占显存；Python等待超时不能强杀CUDA线程；文件锁依赖共享文件系统。没有生产压测、持续监控SLA、持久聊天/Graph恢复或完整语义事实裁判。费用估算基于已返回usage，不等于实际账单；首次上游失败未返回usage，未把它从调用记录中删除。
