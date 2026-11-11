# 消息队列升级第一批开发报告

## 1. 目标

引入 RabbitMQ 专用 broker，使用 PG Transactional Outbox 解决任务与通知双写窗口；消费者保持原任务幂等、发布门禁、执行证明、配额和独立工单确认。

## 2. 文件

- `db/messaging.py`、`migrations/versions/0017_messaging.py`：Outbox/Inbox、约束和到期索引。
- `services/messaging.py`：短事务发布租约、确认后标记、重复消息和持久化延后。
- `infrastructure/rabbitmq.py`：Pika、确认发布、手动 ACK、心跳、业务线程物理收尾。
- `api/jobs.py`、`services/ai_worker.py`、`services/mq_worker.py`：事务入队及按 ID 执行。
- `scripts/ai_worker.py`、`scripts/outbox_relay.py`：独立消费者与 Relay。
- `config.py`、`release_control.py`：默认关闭的私有配置及启用配置指纹。
- 三份可选 Compose、`public_resources.py`：专用资源和隔离测试环境。
- 配套 Python/PG/broker 测试、CI、依赖与部署说明。

## 3. 核心实现

同事务保存 job/reservation/outbox；Relay 在事务外发布，mandatory 路由与 publisher confirm 成功后凭 token 标记；消费者按 job_id 幂等领取，只在结果或后续处理决定提交后 ACK。消息不包含正文和权限凭据。空闲 Relay 和执行中的消费者均维护心跳。

## 4. 设计决策

RabbitMQ 管投递，PG 管事实状态和业务事务；不以 AMQP 断开、消息重投或 Inbox 超时认定旧执行已结束。保持单 Worker/prefetch1，物理业务 Future 收尾后才重连。单节点不冒称高可用。本批不恢复旧 unknown 的模型执行。

## 5. 自动验证

执行者为 Codex，2026-11-11；模型全部为替身，不登记为用户本人手测。

- `PC_TEST_PYTHON=.venv/bin/python npm run verify -- --python`：1939 passed、442 skipped、0 failed，139.47秒。跳过项主要为显式真实服务、GPU和公开链路；不是这些服务已通过的证据。
- `npm run verify -- --frontend`：344 passed、0 skipped、0 failed，101.32秒，包含既有浏览器回归及新增验证环境隔离。
- 显式开启隔离 PG 后，Outbox/Inbox、API事务、Worker领取、原执行证明与队列回归共108 passed，52.39秒。实际 PG16.15，每例随机新库；发布/供应商边界受控。
- RabbitMQ4.2.9真实 AMQP7项＋transport单元11项通过（18 passed，16.96秒）；覆盖真实PG＋broker丢ACK、mandatory路由失败、reject-publish溢出、有限投递/死信、12秒执行期间心跳。
- 单独部署/Relay停止与transport共13 passed；资源/配置/备份兼容等定向测试通过。统计中有重叠，不将这些数目相加冒充唯一总测试数。
- 真实停止并重启本轮独立RabbitMQ进程后，已确认而未ACK的消息仍可重投。运行目录仅为`/tmp/pc-messaging-lab`，没有操作演示容器。Docker Desktop当前WSL不可用，因此不宣称实际Compose容器启动验收。

付费模型调用0次；未执行生产部署、真实库迁移、现有数据卷操作或云端配置修改。CI配置已增加独立broker服务；远端运行结果以PR实际checks为准。

## 6. GitHub

基于 PR #24 的 `5730c37`，分支 `feat/rabbitmq-outbox`。审核后创建依赖 PR #24 的独立 PR，不自动合并。真实 SHA/链接在交接和 PR 中记录，避免报告自引用。

## 7. 学习重点

Outbox 原子性与最终投递；publisher confirm/consumer ACK 区别；Inbox 与业务幂等；发布租约和物理执行证明；broker I/O 与长耗时业务线程。

## 8. 后续

第一批关键验收通过后进入 attempts/步骤结果/跨重试提案幂等，再接 Agent 检查点与 SSE 恢复。用户学习及手测单独进行，不自动部署。
