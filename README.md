# Project Copilot

基于RAG与LangGraph的研发知识及工单协作智能体。面向“查询研发资料、参考真实工单、分析问题、提出后续工单”的场景，将可核对的知识引用与PostgreSQL业务结果连接起来。模型提出工具调用，后端控制参数、权限、执行预算和事务；创建工单必须经过独立确认。

当前最终D32整合批次覆盖原D32–D34：容器部署、Phoenix真实Trace、七项能力演示和最终文档。D29的源码学习尚待用户明确确认；最终批次的ChatGPT学习与本人手测也未登记完成。开发与自动验收由Codex完成，用户此前参与的实际学习/手测见[协作规范](docs/COLLABORATION_RULES.md)和[开发日志](docs/DEVELOPMENT_LOG.md)。

## 技术栈和主要职责

| 组件 | 作用 |
| --- | --- |
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

需要Docker Desktop/Engine、本项目`.env`、固定模型缓存；GPU部署还需要兼容CUDA 13的NVIDIA驱动及Docker GPU支持。保留已有`.env`，按[配置模板](.env.example)补齐本项目智谱/DeepSeek密钥，不要用模板覆盖真实文件。API镜像不包含密钥或模型权重。

```bash
cd /home/yanye/project-copilot
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
| API / Swagger | `http://127.0.0.1:8002/docs` |
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

本次完整回归 **1386 passed、0 failed、0 skipped**，启用真实PG、Qdrant及GPU；另有最终指标边界定向测试。Docker CPU/GPU镜像实际构建、CPU和GPU模型实际推断、存储容器重建以及Phoenix真实收存分别留证。Mock、受控模型、真实服务和真实模型按报告区分，自动验收不是用户亲自手测。

```bash
OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 \
PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 PROJECT_COPILOT_RUN_QDRANT_INTEGRATION=1 \
PROJECT_COPILOT_RUN_FINAL_QDRANT=1 PROJECT_COPILOT_RUN_FINAL_GPU=1 HF_HUB_OFFLINE=1 \
.venv/bin/python -m pytest -q -p scripts.retain_test_qdrant
```

真实PG测试需先启动独立postgres-test服务；测试保留远端Qdrant集合。最终演示脚本为[scripts/final_acceptance.py](scripts/final_acceptance.py)，新输出目录与显式`--live`才允许模型请求；Phoenix回读和指标脚本为[scripts/final_metrics.py](scripts/final_metrics.py)。历史实验verify只读原档案，不重新调用模型或重签历史SHA。

## 学习交接与限制

[最终八项报告](docs/reports/DAY_32.md)、[学习笔记](docs/STUDY_NOTES.md)、[面试材料](docs/INTERVIEW_NOTES.md)和[仅三项本人手测](docs/FINAL_MANUAL_VERIFICATION.md)为后续集中教学入口。用户完成ChatGPT学习并主动要求后才开始手测。

原D20 `POST /tickets`仍是本地未鉴权直建接口；D29 bearer上下文不替代登录、租户和完整授权。GPU模型按进程占显存；Python等待超时不能强杀CUDA线程；文件锁依赖共享文件系统。没有生产压测、持续监控SLA、持久聊天/Graph恢复或完整语义事实裁判。费用估算基于已返回usage，不等于实际账单；首次上游失败未返回usage，未把它从调用记录中删除。

历史模块报告：[D20工单](docs/reports/DAY_20.md)、[D24 Agent](docs/reports/DAY_24.md)、[D24.1最终检索](docs/reports/DAY_24_1.md)、[D29受控确认](docs/reports/DAY_29.md)。全部核心验收通过后结束正式开发，D35不新增功能；后续学习和手测状态独立记录。
