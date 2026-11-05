# Unknown 任务只读诊断

任务：`3f72446c-6aed-470f-85da-774004b97555`。检查日期：2026-11-05。

本次没有重放任务、重新调用模型、创建工单、修改历史状态、释放配额，
也没有启动或停止现有服务。下列证据来自 Codex 只读检查，不是用户手测。
PostgreSQL 查询在无网络、私有 tmpfs 中的冷副本上执行，原数据库保持停止、原卷只读。

## 实际可核实的证据

| 来源 | 结果及边界 |
| --- | --- |
| 用户真实公网反馈 | 该任务 API 返回 `unknown`，与下述冷副本 SQL 状态一致 |
| Docker default 上下文 | 公共演示的 API、Worker、PostgreSQL、Qdrant、Nginx、Tunnel、Phoenix 当前均为 exited；普通本机 API/Phoenix 仍运行，不是同一环境 |
| 公共 Worker 状态 | 开始于 `2026-11-05T07:37:29Z`，结束于 `2026-11-05T10:12:29Z`；退出码 255，`OOMKilled=false` |
| 公共 PostgreSQL/Phoenix 状态 | 与 Worker 几乎同时于 `10:12:29Z` 退出，退出码 255，`OOMKilled=false` |
| 公共 Worker 保留日志 | 49 行，没有该 job_id，也没有 `worker failure type=` 记录；只输出数量及固定异常类型，未输出原始日志内容 |
| 公共 Phoenix 保留存储 | 停止容器中的 `phoenix.db-wal` 长度为 0；将 SQLite 主文件复制到权限受限的临时目录，以 `mode=ro&immutable=1` 查询后移除临时副本；`spans` 总数为 0，本任务匹配数为 0 |
| Docker desktop-linux 上下文 | 不可用，`protocol not available`；未更改默认上下文 |

公共 PostgreSQL 已停止。本次没有启动旧数据库或 Worker，也没有查询普通本机数据库。
补充诊断使用已缓存 PG17 镜像，将约 47MB 的 `pc-public-v2-postgres` 原卷以只读方式
挂载，复制到临时容器的 256MB 私有 tmpfs。容器使用 UID999、无网络、无 capability、
只读根文件系统；PG 仅在副本上执行恢复，通过 Unix socket 和 `BEGIN READ ONLY`
查询指定 job，不暴露端口。结束后仅移除本次临时容器及 tmpfs，原卷没有写入。

冷副本 SQL 实际结果：

| 字段 | 实际值 |
| --- | --- |
| kind / state | `agent` / `unknown` |
| provider_attempts | `3`，代表供应商请求发起计数，不是重试次数或账单 |
| created_at / finished_at | `2026-11-05T09:38:17.911814Z` / `NULL` |
| usage | prompt 1389、completion 102、total 1491；cache hit 896、cache miss 493 |
| cost_status | `unknown`，未取得实际计费账单 |
| 请求提交审计 | `accepted: 1`，未看到此任务的 reused 提交 |
| payload / result | 输入仍保留；最终结果 `NULL`，未输出任何输入内容 |
| Context / actions | 存在 Context；该 Context 下 `pending_actions=[]`，没有可关联的提案或确认工单 |
| 人工恢复 | 未执行 |

这是一份停止时原卷经恢复后的冷快照证据，不是正在运行数据库的在线查询。
没有提案只能排除该 Context 的审批创建链，不能推断其他上下文或独立请求的工单状况。
Phoenix 没有留存 span 只证明当前被检查的存储没有证据，不能证明模型未调用。

## 旧代码为何无法确定具体失败阶段

已运行的 PERSONAL_DEMO 版本位于提交 `ca50d97`：

- `services/ai_worker.py::_process_one()` 把 Agent 的 `failed`、
  `budget_exhausted` 和超时事件都映射成 `unknown`，并不保存该 `AgentRun`。
- 同一函数捕获其他 `Exception` 时也只保存 `unknown`，不保留具体失败阶段。
- `scripts/ai_worker.py` 只记录逃出 `process_one()` 的异常；上述已捕获错误
  不会产生可用于本任务定位的 Worker 日志。
- `agent/graph.py` 的 `trace_span()` 只有在 `TraceRuntime` 上下文中才导出；
  旧独立 Worker 没有建立请求追踪上下文。API 提交请求的 Trace 不等同于 Worker 执行 Trace。

`usage` 非空与旧 Worker 的 AgentRun 判定分支一致：Worker 已取得包含统计的执行结果，
随后因 Agent 失败、预算耗尽或超时事件将任务记为 unknown，并丢弃结果。
这比仅有网页断线更具体；但不能区分最终 JSON/source_ids 校验、工具故障或预算原因。
三个发起计数可能包含 Agent 多步决策和检索 Embedding，不能视为同一请求重试三次。

任务创建于北京时间 17:38:17，容器统一退出于 18:12:29，间隔约 34 分钟。
多个服务同时退出提示可能存在 Docker/宿主机级中断，但不能据此认定本任务根因。
`OOMKilled=false` 也不能排除所有宿主机资源故障。

## 保留状态后的下一步

如需在用户主动恢复 **原独立公共演示环境** 后再次核实，可在本机交互终端使用
现有只读管理员工具（不会重试任务）；本次冷副本查询已完成，无需为排查重放：

```bash
cd /home/yanye/project-copilot
.venv/bin/python -m scripts.public_maintenance inspect-job \
  --job-id 3f72446c-6aed-470f-85da-774004b97555 \
  --env-file .env.public-demo
```

管理员凭据由交互输入，不放在命令、文件或 PR 中。该工具返回供应商调用
**发起计数**、usage、提交尝试、上下文提案及工单绑定；发起计数不是实际账单，
上下文内提案也不自动证明由本任务产生。未完成供应商和业务核实之前，
保持 `unknown`，不重新提交相同业务、不执行 `resolve-job`、不退款或释放占用。

本次新增任务事件记录只能改善之后的诊断，不能追补为本条历史任务生成证据。
