# Agent 任务合同基线 v1

本模块使用**实际编译的生产 `AgentRunner` / LangGraph**、预设模型响应和内存合成工具。30 个固定场景覆盖知识、工单、联合查询、条件提案、证据不足和异常；按场景族划分为 20 dev / 10 holdout，族之间无交叉。它验证图执行、工具权限/次数、来源身份、提案终态及失败统计，不验证模型自主规划能力。所有题和脚本公开，holdout 也不是保密盲测；人工 gold / 事实支持仍待复核。

不读取 `.env`、不连接数据库/检索/模型服务，无新增依赖，无 live 命令。旧检索数据、选型、holdout 结果和证据不作修改。本数据集独立存放在 `evals/datasets/agent_tasks_v1/`，冻结哈希不匹配会拒绝加载；新版本另建目录，不能修改 v1 后冒称同一基线。

## 运行

在项目根目录，使用项目虚拟环境和 `PYTHONPATH=src:.`：

```bash
PYTHONPATH=src:. .venv/bin/python -m evals.agent_tasks contract --output /tmp/agent-dev-v1
PYTHONPATH=src:. .venv/bin/python -m evals.agent_tasks contract --split all --output /tmp/agent-all-v1
PYTHONPATH=src:. .venv/bin/python -m pytest -q tests/test_agent_task_benchmark.py tests/test_agent_graph.py tests/test_eval_freeze.py
```

默认只执行 dev。`--split holdout` / `--split all` 为显式选择。输出 `records.json` 和 `summary.json`，已有输出路径会报错，不能覆盖归档。合同不通过或缺少题目时退出码为 1；预设故障被正确隔离时合同可通过，但故障终态仍计入失败分母。

`records.json` 保留真实图结果、工具事件和耗时；耗时依赖机器，不作性能结论。相同代码和依赖的重复运行应有相同合同评分、计数和观察字符数，耗时不要求逐字相同。

## 指标边界

- `contract_success / contract_failure`：全部所选任务为分母，缺记录计失败；按 split / category / family 同时给分母。只检查预设终态、来源ID集合、成功工具、错误类型、调用上限及提案次数。**不对回答语义自动判分**。
- `terminal_failures`：`failed` / `budget_exhausted` 的真实图终态，不因其是预设异常就删去；`failure_type_counts` 保留类别。
- `model_calls` 是模型接口尝试次数，替身不等于真实请求；`tool_attempts` 包括拒绝，`tool_handler_executions` 为实际进入处理函数的次数。本基线处理函数是替身，`external_model_requests` / `real_service_executions` 均为 0。外部导入缺测则为 null，并保留已知和、测量覆盖数及分母。
- `prompt_characters` 在每次模型接口入口测量：消息去除随机 id 后的 JSON 字符、工具 Schema 字符、已观察 ToolMessage.content 字符及完全相同载荷去重后的字符。累计值包含每次调用重发的历史；不是供应商 token、网络字节或计费统计。跨不同 query 返回同一资料、搜索后详情、相同参数拒绝重复等场景支持后续观察压缩对照。语义相近但序列化不同的内容不会被“完全相同”指标识别。
- 替身不伪造 usage；缺失为 null，不能填 0。已保存的逐步 `model_events` / `usage` 可用于外部模型记录复核。
- `human_factual_support` 只接受显式人工标签，绑定 dataset / answer / evidence SHA；来源编号合法不等于事实得到支持。未标注、无法评估、支持/不支持的分母分别记录。`model_decision_success` 始终 null，因为本评分器没有完整自主决策与回答质量的裁判。
- provenance 记录数据、配置、评测器与实际图/工具/schema 代码的 SHA、Git HEAD、依赖版本。哈希使用规范 JSON 的 SHA-256；源文件内容以 JSON 字符串纳入哈希。HEAD 是运行起点；代码定位依据实际导入的AgentRunner，跨工作区时同时记录生产代码根路径与HEAD；工作区真实代码以逐文件指纹为准，不用 HEAD 掩盖未提交代码。

## 离线导入与人工标注

```bash
PYTHONPATH=src:. .venv/bin/python -m evals.agent_tasks score --split all --records /path/records.json --output /tmp/imported-summary.json
```

导入容器沿用 `records.json` 格式：`schema_version: 1`、`provenance`、`records`。外部结果必须设置 `execution_mode: "recorded_external"`，提供真实来源说明 `model_decisions`、`dataset_sha256`、`code_sha256`、`config_sha256`；如包含 `config` 则校验其哈希。每条记录必须含唯一 `case_id` 和现有 `AgentRun` 合同的 `result`。事件/执行计数必须一致；未知 `prompt_observations`、外部请求数、真实服务执行数和后台执行清单保持 null。未知题号、重复题号、数据哈希或计数错误会拒绝，缺题保留在选定分母中；所选题目未完整覆盖时，总请求数/usage仍为null，只报告已知小计。

评分命令只读取已有记录，不会自动调用模型重跑、补齐或生成 live 请求。外部声明未经本评分器独立见证；把脚本记录改名不能形成真实模型证据。

人工审阅文件是 JSON 数组，每项格式如下。这里仅展示格式，仓库没有预填“人工已支持”的标签：

```json
{
  "case_id": "knowledge_lookup-1",
  "dataset_sha256": "从 provenance 复制",
  "answer_sha256": "从 summary.cases 对应题复制",
  "evidence_sha256": "从 summary.cases 对应题复制，绑定完整来源内容",
  "reviewer_kind": "human",
  "reviewer": "实际审阅者",
  "verdict": "supported"
}
```

`verdict` 可为 `supported`、`unsupported`、`unassessable`；按每题 `review_rubric` 对照原始合成来源审阅，再为 `score` 增加 `--reviews /path/reviews.json`。脚本生成标签或测试中的示例审阅者不能登记为用户人工复核完成。

## 观察压缩前后对照

本次固定全量结果位于 `evals/results/agent-contract-v1-20261107/`。后续图改动可在新目录重跑同一数据，然后比较：

```bash
PYTHONPATH=src:. .venv/bin/python -m evals.agent_tasks compare --before evals/results/agent-contract-v1-20261107/summary.json --after /tmp/agent-after-v1/summary.json --output /tmp/agent-delta-v1.json
```

比较要求数据、配置、模式、分母、依赖和完整观察覆盖一致，允许代码指纹不同。输出合同成功数、真实处理函数执行次数和观察字符变化；不自动推断速度、费用或模型正确率提升。修改 System Prompt / 预算应另记实验配置，不能借同名结果做受控观察压缩比较。真实服务行为继续由已有 `test_agent_integration.py` / 提案与数据库集成测试验证，本基线不代替它们。
