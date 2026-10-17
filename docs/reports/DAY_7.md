# 【Day 7 开发报告】

开始日期：2026-10-16；2026-10-17继续完成开发交接。资料/工具准备和离线验证已完成，待用户提交、推送；人工gold为0/20，D7尚未整体验收。D1–D6阶段A/B及收尾3f31599已同步，本轮只做D7。

## 1. 今日开发目标

建立第一批中文研发资料、问题与独立证据标签的可追溯草稿，提供离线一致性检查，为用户学习后人工完成约15–20题gold准备材料。当前18题可回答、2题无答案候选，覆盖八类题型；自动准备不等于人工标注已完成。

## 2. 新增和修改的文件

**新增：**

- `evals/datasets/copilot_v0/corpus/`下5份Markdown：启动配置、上传排查、Dense身份/引用、Compose环境、Qdrant向量契约。
- `sources.jsonl`、`questions.jsonl`、`labels.draft.jsonl`（位于上述数据集目录）：来源/版本、问题/问题族及原文证据/人工状态。
- 同目录`README.md`、`REVIEW_QUEUE.md`：字段口径、使用边界和便于逐题复核的视图。
- `evals/validate_dataset.py`：纯离线结构、文档身份/版本、原文切片与人工复核门禁。
- `tests/test_eval_dataset.py`：真实D3原文身份、中文字符位置、无效引用/路径/重复证据、人工状态与CLI检查。
- 本报告。

**修改：** README、DEVELOPMENT_PLAN第10节进度、DEVELOPMENT_LOG、STUDY_NOTES、INTERVIEW_NOTES；业务源码、依赖、锁文件及35日任务表保持原样。

## 3. 核心实现内容

- 3份项目真实记录整理、2份官方资料自编笔记，保留固定公开提交/官方URL、取得日期和版本依据；不是企业事故或真实用户问题集。
- 来源复用D3 document_id和文件SHA256；证据记录原文、page=null及Unicode字符[start,end)，不存chunk_id，不随750/150切分绑定。
- 20题含固定追问历史和独立问题；近似题/追问归问题族。当前尚未开发/保留划分、冻结或运行检索指标。
- 所有标签pending，reviewed_by/reviewed_at均null；无答案候选明确空证据和缺失原因。工具不更改复核状态，严格模式要求实际人工确认。
- 检查器拒绝错误版本/位置、重复文档或证据位置、缺失来源和题型/标签矛盾；只能验证结构一致性，不能证明证据语义充分。

## 4. 关键设计决策

JSONL按题组织，原文与问题/标签分开，复核视图仅用于阅读，不进入知识库。使用标准库和现有D3 loader，不新增依赖，不创建模型或数据库客户端。D8再划分/冻结，D9再实现映射与Recall/MRR，D10再运行收费基线。

人工复核保留为必要环节：AI整理、字符串匹配及测试中的合成确认态，均不计入用户gold。题目由语料反向拟定且主题较窄，材料明确标记小型自建数据集的局限。

## 5. 自动测试结果

执行者：Codex；工作目录为新项目；全部离线，无项目模型API、HTTP服务或索引写入。

| 实际命令/检查 | 结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_eval_dataset.py`，检查器与测试初稿 | 首次25通过；补来源不可为空后26通过。测试确认态只在临时夹具中出现 |
| `.venv/bin/python -m pytest -q tests/test_eval_dataset.py tests/test_documents.py`，补三项一致性守卫及回归后 | **58 passed，0.60秒**；D7新增31项与既有D3加载/切分相关27项，无失败 |
| `.venv/bin/python evals/validate_dataset.py --dataset evals/datasets/copilot_v0` | 退出0，5来源/20题、18可回答/2无答案候选、pending20/confirmed0、ready_for_baseline=false；2026-10-17中断恢复后核对相同 |
| 同一数据集添加`--require-reviewed` | 预期退出1，明确提示标签尚未全部人工确认；这是门禁成功拒绝，不是把草稿登记为已通过人工验收 |

新增测试覆盖真实字节版本、Unicode字符定位、路径及符号链接、来源去重、证据单元去重、无答案/追问与人工状态、CLI输出；当前业务入口未改，原408项全量应用测试本轮未重跑。没有语义效果指标、开发/保留集、真实检索或人工标签验收结果。

收尾通过2份Python AST、45行JSONL、24份Markdown围栏、54张表格、138处本地链接及37处锚点、固定8项报告和18个提交候选的凭据/行尾检查；git diff --check无输出，暂存区为空。计划仅更新第10节进度，原35日安排未变。只读语义复核属于Codex辅助检查，人工确认仍为0。

## 6. GitHub 提交信息

- 分支：`main`，跟踪`origin/main`；远端为`https://github.com/yan-ye-dhy/project-copilot.git`。
- 开发基点：[3f3159965b94655095acca599333e318dc5287b7](https://github.com/yan-ye-dhy/project-copilot/commit/3f3159965b94655095acca599333e318dc5287b7)，D6收尾已同步，开始D7时工作区干净。
- D7 commit：**待用户提交**；推送：**待用户执行**；暂无已同步D7提交链接。Codex未暂存、提交或推送。

## 7. 后续学习提示

交ChatGPT结合 [D7学习笔记](../STUDY_NOTES.md#d7从来源原文到独立证据标签)与本轮commit带读：

1. corpus、问题、标签与原始来源的边界，为什么不能把答案文件一起索引。
2. SHA256/UUID、Unicode字符位置与chunk身份的区别。
3. 多证据单元、无答案判断和人工事实支持；自动检查能保证什么。
4. 问题族、固定历史/独立问题，以及D8如何防止近重复泄漏。
5. CLI正常检查与require-reviewed门禁、临时合成测试与真实人工标注的区别。

## 8. 下一步计划

先由用户亲自提交、推送资料和工具；随后通过ChatGPT学习。用户主动要求开始D7手动验证后，Codex再分步指导来源/证据核对与逐题人工复核，达到原定约15–20题质量要求后记录实际gold进度及理解确认。

当前人工确认0题，D7未整体验收；不自动进入D8，不提前冻结或跑基线。未完成的人工工作如实记待补，不用草稿数量替代验收。
