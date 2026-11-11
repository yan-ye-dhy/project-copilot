# 第一周后端升级：实现与验证报告

日期：2026-11-10。执行者：Codex。基线为`bbbfb2c`；所有业务测试使用隔离资源或替身，不访问演示数据库，不调用DeepSeek/智谱，不修改现有授权、容器、模型缓存或历史评测。

## 1. 目标与范围

完成审计后分阶段增加可选Redis基础设施/原子准入、排名元数据缓存/重建租约、PG背压与资源生命周期、故障和可观测性验证。没有替换现有PG队列、OS执行证明、Hybrid＋Rerank、审批事务或公网默认CLOSED门禁。

## 2. 主要改动

| 范围 | 核心源码 |
| --- | --- |
| Redis客户端与双桶 | `infrastructure/redis_runtime.py`、`services/redis_admission.py`、`api/jobs.py`、`call_accounting.py` |
| 版本排名缓存 | `infrastructure/ranking_cache.py`、`rag/final_retrieval.py` |
| PG背压/部分索引/池 | `services/queue_control.py`、`quotas.py`、`ai_worker.py`、`db/session.py`、迁移`0016_queue_backpressure` |
| 生命周期/GPU所有者 | `services/personal_profile.py`、`infrastructure/resource_limits.py`、Worker CLI |
| 异步记账/回执/进度 | `execution_tracking.py`、`agent_model.py`、`agent/service.py`、`job_progress.py`、`agent/graph.py` |
| 可选部署/预检 | `docker-compose.redis.yml`、`deploy/public-demo.redis.yml`、`scripts/public_resources.py` |
| 可复现实验 | `docker-compose.week1-lab.yml`、`scripts/week1_backend_lab.py`、`scripts/week1_queue_lab.py` |
| 验证/教学 | 新专项、CI Redis服务、`WEEK1_BACKEND_AUDIT/ENGINEERING/VERIFICATION.md`及公开合成证据 |

## 3. 已验证的关键行为

- Redis脚本原子扣全局/账号桶，拒绝不扣任一方；配置版本隔离、时钟回退、TTL与Retry-After。关闭Redis功能时不建池/不联网，开启准入但不可用时失败关闭。
- 原幂等重放先于Redis和队列限流；新任务失败回滚。供应商准入拒绝发生在provider-open和发起计数前，实际HTTP为0。
- 缓存仅存片段UUID/分数，HMAC绑定账号与完整索引代际；命中仍检验当前Qdrant/BM25和模型。Redis错误不触发纯Dense降级。
- 真实缓存租约竞争、过期、错误owner释放、递增fence及旧持有者恢复；另有真实子进程`os._exit`后TTL接管测试。修复了旧miss在前一owner发布后再次重建的竞态。
- PG多进程/多线程领取、背压原子性、队列满后幂等重放、held unknown保守占槽；PERSONAL真OS锁持有者SQL断连时仍不能回收，实际进程终止后才按原证明回收。
- PG连接池真实耗尽和仅终止自己随机测试库的提交连接：不把失败当成功，不重放已调用runner。
- 异步记账/认证/journal慢操作时事件循环继续运行；取消不会重新提交记账或提前物理drain。同步RAG provider边界保持。
- Worker稳定复用个人配置和检索器，配置/凭据变更拒绝复用；GPU只在模型实际卸载后释放同共享目录的OS所有者锁。测试使用假评分器，未加载GPU权重。

## 4. 设计取舍与环境

本机WSL Ubuntu 24.04、Python **3.13.14**、Node **24.21.0**、20个逻辑CPU；宿主未独占。Docker Desktop集成不可用，因此没有启动/重建任何Docker容器。

真实服务是本批新建的临时进程：PostgreSQL **16.15**（5434专用测试角色，每例随机空库）、Redis **8.2.3**（16380数据库15、随机namespace）、Qdrant **1.19.2**（16433全新存储）。Redis从官方源码编译；Qdrant官方release资产摘要已核对。没有把原生PG16称为容器PG17验收；CI继续用现有PG17.11并新增固定摘要的Redis8.2.3服务。

Docker Compose **2.40.1** CLI已核对官方发布校验和。实际解析本机overlay、独立lab、公开base/Redis overlay及合成env绑定；Docker daemon实际容器启动未执行。新增Redis不开放host端口；只有专用合成lab把测试端口绑定loopback。

## 5. 自动测试与压测结果

### 最终本机回归

| 检查 | 结果与边界 |
| --- | --- |
| `PC_TEST_PYTHON=.venv/bin/python npm run verify -- --python` | 1886 passed、403 skipped、0 failed；默认不连接PG/Redis/Qdrant，不调用真实模型 |
| `npm run verify -- --frontend` | 341 passed、0 failed、0 skipped；真实Chromium/静态构建/Node HTTP回环，业务响应受控 |
| 冻结源码的全服务pytest | **2279 passed、10 skipped、0 failed**，302.93秒；真实隔离PG/Redis/HTTP Qdrant，模型替身 |
| 原生Redis停机/AOF重启专项 | 1 passed；只重启此测试新建的子进程，不停止共享Redis |
| 编译/锁文件/Compose/Git | compileall、JS语法、`uv lock --check`、实际Compose配置、`git diff --check`通过 |

离线结果收集之后增加的两个工具准入错误分类用例，定向8项全部通过；不与全量重叠计数。完整服务命令显式设置：

```bash
PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 \
PROJECT_COPILOT_RUN_REDIS_INTEGRATION=1 \
PROJECT_COPILOT_TEST_REDIS_URL=redis://127.0.0.1:16380/15 \
PROJECT_COPILOT_TEST_REDIS_SERVER_BIN=/path/to/test-only/redis-server \
PROJECT_COPILOT_RUN_QDRANT_INTEGRATION=1 \
PROJECT_COPILOT_RUN_FINAL_QDRANT=1 PC_TEST_WEB_QDRANT=1 \
PROJECT_COPILOT_QDRANT_URL=http://127.0.0.1:16433 \
PROJECT_COPILOT_RUN_FINAL_GPU=0 PROJECT_COPILOT_RUN_INGRESS_DOCKER=0 \
HF_HUB_OFFLINE=1 OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 \
.venv/bin/python -m pytest -q -ra
```

常规Docker测试资源16379可直接使用`npm run verify -- --services`，无需原生重启二进制；缺少二进制时那项明确跳过。原生本轮使用16380以同时保留另一临时版本验证，不改变CI固定端口。

### 合成压测

原始公开JSON为 `docs/evidence/WEEK1_BACKEND_BENCHMARK.json`，包含采样范围、计数、延迟分位数及修复前热点竞态失败样本。最终表在所有回归停止后重新采集；不挑选最快一轮。

测量限于本机Redis/线程协调和8毫秒合成只读工作，不含HTTP、PG完整任务链、模型/GPU、网络公网或真实费用。每档100样本、不同账号竞争，桶容量20；拒绝80是预期限流，不计作意外错误。缓存比较使用完全相同的100输入（50不同key＋50相同热点），关闭/启用各100，总实验500次。没有自动重试。

| Redis准入并发 / 样本 | 允许 / 预期拒绝 / 意外错误 | 全请求RPS | mean / P50 / P95 / P99 毫秒 |
| --- | --- | --- | --- |
| 10 / 100 | 20 / 80 / 0 | 2048.45 | 4.52 / 3.45 / 13.58 / 18.73 |
| 50 / 100 | 20 / 80 / 0 | 985.82 | 48.71 / 48.74 / 83.91 / 93.21 |
| 100 / 100 | 20 / 80 / 0 | 520.44 | 185.42 / 185.58 / 185.68 / 185.73 |

三档均无令牌超发，意外错误率0；80%拒绝率是所配置容量的预期行为。每请求延迟从执行线程开始计时，不包含executor排队；RPS是整组尝试数/整组耗时。初次建连/线程协调包含在测量中；命名空间独立，未预热到人为挑选的最快状态。

| 缓存配对场景（100并发、各100输入） | 实际计算 / 命中 | RPS | mean / P50 / P95 / P99 毫秒 |
| --- | --- | --- | --- |
| 关闭 | 100 / 0 | 8175.41 | 8.93 / 9.11 / 10.21 / 10.64 |
| 开启 | 51 / 49 | 418.69 | 230.21 / 232.64 / 236.17 / 237.13 |

启用后热点50次请求仅计算1次，无错误或fallback，结果与关闭时一致。缓存减少49次合成计算，但本次8毫秒轻工作场景中Redis协调成本使延迟上升；这不是性能提升证明。部署overlay的AOF always fsync成本没有包含在本次无持久化Redis压测中。真实模型/GPU资源节省及实际账单仍未采集。

队列实验是两个真实CPU进程、真实PG领取和结果提交，release/health/runner明确受控；3个初始任务每个只提交一次结果/terminal。真实claim后退出73导致保守占槽，11项检查全部通过，0供应商调用，总耗时3.018秒。不能将这3个样本当吞吐压测或PERSONAL业务恢复证明。

### 未执行及保留边界

- 真实GPU推断/显存性能、付费模型、实际Phoenix新增Span回读、公网端到端与Docker容器启动均未执行。
- GPU唯一owner只覆盖同主机共享manifest根；不支持整机任意进程或跨主机调度。GPU普通/个人profile切换需在执行结束后重启Worker卸载旧owner。
- 保留单Worker进程内部串行；多个CPU Worker不代表知识检索线性加速，现有索引flock仍覆盖完整检索。
- 未对当前真实unknown任务执行任何恢复/退款/重放；Redis不参与这类裁决。
- 本机压测和Mock合同均不证明生产容量或真实模型自主决策质量。

全服务回归的10项跳过为1项真实GPU和9项Docker ingress；原生Redis停机/重启已在该轮显式提供二进制并通过。独立专项不与全量累计。GitHub CI的Docker-only runner没有原生Redis二进制时会多跳过该项；以其实际Checks统计为准。

## 6. Git与交付

独立分支 `feat/week1-backend-engineering`，指向main创建PR；实际提交SHA和PR链接在交付摘要/GitHub记录提供。按用户要求不合并、不部署，main生产绑定不变；没有修改私有.env、API Key、数据库卷或现有Worker状态。

## 7. 学习重点

先读[教学文档](WEEK1_BACKEND_ENGINEERING.md)的知识映射与调用链，再依次学习客户端/令牌桶、缓存与写入端fence、PG队列及背压、真实Future与取消、模型生命周期，最后用三个实验解释失败边界。已有PG审批和Hybrid＋Rerank只阅读接入变化，无需重写。

## 8. 后续操作

三个手动实验的精确命令与预期已写入教学文档，本批不自动带练或登记用户完成。用户审核PR后再决定是否合并；真实升级仍需CLOSED、执行结束证明、0016迁移、镜像更新与原管理员授权流程。
