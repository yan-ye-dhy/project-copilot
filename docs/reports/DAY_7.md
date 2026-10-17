# 【Day 7 开发报告】

开始日期：2026-10-16；2026-10-17完成开发交接、学习和手动验证。D7阶段A/B均完成：开发提交acd87cd已由用户推送，20题已人工确认，用户严格检查退出0并确认理解。此次复核元数据与文档补记待用户另行提交；不自动进入D8。

## 1. 今日开发目标

建立第一批中文研发资料、问题与独立证据标签，提供离线一致性检查，并在用户学习后完成约15–20题人工gold。实际完成5份来源、20题，含18题可回答、2题无答案，覆盖八类题型；人工确认依据为用户实际核对后的明确反馈。

## 2. 新增和修改的文件

**新增：**

- `evals/datasets/copilot_v0/corpus/`下5份Markdown：启动配置、上传排查、Dense身份/引用、Compose环境、Qdrant向量契约。
- `sources.jsonl`、`questions.jsonl`、`labels.draft.jsonl`（位于上述数据集目录）：来源/版本、问题/问题族及原文证据/人工状态。
- 同目录`README.md`、`REVIEW_QUEUE.md`：字段口径、使用边界和便于逐题复核的视图。
- `evals/validate_dataset.py`：纯离线结构、文档身份/版本、原文切片与人工复核门禁。
- `tests/test_eval_dataset.py`：真实D3原文身份、中文字符位置、无效引用/路径/重复证据、人工状态与CLI检查。
- 本报告。

**修改：** README、DEVELOPMENT_PLAN第10节进度、DEVELOPMENT_LOG、STUDY_NOTES、INTERVIEW_NOTES；手测收尾另更新标签复核元数据、复核队列/数据说明及COLLABORATION_RULES的批量确认约定。业务源码、依赖、锁文件及35日任务表保持原样。

## 3. 核心实现内容

- 3份项目真实记录整理、2份官方资料自编笔记，保留固定公开提交/官方URL、取得日期和版本依据；不是企业事故或真实用户问题集。
- 来源复用D3 document_id和文件SHA256；证据记录原文、page=null及Unicode字符[start,end)，不存chunk_id，不随750/150切分绑定。
- 20题含固定追问历史和独立问题；近似题/追问归问题族。当前尚未开发/保留划分、冻结或运行检索指标。
- 初稿标签均pending，复核者/时间为null；用户实际核对并明确确认001–020全部通过后，Codex按反馈登记20题confirmed。复核者为yanye，统一登记时间2026-10-17T22:14:55+08:00；问题、答案及证据未改。工具不自动更改复核状态，无答案题仍保留空证据和缺失原因。
- 检查器拒绝错误版本/位置、重复文档或证据位置、缺失来源和题型/标签矛盾；只能验证结构一致性，不能证明证据语义充分。

## 4. 关键设计决策

JSONL按题组织，原文与问题/标签分开，复核视图仅用于阅读，不进入知识库。使用标准库和现有D3 loader，不新增依赖，不创建模型或数据库客户端。D8再划分/冻结，D9再实现映射与Recall/MRR，D10再运行收费基线。

人工复核保留为必要环节：AI整理、字符串匹配及测试中的合成确认态，均不计入用户gold。用户实际核对后可按明确题号范围批量确认，只对存疑或需修改项补充说明，无需逐题重复书面依据。题目由语料反向拟定且主题较窄，材料明确标记小型自建数据集的局限。

## 5. 自动测试结果

**开发期自动验证（Codex）：** 工作目录为新项目；全部离线，无项目模型API、HTTP服务或索引写入。下表保留人工复核前的实际结果。

| 实际命令/检查 | 结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_eval_dataset.py`，检查器与测试初稿 | 首次25通过；补来源不可为空后26通过。测试确认态只在临时夹具中出现 |
| `.venv/bin/python -m pytest -q tests/test_eval_dataset.py tests/test_documents.py`，补三项一致性守卫及回归后 | **58 passed，0.60秒**；D7新增31项与既有D3加载/切分相关27项，无失败 |
| `.venv/bin/python evals/validate_dataset.py --dataset evals/datasets/copilot_v0` | 退出0，5来源/20题、18可回答/2无答案候选、pending20/confirmed0、ready_for_baseline=false；2026-10-17中断恢复后核对相同 |
| 同一数据集添加`--require-reviewed` | 预期退出1，明确提示标签尚未全部人工确认；这是门禁成功拒绝，不是把草稿登记为已通过人工验收 |

新增测试覆盖真实字节版本、Unicode字符定位、路径及符号链接、来源去重、证据单元去重、无答案/追问与人工状态、CLI输出；业务入口未改，原408项全量应用测试本轮未重跑。没有语义效果指标、开发/保留集或真实检索结果；人工标签验收见下方手动记录。

开发交接时通过2份Python AST、45行JSONL、24份Markdown围栏、54张表格、138处本地链接及37处锚点、固定8项报告和18个提交候选的凭据/行尾检查；git diff --check无输出，暂存区为空。计划仅更新第10节进度，原35日安排未变。只读语义复核属于Codex辅助检查，当时人工确认仍为0。

**用户手动验证（2026-10-17）：** 用户主动开启，本人运行以下命令并反馈结果；没有模型调用、服务启动或索引写入。

| 用户实际命令/操作 | 实际结果 |
| --- | --- |
| `uv run --frozen python evals/validate_dataset.py --dataset evals/datasets/copilot_v0`及`echo $?` | 退出0，5来源/20题、18可答/2无答案候选，confirmed0/pending20、ready_for_baseline=false |
| 同一命令添加`--require-reviewed`及`echo $?`，复核前 | 退出1，提示标签尚未全部人工确认，门禁按预期拒绝 |
| 实际核对后明确回复“001–020已核对，全部通过” | 人工确认20/20；Codex按批量确认登记三个复核字段并同步队列 |
| 登记后本人再次执行`uv run --frozen python evals/validate_dataset.py --dataset evals/datasets/copilot_v0 --require-reviewed`及`echo $?` | 退出0，status=ok，confirmed20/pending0/needs_changes0、ready_for_baseline=true，来源/题数/八类计数不变 |

Codex另行自动核对标签只改review_status/reviewed_by/reviewed_at，来源、问题和5份corpus字节与开发提交一致；登记后离线严格校验通过。这项完整性检查与用户操作分别记录，不替代人工判断。用户已确认通过ChatGPT学习，能区分结构检查与人工gold、文档版本/原文位置与chunk_id。

手测收尾检查通过：10份修改文件、45行JSONL、24份Markdown围栏、55张表格、138处本地链接及37处锚点，原35日任务表未变；本次纯复核元数据/文档变更未重跑旧业务测试，暂存区为空。

## 6. GitHub 提交信息

- 分支：`main`，跟踪`origin/main`；远端为`https://github.com/yan-ye-dhy/project-copilot.git`。
- 开发基点：[3f3159965b94655095acca599333e318dc5287b7](https://github.com/yan-ye-dhy/project-copilot/commit/3f3159965b94655095acca599333e318dc5287b7)，D6收尾已同步，开始D7时工作区干净。
- D7开发提交：[acd87cd3f0287cdbe7ba7b3131500ab1db904add](https://github.com/yan-ye-dhy/project-copilot/commit/acd87cd3f0287cdbe7ba7b3131500ab1db904add)，`feat(evals): add corpus and evidence annotation drafts`。用户亲自commit并推送main成功（3f31599..acd87cd）；首次将origin误写为oringin失败，修正后成功。Codex只读核实HEAD/origin/main一致、当时工作区干净。
- 本次人工复核元数据、状态及协作规则补记：**待用户提交/推送**。Codex未暂存、提交或推送，不预填收尾commit。

## 7. 后续学习提示

用户已确认完成ChatGPT学习，以下要点结合 [D7学习笔记](../STUDY_NOTES.md#d7从来源原文到独立证据标签)与开发commit保留作复盘：

1. corpus、问题、标签与原始来源的边界，为什么不能把答案文件一起索引。
2. SHA256/UUID、Unicode字符位置与chunk身份的区别。
3. 多证据单元、无答案判断和人工事实支持；自动检查能保证什么。
4. 问题族、固定历史/独立问题，以及D8如何防止近重复泄漏。
5. CLI正常检查与require-reviewed门禁、临时合成测试与真实人工标注的区别。

## 8. 下一步计划

D7开发、用户Git交接、ChatGPT学习及手动验证均完成；当前20题人工gold已满足首批目标。接下来由用户亲自提交、推送本次复核记录。`ready_for_baseline=true`只表示当前目录人工复核门禁满足，不代表评测冻结或检索效果已达标。

用户明确开启D8后，再继续扩充到30–50题、按问题族划分开发/保留集并冻结；D9评测器与D10真实基线各自验收。不自动进入D8，不提前运行收费基线。
