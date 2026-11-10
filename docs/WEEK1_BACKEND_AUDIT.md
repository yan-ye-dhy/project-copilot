# 第一周后端工程化：现状审计与升级清单

基线：`bbbfb2c`，2026-11-10。工作区干净、main 与 origin/main 一致，开始时无开放 PR。本批独立分支实现，不修改现有运行服务、私有配置、业务数据库和历史评测。

| 知识 | 已有且保留 | 本批处理 |
| --- | --- | --- |
| GIL、线程、锁 | `ExecutionTracker` 跟踪真实 Future；重排专用线程池；索引跨进程 flock | 修复个人配置派生造成检索器/模型重复常驻；补资源生命周期与上限验证 |
| asyncio、阻塞 I/O | 工具使用 `tracked_to_thread`；SSE 数据库读取使用受控线程池 | 检查异步模型前后同步数据库操作，采用保留物理执行跟踪的受控卸载 |
| PG 事务、索引、行锁、唯一键 | 幂等 reservation、SKIP LOCKED、审批行锁及唯一创建键、原子结果提交 | 保留权威状态，增强队列背压、多 Worker 与故障证据 |
| Redis、令牌桶、缓存 | 尚无 Redis 生产依赖或客户端 | 可选 Redis 生命周期；Lua 双粒度令牌桶；仅缓存版本绑定的检索排名元数据 |
| 消息队列、ACK、重试 | PG 持久任务、Worker；无未知任务自动重放；GET 恢复不重发 POST | 增强积压准入与只读失败诊断；仅安全只读操作有界退避，不重试付费/业务写入 |
| 租约、故障恢复、fencing | PERSONAL OS 租约、数据库执行 token、供应商 in-flight、物理 drain | 保留原协议；Redis 租约仅用于缓存重建，不能证明模型/GPU执行终止 |
| 高并发综合 | PG 账号/全局配额，连接池等待上限，单 Worker 顺序执行 | 可选速率准入、背压、资源保护、真实隔离服务并发及故障测试 |

## 已确认的具体问题

1. `personal_runtime_settings()` 每任务创建 Settings，原检索器按对象 id 缓存并强引用，Worker 不释放该缓存；会重复常驻模型/线程池。
2. `DeepSeekToolModel.ainvoke()` 的同步 provider accounting 包含数据库/依赖探测，可能阻塞事件循环。卸载必须保留取消后的真实线程证明。
3. 最终检索现有文件锁覆盖完成清单、Qdrant回读、Embedding和重排；多个 Worker 不意味着 GPU 并行，也不能用 Redis 锁替换它。
4. `DenseIndexSpec.index_version` 只代表配置，不能单独作为新增/删除后的缓存版本。缓存必须绑定完整完成清单，且仍核对真实 Qdrant/BM25及模型可用性。
5. 公网资源预检严格限制服务/网络。Redis 采用独立、显式 overlay；不得放宽既有卷/网络归属检查或误宣称旧公网预检已经覆盖新增组件。

## 分阶段清单

- Phase 1：可选 Redis、连接/关闭与超时、Lua 全局/账号令牌桶、任务与供应商准入、失败关闭。
- Phase 2：Cache Aside 排名缓存、TTL 抖动/负缓存、查询 HMAC、版本及权限隔离、随机 owner 租约与原子条件发布。
- Phase 3：检索器复用/关闭、异步阻塞边界、持久队列背压及资源上限、只读失败视图。
- Phase 4：隔离 PG/Redis、并发与故障注入、合成压测、脱敏 Trace、完整回归和教学文档。

## 不适合引入或不能承诺的能力

- 不用 Celery/RabbitMQ/Kafka 替换既有 PG 队列；当前不提供自动重试、消息重放或 exactly-once 外部请求保证。
- Redis 不保存问答正文、文档正文、凭据或授权 Context；不缓存权限判断或工单事务结果。
- Redis 缓存重建锁不能让当前共享文件系统协议变成跨主机索引协议。
- Redis 断线、锁 TTL 或 HTTP 超时不证明旧模型/GPU执行终止。
- 无真实付费模型/GPU压测；合成吞吐仅证明所测组件和资源边界。

验收结果、执行环境及三个手动验证入口在 `WEEK1_BACKEND_ENGINEERING.md` 汇总；实现前的本表不代表各阶段已经验收通过。
