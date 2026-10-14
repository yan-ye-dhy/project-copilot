# 【Day 5 开发报告】

日期：2026-10-14。当前实现、离线及最小真实自动验收完成，用户Git交接及阶段B待完成。D1–D4阶段A/B已完成；本轮只实施用户明确开启的D5。

## 1. 今日开发目标

让D4检索证据进入DeepSeek问答，返回受本地契约约束的答案与可追溯原文引用。实现固定Zero-shot消息、带编号/来源的User上下文、JSON/Pydantic校验和薄HTTP入口。不增加依赖或提前实现D6上传。

## 2. 新增和修改的文件

**新增：**

- `src/project_copilot/schemas/rag.py`：问题、模型回答、后端引用及运行元信息。
- `src/project_copilot/rag/answering.py`：消息模板、上下文预算/编号、回答校验与D4资源编排。
- `src/project_copilot/api/__init__.py`、`api/rag.py`：`POST /rag/ask`与HTTP错误映射。
- `scripts/rag_smoke.py`：显式live的一条固定问题，不入库或自动重试。
- `tests/test_chat_models.py`、`test_answering.py`、`test_rag_api.py`、`test_rag_pipeline.py`、`test_rag_smoke.py`：SDK协议、回答/接口、跨模块链路及CLI离线验证。
- 本报告。

**修改：**

- `src/project_copilot/config.py`、`infrastructure/models.py`、`.env.example`：独立DeepSeek配置/适配；保留原Embedding行为。
- `src/project_copilot/main.py`：注册问答路由，启动时仍只加载配置。
- `README.md`、`docs/DEVELOPMENT_PLAN.md`、`DEVELOPMENT_LOG.md`、`STUDY_NOTES.md`、`INTERVIEW_NOTES.md`：运行参考、实际状态、教学交接与回答思路；35日任务不变。

## 3. 核心实现内容

- `create_chat_client/DeepSeekChat.generate_json`：非推理JSON mode、timeout30、temperature0、max_tokens1024、max_retries0；检查stop/content/工具字段/实际模型及usage。
- `RagAnswerer`：校验问题 → D4检索 → 完整片段选择/去重/编号 → System/User → JSON解析及ModelAnswer → 本次引用映射。
- `ModelAnswer`只允许status、answer、citation_numbers；answered必须有引用，insufficient_evidence不得引用。拒绝重复字段、非法类型/编号和Schema；不重试修复。
- `Citation`的text和metadata来自实际发送的片段。响应另有Prompt/Schema指纹、Dense版本、实际证据数量、请求/实际模型和可用usage；空证据不生成，不采集的数据为null。
- `/rag/ask`同步执行在框架线程池中，验证请求体后才构造客户端；HTTP422/503/502分别对应输入、配置和上游问题，所有with资源正常或异常时关闭。

## 4. 关键设计决策

参考旧graph/generation/utility的非推理、消息与编号思路；System保持固定规则，问题/文档作为User数据，不搬原context提升为System的实现。新的回答契约、引用映射和编排自主设计。

当前[官方模型](https://api-docs.deepseek.com/quick_start/pricing/)使用deepseek-flash；旧别名已[退役](https://api-docs.deepseek.com/news/news260424/)。仅复用已安装SDK，不引入新的模型框架。JSON格式骨架保持Zero-shot；Few-shot效果对照留D19。

原文预算6000字符，按检索顺序保留完整片段、去重、跳过超预算块，重新从1编号，不改D3 chunk身份或D4索引。有效引用身份不等于事实支持；完整可靠性与基线冻结仍按D19/D10完成。

## 5. 自动测试结果

执行者均为Codex，工作目录为新项目。离线与真实模型验收分别记录：

- `timeout 45s .venv/bin/python -m pytest -q`：**358 passed, 1 skipped，4.83秒**。外部Qdrant测试默认跳过；D1–D4回归、消息/Schema/引用/HTTP/CLI、真实SDK跨模块编排及资源关闭通过。
- 默认沙箱中的同步HTTP测试超时；允许本机线程唤醒权限后通过。初次全量存在1个路由内部结构断言失败，已改为实际HTTP/OpenAPI契约验证并全量重跑；不是未处理业务失败。
- 只读检查：用户保存的DeepSeek配置能加载，不输出key；D4成功清单仍为1份PDF/2块。准备时Qdrant不可达、docker命令I/O错误；恢复后Codex启动现有容器并确认readyz，不删除数据。
- **真实HTTP问答通过：** 自动临时Uvicorn及一次`POST /rag/ask`返回200/answered，1.606秒；“第一页描述了本地文档加载功能”引用sample.pdf页0原文，D3元数据匹配。本次仅1次问题Embedding＋1次DeepSeek生成，SDK自动重试关闭，未重新入库；随后精确count仍2，临时Uvicorn正常关闭。
- 实际DeepSeek usage为输入358/输出24/总382 tokens，缓存命中0/未命中358；请求和返回模型均为deepseek-flash。Embedding usage及实际账单未采集，事前预算约¥0.0005＋US$0.0022不冒充实付。不报告Recall/MRR、普遍事实支持率或完整注入防护效果。
- `UV_CACHE_DIR=/tmp/project-copilot-d4-uv-cache UV_PYTHON_DOWNLOADS=never uv sync --locked --offline --check`：锁定环境检查通过，58个包无需改动。13份改动Python AST、15份Markdown及88处本地链接/锚点、固定8项报告、行尾/换行及20个提交候选的真实凭据检查通过；`git diff --check`无输出，`.env`与运行数据被忽略。

详细历史命令、失败原因与状态见 [开发日志](../DEVELOPMENT_LOG.md#d5deepseek结构化问答与引用2026-10-14)。用户D5功能手测尚未开启。

## 6. GitHub 提交信息

- 分支：`main`，跟踪`origin/main`。
- 远端：`https://github.com/yan-ye-dhy/project-copilot.git`。
- 当前基点：D4收尾`061864bea22a54026f001c038cd272e2e8ca6da1`，已同步GitHub。
- **D5 commit ID：待用户提交；推送结果：待用户推送；暂无已同步的D5 GitHub提交链接。** Codex不自动add/commit/push。

## 7. 后续学习提示

交ChatGPT结合真实代码讲解这5点，完整运行链见 [学习笔记](../STUDY_NOTES.md#d5检索证据到结构化回答)：

1. System/User作用、一次变量绑定与检索文档的指令边界。
2. Zero-shot与Few-shot；格式骨架和具体任务示例的区别。
3. D4证据如何经过预算/去重/编号进入User消息，再映射为真实来源。
4. JSON mode、JSON Schema/Pydantic、引用身份与事实支持的不同保证。
5. 同步SDK/线程池/with资源生命周期，Prompt/实际模型/usage的可追溯性。

## 8. 下一步计划

审核diff后指导用户亲自提交、推送；之后交ChatGPT学习。本次两次最小真实自动验收已完成，不自动重复收费。用户主动要求“开始D5的手动测试”才进入单步带练，学习理解和手测不能自动勾选。

D6仍为上传接入与Dense模块复盘；只在D5阶段A/B完成且用户明确要求后启动，不因本次实现或收费授权自动推进。
