# Agent 模型失败诊断修复（2026-11-07）

## 1. 目标与实际结论

线上两个 Agent 任务在第一轮模型调用后记录为 `failed/model_error`，各发起一次供应商请求，未执行工具、未生成提案，执行槽已按 `drained` 释放。历史记录缺少具体模型错误，不能追溯认定是密钥、限流或协议问题，也不修改历史记录来补造原因。

本次修复把未来失败区分为固定的安全分类，并修正一处已经离线复现的错误：项目供应商预算拒绝继承 `ValueError`，此前会被适配器误捕获为模型响应异常。此问题不是上述两个任务的已证实根因。

## 2. 修改文件

- `infrastructure/agent_model.py`：Agent 专用安全异常、HTTP/协议分类、透传本项目预算拒绝；保留现有 RAG 适配器。
- `agent/graph.py`：将分类传递到执行结果、日志和 Trace；只允许固定分类与数值 HTTP 状态。
- `services/job_failures.py`、`web/assets/api.js`：任务 API 白名单与中文提示。
- `tests/test_agent_model*.py`、`tests/test_agent_graph.py`：真实 SDK + 合成 HTTP、编译图、协议与脱敏回归。
- `tests/test_personal_failure_completion.py`：隔离 PostgreSQL + 实际 Worker/Graph/SDK，验证最终状态及槽位。
- `tests/test_job_failures.py`、`tests/frontend_model_failures.test.mjs`：API/前端安全码、不重提任务。

源码路径均位于 `src/project_copilot/` 下，测试路径位于仓库根目录。

## 3. 核心实现

| 分类 | 含义 |
| --- | --- |
| `model_auth_error` | 模型服务 HTTP 401/403 |
| `model_rate_limit` | 模型服务 HTTP 429，与项目任务配额区分 |
| `model_provider_error` | 模型服务 HTTP 5xx |
| `model_request_rejected` | 其他模型 HTTP 拒绝 |
| `model_timeout` / `model_connection_error` | SDK 超时 / 连接失败 |
| `model_input_invalid` | 本地消息或工具 Schema 无效，未发起请求 |
| `model_response_invalid` | 返回数据未通过原有响应/usage 校验 |
| `model_output_limit` | 模型输出因长度上限结束 |
| `model_tool_protocol` | 工具 ID、类型、结构或结束原因不合法 |
| `provider_budget` | 本项目发起额度拒绝，在网络请求前终止 |

执行链：SDK → 安全异常 → Graph 固定失败码 → Worker 原有完成核验 → 持久化任务/SSE 诊断 → 中文提示。日志只含步数、固定失败码和 HTTP 状态，不写异常原文、模型输出、工具参数、提示词、凭据或请求地址。

## 4. 安全与设计边界

不放宽工具协议、最终 JSON/引用检查；无自动重试、退款、旧任务重放或提案确认。完整 HTTP 拒绝与未确认远端完成仍然区分：未知供应商结果保持 `unknown`，不能仅凭前端超时释放槽。Worker、执行租约、fencing、配额和迁移没有修改。未知 usage 仍为 `null`，不填零。

## 5. 验证

所有自动化模型响应均使用替身。真实 PostgreSQL 测试使用独立 `pc-agent-model-fix` 测试项目的 `postgres-test` / 5434 / tmpfs 和每例新建的随机测试数据库，不连接演示数据库。

| 验证命令 | 结果 |
| --- | --- |
| `PC_TEST_PYTHON=.venv/bin/python npm run verify -- --python` | 1678 passed，360 skipped，0 failed；未启用的外部服务/GPU项目明确跳过 |
| `.venv/bin/python -m pytest tests/test_agent_graph.py tests/test_agent_model.py tests/test_agent_model_failure_tracking.py -q` | 181 passed；含后补的适配器到图预算边界回归 |
| `npm run verify -- --frontend` | 静态构建通过；341 passed，0 failed，含 Playwright |
| `PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 .venv/bin/python -m pytest tests/test_personal_failure_completion.py tests/test_execution_slots_postgres.py tests/test_personal_capacity_jobs.py tests/test_worker_job_progress.py tests/test_job_failures.py -q` | 96 passed，0 failed；有2条既有 Starlette cookie 用法弃用警告 |
| `PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 .venv/bin/python -m pytest tests/test_personal_failure_completion.py -q` | 最终补齐预算耗尽/usage用例后，17 passed，0 failed |
| `git diff --check` | 通过 |

以上测试有交叉覆盖，不将数字累加为不重复测试数。首次前端回归因本地缺少锁定的 Playwright 依赖失败；执行 `npm ci --ignore-scripts --no-audit --no-fund` 后重跑通过，没有修改依赖版本。

另经用户明确授权，仅执行 **1 次**真实 DeepSeek 合成工具协议诊断：输出上限 256 tokens、零重试、零工具执行、零业务数据库访问。现有部署中的适配器接受 HTTP 200 工具调用响应；输入 311、输出 41、总计 352 tokens，约 1.514 秒。费用以账号账单为准；这不是完整 Agent 多步验收，也未复现历史故障。见 [脱敏证据](evidence/AGENT_MODEL_PROBE_20261107.json)。

## 6. GitHub 与部署

独立分支 `fix/agent-model-failure-diagnostics`，提交/PR 信息以本轮交接为准。本轮不自动合并或部署；线上容器、授权、任务和密钥保持原样。

## 7. 排查重点

新失败先按任务编号只读检查 `failure_type`、供应商尝试/完成状态和 Worker 的 `agent_model_failure` 日志。供应商 429 与项目额度 429 不同；完整响应被本地拒绝也不等于供应商没有产生费用。旧任务只读查询不会得到补造的细分类。

## 8. 后续

审核后按正常后端升级流程更新 API/Worker 镜像（纯前端发布不足以生效），保持无运行任务及 CLOSED 的升级边界。源码指纹变化后按既有管理员交互流程重新授权，不写数据库绕过门禁。此版本用于定位下一次真实故障，不能宣称两次历史故障已修复；本轮唯一真实请求授权已用完，不再自动提交诊断或业务任务。
