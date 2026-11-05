# PERSONAL_DEMO：执行结束后自动释放并发槽

本文记录PR #11的0014实现与测试。当前个人模式额度和0015失败结果改进以[高额度个人体验](PERSONAL_DEMO_CAPACITY.md)为准；OS租约、供应商跟踪和物理排空证明继续保留，历史证据不补造。

## 1. 目标与范围

将任务结果与并发占用解耦，复用现有Worker、数据库及Manifest共享卷，不新增服务。仅为新版本领取的PERSONAL_DEMO任务建立执行证明；STAGING/PUBLIC的既有发布规则、并发额度、三任务上限和模型预算不变。

`state=unknown`表示结果未确认，`slot_released_at`表示是否仍占并发槽，两者可以独立。释放槽不代表成功、不退还额度、不清除usage/费用/幂等记录，也不会重新调用模型、工具或创建工单。

## 2. 文件与职责

| 文件 | 职责 |
| --- | --- |
| `db/executions.py`、`db/models.py`、`0014_execution_slots.py` | 独立槽时间、执行身份、供应商未完成计数和业务关联；只向前迁移 |
| `services/execution_slots.py` | 私有文件租约、结束证明、数据库fencing与回收 |
| `infrastructure/execution_tracking.py` | 跟踪真实Future及供应商完整响应，等待真实后台工作退出 |
| `services/ai_worker.py`、`services/quotas.py` | 领取时登记，收尾/后续准入时回收，额度继续累计 |
| `models.py`、`agent_model.py`、`call_accounting.py` | 外呼前持久化尝试，完整响应后确认结束；不重试 |
| `agent/backend.py`、`agent/proposals.py`、`rag/final_retrieval.py` | 跟踪后台线程；提案事务绑定执行身份与action_id |
| `services/public_maintenance.py` | 只读显示独立占槽状态；保留特殊异常的受控人工入口 |
| `test_execution_lease.py`、`test_execution_tracking.py`、`test_execution_slots_postgres.py` | 文件锁、线程、供应商、进程退出、真实PG事务和竞争验证 |

## 3. 完整执行流程

1. 原`POST /jobs`仍持久化幂等任务并占槽。Worker在领取事务中建立唯一token、私有锁文件及执行行。
2. Worker持有原数据库执行锁和该任务真实OS文件锁。外呼前在同一事务核验token、增加尝试数和`provider_open`，再执行一次供应商请求。
3. 完整HTTP响应（即使答案校验失败），或流式实际`[DONE]`，确认一次供应商调用结束。超时、取消、失联和未结束的流保留不确定标记。
4. 原任务结果照常写入。所有真实线程/Future退出后才记录`local_done_at`，检查供应商和Agent业务绑定，fence旧token并释放槽。任务可以继续为unknown。
5. 异常退出后，后续PERSONAL_DEMO准入/Worker领取检查尝试获取原锁文件；只有原执行已不持锁、文件身份未变、供应商无未决调用且业务核对通过，才事务性回收。没有按年龄自动认定死亡的逻辑。

Agent提案写入与`execution.action_id`同一事务提交；结束后旧token不能外呼或写新提案。相同执行的再次提案复用既有action；真正创建工单仍必须经独立确认接口，原行锁、唯一键和确认幂等不变。

## 4. 自动与人工边界

| 情况 | 行为 |
| --- | --- |
| 正常成功、最终回答校验失败或unknown；本地工作和供应商均已结束 | 自动放槽，结果状态及费用记录不变 |
| 工具超时，但真实线程/本地GPU Future仍运行 | 持锁等待，不放槽、不强杀 |
| 新协议RAG进程死亡，原锁空闲、供应商明确无未决调用 | `dead_readonly`自动回收 |
| 新协议Agent进程死亡，另通过提案/工单绑定核对 | `dead_agent_fenced`自动回收；不重复业务操作 |
| 供应商超时/流中断、未收到完成确认、数据库确认写入失败 | 保留占槽，人工核实供应商结果；本地退出不证明远端结束 |
| 历史任务没有执行行、锁缺失/损坏/替换，或业务绑定不一致 | 不猜测、不补造证明，保留人工入口 |
| 上传进程异常退出，索引副作用未核实 | 保留人工入口；正常完整上传收尾可放槽 |

迁移仅保留旧成功/失败任务原有的释放语义，**不释放旧running/unknown**。历史任务`3f72446c-6aed-470f-85da-774004b97555`没有此协议的执行证明，因此本改动不能自动清除它；本轮没有访问或修改真实任务数据库。

已保存成功/失败结果、但供应商完成回执未能写库的登记任务，也可在同样的人工核实后只释放槽；原成功/失败状态和时间戳不变。审计记录保存真实的先前状态，不把成功任务伪装成unknown。

本协议适用于当前单主机Linux/WSL部署：API和Worker必须以相同UID访问同一个支持`flock`的本地Manifest卷。锁文件位于`<manifest_dir>/.job-executions`（目录700、文件600）；不得删除、复制替换或重建它们作为结束证明。不支持多机各自本地锁目录/NFS、混用旧Worker或管理员绕过业务服务写库。进程仍活但真正工作永久卡住时宁可阻塞；槽释放也不修复已有GPU/检索器故障。

## 5. 自动验证

执行者为Codex。真实PG只使用`postgres-test`端口5434的随机测试数据库，测试模型/HTTP响应均为替身，不连接真实演示库、不发起付费请求。覆盖物理线程取消、实际子进程退出、数据库断连仍活、供应商未决、旧token隔离、Agent提案/真实工单及重复确认、竞争准入、迁移与历史任务保留。

实际命令（工作目录为隔离功能分支工作树，Python使用现有项目虚拟环境）：

```bash
PYTHONPATH=src:. .venv/bin/python -m pytest -q --disable-warnings
PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 PYTHONPATH=src:. \
  .venv/bin/python -m pytest -q --disable-warnings
PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 PYTHONPATH=src:. \
  .venv/bin/python -m pytest -q --disable-warnings \
  tests/test_execution_slots_postgres.py tests/test_public_operations_postgres.py
node --test --test-isolation=none tests/*.test.mjs
git diff --check
```

| 验证 | 实际结果 |
| --- | --- |
| 默认Python全量（PG需显式开启） | 1541 passed、260 skipped |
| 开启隔离PG的全量回归 | 1784 passed、17 skipped；0 failed，212.08秒 |
| 补充人工兜底后PG专项 | 43 passed，含33项执行槽测试和10项原维护回归；0 failed |
| Node代理/界面回归（使用本机Playwright缓存） | 200 passed、0 skipped、0 failed |
| Git diff检查 | 通过 |

全量PG首次运行发现精确表清单遗漏事件表及执行表；补齐断言后重跑通过。4项额外人工兜底测试在全量采集后补充，已单独运行通过。17项可选外部环境测试未启用，不据此宣称真实模型、Qdrant或Docker发布验收。Mock供应商完成只证明代码协议，不代表真实供应商超时后已停止；本轮没有部署镜像、迁移演示库或验证真实模型。测试产生的弃用警告不影响通过，未放宽检查或重试业务任务。

## 6. Git交付

独立分支`feat/personal-execution-slots`，基于`origin/main`的`d8407d7`。不包含尚未合并的PR #10视觉分支，不修改用户现有工作树。审核后创建指向main的PR，实际提交及链接在交接中提供；不自动合并或部署。

## 7. 后续核对入口

已有管理员只读命令：

```bash
.venv/bin/python -m scripts.public_maintenance inspect-job --job-id <任务UUID>
```

分别观察`state`、`slot_held`、`slot_released_at`和`execution`。`execution=null`是缺少新协议证明，不是可以按超时清除。特殊异常仍用[运行手册](PUBLIC_DEMO_RUNBOOK.md#异常任务调查与恢复)的既有CLOSED/停机/供应商与业务核实流程；人工处理也只释放槽、保留unknown，不退款或重放。

## 8. 后续操作与限制

本轮仅提交PR。后续经用户审核授权才能更新代码、在停止旧API/Worker等写进程后执行0014迁移并使用一致的新API/Worker镜像；新PERSONAL_DEMO授权应按既有代码身份校验重新生成。不得为了上线自动续期、放宽限额或沿用旧工件。历史unknown的人工核实与后续部署均未执行，等待用户决定。
