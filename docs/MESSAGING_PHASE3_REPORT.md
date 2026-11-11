# 消息队列升级第三批开发报告

## 1. 目标

将第二批的恢复协议接入实际编译的 LangGraph、RAG 和持久化任务事件。复用已完成步骤时仍检查当前权限、索引和最终响应契约，防止重新付费、重复提案及跨 attempt 草稿混合。范围不包含部署、旧任务重放和真实模型验收。

## 2. 文件

- `infrastructure/recovery_runtime.py`：每次物理执行的恢复上下文、原始剩余时限、来源再授权和原提案回读。
- `agent/recovery.py`、`agent/graph.py`：模型与工具步骤的标准化收据，按原始输入恢复图执行，保留原 `tool_call_id`、工具观察和最终校验。
- `rag/answering.py`、`rag/final_retrieval.py`：检索证据和模型返回收据；复用前检查完成清单、Qdrant、BM25 和固定重排模型可用性。
- `services/job_recovery.py`、`services/ai_worker.py`：从真实完整收据推导可恢复性；恢复原后端提案；明确 checkpoint 不确定性和预算失败类型。
- `services/job_events.py`、`api/job_events.py`、`api/jobs.py`：只读 attempt 元数据和排除旧草稿的事件下界。
- `web/assets/api.js`、`app.js`、`task-center.js`：识别 attempt 切换、清空旧草稿、查询原任务、显示恢复或阻塞原因。
- 配置示例、可选 RabbitMQ Compose、验证入口及新增 PG/HTTP/浏览器测试；不修改私有配置或已有数据。

## 3. 核心实现

`POST /jobs` 同一事务创建额度、任务和 Outbox → Relay 发布确认 → Inbox 和 PG 指定任务领取 → Worker 建立物理执行证明及恢复上下文 → LangGraph/RAG 在调用前持久化意图，调用后保存有限收据 → 原有 Schema、引用和权限校验 → PG 保存最终结果 → 持久化消费决定后 ACK。

符合第二批证据要求的任务，通过下一代 Outbox 进入第二次 attempt。图从入口重建，完成的模型/工具节点复用收据，未完成节点仍受原预算和 deadline 约束。工具观察同时保留原 JSON 文本，避免 JSONB 键顺序变化破坏下一步模型输入指纹。被复用的步骤不重新计费或声明真实工具执行；进度明确显示“正在恢复已保存的执行结果”。

提案提交成功但工具收据丢失时，用不可变业务映射回读原 action；不请模型再生成内容、不生成第二个 action、不自动确认。知识收据经真实完成索引再验证，工单及提案按当前所有者重新查询。缓存内容不授予权限。

## 4. 设计决策

采用现有 PG 意图和收据协议恢复操作，没有增加另一套 LangGraph 检查点数据库。模型响应先落收据，再走现有最终校验；保留响应已返回但收据未提交的不可原子化窗口，不承诺供应商 exactly-once。

API 返回安全的 attempt 元数据，旧任务响应保持原状。SSE 仍为20秒连接、5秒 heartbeat、按事件序号续读；attempt 切换发 reset，旧 terminal/delta 不再当作新结果。最终回答和审批卡片仍由已认证的 canonical GET 返回，恢复永远不重复 POST。

RabbitMQ 可选 Worker 使用1260秒退出宽限，容纳900秒任务和已启动工具的排空；宽限不构成执行已停止证明。独立复核发现本机覆盖缺少完整 Worker 定义，已补全，并新增极小GPU叠加文件对齐API/Worker/migrate配置；原本机基础和GPU Compose 保持不变。合成环境下真实Compose CPU/GPU解析及相关回归21 passed，未启动业务容器。现有公开启动/停止脚本尚未整合MQ，文档给出显式覆盖命令，避免旧360秒参数覆盖新宽限。

## 5. 自动测试结果

测试由 Codex 执行，模型均使用替身；PG16.15、RabbitMQ4.2.9、Qdrant1.19.2 HTTP、Redis8.2.3 使用独立合成环境，CI另用锁定的PG17.11服务容器。未访问演示库，未调用 DeepSeek/智谱或真实BGE推断，未运行Tunnel或云端部署。

- 统一服务回归：最终2504 passed、10 skipped、0 failed（484.57秒），显式验证了独立Redis进程重启。跳过1项真实CUDA推断和9项Docker入口测试；107条警告来自既有测试Cookie弃用和合成URL序列化，不是业务失败。首次统一运行2502 passed、11 skipped，其后新增恢复进度断言及Redis重启验收。
- `node scripts/verify.mjs --frontend`：353 passed、0 skipped、0 failed，含桌面/手机浏览器、Markdown、历史、SSE、提案和任务恢复。
- 实际图与PG专项：模型节点返回后中断、部分多工具批次、原工具消息配对、无额外 provider 调用的最终恢复、旧 token fencing、权限/索引变化拒绝、预算不重置均通过。随后77项回归验证恢复进度不伪装新执行。
- RAG/runtime 专项：原证据回读、Schema和引用再校验、完整 Worker 恢复后提交、重复投递不再次生成均通过；相关进度/Worker测试26 passed。
- Qdrant验证21项通过，其中2项使用真实 HTTP 和新合成集合，其余为受控客户端测试。
- 第一批真实 broker 已验证确认丢失、重投、死信、队列溢出、心跳和 PG 写入幂等；原生 broker 进程重启后消息仍可回读。此次没有将这些结果冒充当前演示环境的容器升级。

最后新增的2项本机Compose CPU/GPU配置解析另随相关回归执行，21 passed（5.55秒）。各专项有重叠，不将它们相加为唯一总数。GitHub CI未提供原生Redis重启/可选Compose二进制时会明确跳过，以对应head的Checks为准。

实际命令（从本批独立工作树执行；解释器复用项目虚拟环境，产物留在被忽略的 `test-results/`）：

```bash
PC_TEST_PYTHON=/home/yanye/project-copilot/.venv/bin/python \
PROJECT_COPILOT_TEST_REDIS_SERVER_BIN=/tmp/pc-week1-tools/redis-8.2.3/src/redis-server \
node scripts/verify.mjs --services
node scripts/verify.mjs --frontend
PYTHONPATH=src PROJECT_COPILOT_TEST_COMPOSE_BIN=/tmp/pc-week1-tools/docker-compose \
/home/yanye/project-copilot/.venv/bin/python -m pytest \
  tests/test_rabbitmq_deployment.py tests/test_rabbitmq_transport.py tests/test_ai_worker_signals.py -q
uv lock --check
git diff --check
```

静态构建、Python编译、JS语法和新增文档相对链接检查均通过。当前WSL未提供Docker运行连接，所以原生隔离服务的通过不等于实际应用容器升级完成。

## 6. GitHub 提交信息

分支 `feat/agent-checkpoints`，PR 基于 `feat/job-recovery`。前三项依赖依次为既有 PR #24、消息分发 PR #25、恢复协议 PR #26；本批不自动合并或部署。真实提交号、PR链接和最终CI状态由交接及PR记录提供，报告不填写自引用SHA。

## 7. 后续学习提示

1. 事务 Outbox/Inbox 解决什么，以及为什么消息重投不能直接等于模型重试。
2. logical job、physical attempt、OS租约、fencing、供应商操作证明之间的关系。
3. 收据恢复与原模型/工具输入、`ToolMessage` 配对、权限和来源再验证。
4. 跨 attempt 的业务幂等、工单提案与独立用户确认。
5. SSE事件序号、attempt下界、草稿与最终契约、费用范围未知时的表达。

## 8. 下一步计划

审核三批 PR，按依赖顺序合并后再由管理员显式安排升级；迁移为0017/0018，另需先合并上游0016。完整步骤及仅三项学习后验证见 [MESSAGING_UPGRADE.md](MESSAGING_UPGRADE.md)。当前历史任务、实际授权、数据库、Worker、Tunnel和网站均未变更。用户学习与本人手测未登记为完成。
