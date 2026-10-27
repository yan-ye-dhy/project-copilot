# 【D29 整合开发与验收报告】

日期：2026-10-27；覆盖原D29–D31。D24.1已独立推送，本批从其新架构重新实现，没有恢复旧备份覆盖源码。用户学习和手测尚未确认。

## 1. 今日开发目标

实现Agent工单提案、明确预览/确认/拒绝及真实PostgreSQL事务与并发幂等，继续使用统一Hybrid＋Rerank，保留D20和D24只读能力。

## 2. 新增和修改的文件

| 文件 | 职责 |
| --- | --- |
| [数据库模型](../../src/project_copilot/db/models.py)、[迁移](../../migrations/versions/0002_approvals.py) | 上下文、不可变提案、状态约束、确认工单唯一键与数据库触发器 |
| [审批Service](../../src/project_copilot/services/approvals.py)、[工单Service](../../src/project_copilot/services/tickets.py) | 生成指纹、凭据验证、行锁、原子确认/拒绝和实际工单创建 |
| [接口](../../src/project_copilot/api/actions.py)、[Schema](../../src/project_copilot/schemas/approvals.py) | 上下文凭据、预览、查询、明确确认与拒绝 |
| [提案工具](../../src/project_copilot/agent/proposals.py)、[工具注册](../../src/project_copilot/agent/tools.py)、[Agent图](../../src/project_copilot/agent/graph.py) | 有凭据时仅允许提案，实际图返回后端确定的待确认状态 |
| [真实PG测试](../../tests/test_approvals_postgres.py)、[接口/图测试](../../tests/test_approval_api_integration.py) | 行锁竞争、回滚、重启和权限验证 |
| [真实PG证据脚本](../../scripts/approval_pg_evidence.py)、[真实模型验收](../../scripts/approval_live_smoke.py) | 独立数据库并发证据和真实模型/检索组合路径 |
| [学习后手测](../D29_MANUAL_VERIFICATION.md)、[架构](../ARCHITECTURE.md)、计划/日志/学习与面试笔记 | 交接、边界和操作指南 |

## 3. 核心实现内容

先通过`POST /agent/contexts`生成随机上下文ID和仅返回一次的令牌；数据库只保存令牌哈希和有效期。携带凭据时Agent额外获得`propose_ticket`，工具只创建pending提案并返回真实预览，编译图由后端结束为`pending_confirmation`。没有凭据时保留只读工具。普通聊天中的确认文字没有写入权限。

`POST /actions/preview`也可创建提案，`GET /actions/{id}`读取预览，确认/拒绝接口要求同一上下文和预览指纹。确认持行锁检查过期和不可变内容，在同一事务内以action ID作为唯一创建键写入工单并更新completed/ticket_id；返回ID来自PostgreSQL。重复确认或确认/拒绝竞争返回实际终态。`search_knowledge`继续调用D24.1统一FinalRetriever。

## 4. 关键设计决策

凭据是本地范围的bearer capability，并非用户登录、租户隔离或完整权限体系；令牌不进入模型消息、日志或数据库明文。上下文有效期24小时，提案最多15分钟且不超过上下文有效期。数据库触发器保护提案不变字段和终态，completed必须关联创建键匹配的工单；唯一键与行锁跨进程生效。只保证同一action幂等，不保证不同提案语义去重。

确认和拒绝以取得锁后的数据库时间判定有效期；后来的请求读取已有终态。数据库异常和提交失败经事务回滚，API只记录类型与run_id。Agent超时不能强杀底层线程；极窄的取消与提交竞争可能留下待确认提案，但不会创建工单。服务重启恢复数据库提案和工单，不代表聊天上下文或LangGraph执行可恢复。

## 5. 自动测试结果

完整回归启用真实PostgreSQL、Qdrant和GPU：**1360 passed，0 failed，0 skipped，120.54秒**。随后追加迁移升级、延迟约束提交失败和模型越权调用的测试，最终定向回归**126 passed，0 failed**；完整1360次运行不包含之后追加的五个用例。

```bash
PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 PROJECT_COPILOT_RUN_QDRANT_INTEGRATION=1 \
PROJECT_COPILOT_RUN_FINAL_QDRANT=1 PROJECT_COPILOT_RUN_FINAL_GPU=1 HF_HUB_OFFLINE=1 \
timeout --signal=INT 300s .venv/bin/python -m pytest -q -p scripts.retain_test_qdrant
PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 .venv/bin/python -m pytest -q \
  tests/test_approvals_postgres.py tests/test_approval_api_integration.py \
  tests/test_ticket_database.py tests/test_ticket_schemas.py tests/test_agent_api.py tests/test_agent_graph.py
```

真实隔离PG中8个并发确认产生1个工单ID、表中1行；3轮确认/拒绝竞争出现两次cancelled/0行及一次completed/1行；flush后故障回滚为pending/0行，连接归还，见[数据库证据](../evidence/D29_POSTGRES_CONCURRENCY.json)。真实测试覆盖旧版0001数据库保留工单升级到0002、约束/篡改、错误凭据、指纹、过期、提交失败和重试；随机测试库在验证后由隔离辅助程序清理，未删除其他数据库/持久卷或Qdrant集合。

[真实联调](../evidence/D29_LIVE.json)使用真实DeepSeek、embedding-3、Qdrant、CUDA模型和隔离PG，ASGI真实应用内执行search_tickets→search_knowledge→propose_ticket：确认前1张预置工单、1个pending提案；应用重启后确认两次返回同一真实新工单，再次重启可查。2次DeepSeek＋1次Embedding、无重试；DeepSeek输入2774/输出199 tokens（缓存命中1024、未命中1750），实付和Embedding token未采集。没有将受控模型测试冒充真实模型；ASGI重建证明应用生命周期重启，未宣称操作系统进程或数据库重启。

## 6. GitHub 提交信息

前置D24.1提交[338fdf8fd76313cfe5780332d6ca9ea816f9ac32](https://github.com/yan-ye-dhy/project-copilot/commit/338fdf8fd76313cfe5780332d6ca9ea816f9ac32)已推送并通过`git ls-remote`核实。本批独立提交和推送结果以最终交接中实际SHA为准，报告不自引用尚未生成的提交ID。

## 7. 后续学习提示

沿HTTP→上下文凭据→模型工具→提案预览→用户明确确认→PG行锁/唯一键→事务提交→真实ticket_id追踪代码。重点解释ORM/Schema、时间与指纹、两个终态竞争、失败回滚、幂等只覆盖同一action、错误脱敏，以及Hybrid知识来源不能赋予写权限。

## 8. 下一步计划

用户先通过ChatGPT学习，再主动开启[三项手动验证](../D29_MANUAL_VERIFICATION.md)。学习与手测未由自动验收代替；D32–D34未开始。

当前保留的D20 `POST /tickets`是本地开发直建接口，本身没有身份认证，也不经过D29提案确认。D29的确认保障适用于Agent受控创建路径；如果部署到不可信网络，必须先为包括该旧接口在内的所有写接口增加真实身份认证、授权及网络隔离，不能将本地bearer上下文冒称完整安全边界。
