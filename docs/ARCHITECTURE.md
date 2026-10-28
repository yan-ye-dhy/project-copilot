# Project Copilot 最终工程架构

Project Copilot把中文研发知识检索和真实工单业务连接起来：模型负责提出工具调用，后端负责参数、来源、执行预算、上下文权限及数据库事务。所有业务知识入口使用同一个Hybrid＋BGE重排服务。

```mermaid
flowchart TD
    U[本机 HTTP 客户端] --> API[FastAPI / run_id]
    API --> RAG[/rag/ask]
    API --> G[LangGraph Agent]
    G --> M[DeepSeek 生成 Tool Call]
    M --> T[后端工具白名单 / Schema / 预算]
    T --> K[search_knowledge]
    RAG --> F[FinalRetriever]
    K --> F
    F --> D[智谱 Embedding → Qdrant Dense 10]
    F --> B[BM25 10]
    D --> R[RRF c=60 → 10]
    B --> R
    R --> E[固定 BGE Cross-Encoder → 5]
    E --> A[原文与真实来源 → 回答或 ToolMessage]
    A --> G
    T --> S[get_ticket / search_tickets]
    S --> PG[(PostgreSQL 工单)]
    T --> P[propose_ticket → pending 提案]
    P --> V[用户预览]
    V --> C[独立确认或拒绝 API]
    C --> L[上下文 / 指纹 / 有效期 / 行锁]
    L --> PG
    API -. HTTP 与业务阶段 Span .-> PH[Phoenix OTLP / UI]
    G -. 模型与工具 Span .-> PH
    F -. 检索与重排 Span .-> PH
    L -. flush / commit / rollback Span .-> PH
```

## 组件与职责

| 组件 | 职责与持久化 |
| --- | --- |
| FastAPI | 请求校验、独立Session、返回实际结果；服务端生成run_id |
| LangGraph | model/tools节点、条件路由、消息历史、工具调用ID配对、确定性预算与终止 |
| DeepSeek | 非思考模式生成回答或Tool Call；不执行SQL、不拥有确认工具 |
| 智谱embedding-3 | 文档与问题的2048维向量；请求错误无自动重试 |
| Qdrant | Dense候选和原文metadata，使用既有qdrant_data卷 |
| BM25 / RRF | 相同完成文档的词法候选；基于名次融合，不直接相加不同分数 |
| BGE重排 | 固定本地revision，query/passage成对评分后保留原文；logit不是概率或可信度 |
| PostgreSQL | 实际工单、上下文哈希和提案；postgres_data卷保存业务状态 |
| Phoenix | 接收真实OTLP/protobuf Trace，独立phoenix_data卷保存本机追踪数据库；不替代业务PostgreSQL |

## 最终检索与索引一致性

固定业务参数：embedding-3/2048、切分750/150、Dense 10＋BM25 10、RRF常数60取10、BGE取5。模型为`BAAI/bge-reranker-v2-m3` revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`、batch2、max_length512、本地缓存。GPU显式使用cuda/float16；CPU显式使用cpu/float32，同一算法不自动降级。

完成清单、查询和入库写入共用文件锁；BM25与清单不匹配时在锁内读取完成片段，验证临时快照后fsync并原子替换。上传只有在匹配快照发布后才成功；失败时可能已有完成清单，D18安全重试会核对并恢复同步。快照过期、模型/检索失败不能继续用旧快照或只返回Dense。该锁要求进程共享支持flock和原子rename的文件系统，不是跨不共享文件系统的分布式协议。

模型每API进程首次准备时加载和预热，之后复用，线程池串行推断；单进程适配本机8GB GPU。多个worker各自占显存。重排等待默认90秒、Agent单工具240秒、总300秒；超时不能强杀已在运行的CUDA线程，服务关闭等待工作结束后释放对象。部署默认限制CPU数值计算线程为2，可按机器显式调整。

## 工单权限与事务

没有上下文凭据的Agent只有三个只读工具，工单查询事务声明READ ONLY。存在有效凭据时可额外提出`propose_ticket`，后端保存不可变pending内容与指纹，并以`pending_confirmation`返回；模型不能注册或调用确认工具。

确认API要求上下文凭据与指纹，锁定提案后按数据库当前时间校验有效期。工单以action ID作为唯一创建键，和completed/ticket_id更新一起提交；重复确认同action返回原ID，确认/拒绝竞争返回实际终态。不同action不保证语义去重。上下文是局部bearer能力，数据库只保存哈希；不等于用户登录或租户授权。普通D20 `POST /tickets`仍为本地未鉴权直建接口，对外部署必须为全部写接口增加真实认证和授权。

## Trace 数据流与失败语义

HTTP中间件建立根Span并通过contextvars传播到异步工具和线程；Span只记录阶段名、状态、ID、数量、数字usage和错误类型。原始问题、文档正文、请求头、上下文令牌、模型密钥、SQL参数及异常原文不进入Trace。

业务阶段包括agent.model/tool.execute/tool.result/run、rag.dense/bm25/rrf/rerank/generate、embedding.request，以及工单、提案和db.transaction。提案确认的Service只能记录awaiting_commit，真正db.transaction退出Session.begin成功才记录committed；失败记录rolled_back。同步依赖的事务Span不跨yield附着线程上下文，以适应FastAPI在不同线程进入和退出依赖。

每个请求按trace_id独立收集完成Span，在根Span结束后单次导出OTLP。只有Phoenix返回接受且未部分拒绝，HTTP头`X-Trace-Exported=true`和已有响应字段才写true；`X-Trace-ID`可用于查询，run_id贯穿日志和Span。实际存储仍由Phoenix查询回读验证，内部日志本身不算导出证明。导出失败保留业务结果、记录脱敏诊断并标false；避免将已提交业务伪装成失败而诱发重复业务操作。

当前API均为有限JSON响应，追踪最多缓存16MiB响应、每请求最多256个Span；超限明确报告未完整导出。关追踪时继续原业务响应。迟到的工具线程不被挂到其他请求；超时Trace可能不含尚未结束的子Span。Trace各阶段存在嵌套，不能将父子耗时相加当端到端延迟。

## 部署与工程边界

[Docker部署](DEPLOYMENT.md)使用独立API镜像、一次性迁移、PostgreSQL、Qdrant与Phoenix；容器间服务名通信，宿主端口仅绑定127.0.0.1。源码和锁定依赖在镜像中；真实.env、固定模型缓存只读挂载，清单/快照可写挂载，存储服务使用命名卷。健康检查证明进程/服务就绪，模型缓存与业务链路由实际请求验收。

当前是本地单用户可演示工程，尚无完整账户/租户授权、持久聊天/Graph恢复、分布式索引锁或生产压测SLA；Phoenix本机部署也没有登录。历史冻结评测和当前线上业务分别记录，保留优化中的退化案例及未采集指标。
