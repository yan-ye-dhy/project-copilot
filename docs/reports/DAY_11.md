# 【Day 11 开发报告】

日期：2026-10-20。当前：D11实现及离线自动验证完成，**阶段A待用户提交/推送，阶段B待ChatGPT学习及主动手动验证**。无新增依赖、模型费用或Qdrant操作，未开启D12。

## 1. 今日开发目标

从真实V0 dev Bad Case提出一个切分优化假设，准备可复现的最小候选及原文边界测试。只改chunk_size750→500，overlap150/分隔符、业务默认与冻结模型/Prompt合同不变；本日不采集候选排名或回答。

## 2. 新增和修改的文件

**新增：**

- `evals/configs/chunk_size500.json`：唯一变量、独立候选名、V0归档/配置身份与4道dev关注题。
- `evals/chunking.py`：只读预检入口、单因素/dev/归档门禁、原文位置与边界输出。
- `tests/test_chunking_inspection.py`：离线/复现/单因素及输入身份测试。
- `tests/test_chunking_boundaries.py`：中文、Markdown、实际重叠、真实Bad Case及长gold测试。
- `evals/results/chunk-size500-preflight/inspection.json`：实际自动生成的边界快照，含原文/位置/片段ID，不是检索排名。
- `evals/reports/CHUNK_SIZE500_PREFLIGHT.md`：假设、前后对照、代价、限制与D12前置条件。
- 本报告。

**修改：** 根README、evals/README、开发计划第10节、开发日志、学习笔记、面试笔记，共6份关联文档。业务src、D8冻结数据、D10配置/机器结果/人工review、依赖与锁文件不变。

## 3. 核心实现内容

1. `inspect_chunking()`绑定V0归档及实际消费的三个输入，复用D10完整配置/D8冻结校验，拒绝同时变overlap/分隔符及保留集/非真实dev Bad Case。
2. 复用D3 `SplitConfig/split_document`与D9 `build_chunk_catalog`，同文档版本/页/原文位置检查gold交集及单块完整包含；不把部分交集当完整证据。
3. 从实际V0评分保留evidence_completion_ranks，候选没有排名，retrieval_evaluation/answer_evaluation为null，模型/向量库调用计数为0。
4. 保留中文截句、Markdown标题正文可分离、段落重叠不足150及520字gold需多块连续覆盖的负面样例，不只验证有利结果。

## 4. 关键设计决策

V0的003/012/039在rank4才完整覆盖，034跨文档证据在rank4补齐；假设减小size可减少片段的混合主题，但不认定它是唯一原因。500字符候选16→27片段，累计片段字符10330→10389，四题六段gold仍各自单块包含；这些都是边界数据，不能宣称Recall改善或修复003非法JSON。

D3已具备参数化能力，因此复用现有业务代码，仅新增评测侧候选/检查器，不改默认或放宽D10基线校验。原项目递归组件的750/200及分隔符用法作为只读参考；候选选择、归档绑定、边界检查/测试为自主实现，无标题解析器或额外检索算法。更短块可能碎片化并增加固定k覆盖难度，点数增幅不能代替Token/费用；详见 [候选报告](../../evals/reports/CHUNK_SIZE500_PREFLIGHT.md)。

## 5. 自动测试结果

执行者：Codex；工作目录新项目，现有Python3.13环境；均离线，不代表用户手动验证。

| 实际命令 | 实际结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_chunking_inspection.py tests/test_chunking_boundaries.py` | **25 passed，3.13秒**；含禁用网络的实际预检 |
| `.venv/bin/python -m pytest -q tests/test_chunking_inspection.py tests/test_chunking_boundaries.py tests/test_documents.py tests/test_eval_metrics.py tests/test_eval_run.py tests/test_baseline_config.py tests/test_baseline.py tests/test_eval_cache.py tests/test_eval_freeze.py tests/test_eval_dataset.py` | **294 passed，8.19秒**；D11新增25项＋相关旧用例269项，无失败 |
| `.venv/bin/python -m evals.chunking --dataset evals/datasets/copilot_v0 --baseline-dir evals/results/dense-v0-20261019-01 --candidate evals/configs/chunk_size500.json`，stdout保存至新增inspection.json | 退出0，5文档/原文9331字符；16→27片段，六段gold各自完整包含；model_calls=0、qdrant_requests=0 |

003/012/039承载块从682/698/678→385/386/431字符，034从609/643→389/499，跨文档问题仍存在。1条候选文本与V0相同、26条新文本共10129字符；未实际检查向量缓存或生成候选向量，模型Token/费用、Recall/MRR、候选延迟与回答效果均未采集。独立只读代码复核未发现问题，25项相关测试另通过；未重跑真实V0采集、用户手动验证或保留集评测。

收尾检查通过13个改动文件（6修改/7新增）的空白、Python AST与JSON，8份Markdown围栏/48张表格/172处本地链接及12处锚点，固定8项报告完整。检查快照再次计算逐值一致；V0公开12份机器产物与16份运行源码SHA匹配，D8冻结10文件与HEAD原字节一致且verify通过。计划第10节外无变化，无业务/依赖diff，git diff --check无输出、暂存区为空；常见凭据格式检查通过，.env/data/向量缓存仍被忽略，未读取密钥。

## 6. GitHub 提交信息

- 分支：main，跟踪origin/main；远端`https://github.com/yan-ye-dhy/project-copilot.git`。
- 开始时工作区干净，本地/跟踪分支一致于D10收尾 [1b69fbefd83c0998dfaf0976b91f44a7f484d274](https://github.com/yan-ye-dhy/project-copilot/commit/1b69fbefd83c0998dfaf0976b91f44a7f484d274)，用户此前已推送。
- D11：**待用户亲自add/commit/push，暂无已同步的GitHub提交链接**。Codex未进行Git写操作，不预填提交ID或推送成功。

## 7. 后续学习提示

交给ChatGPT结合本日报、[学习提纲](../STUDY_NOTES.md#d11一个切分假设的边界与代价)及实际commit带读：

1. 从真实Bad Case提出假设，单因素配置与运行合同为何分开。
2. main→inspect_chunking→冻结/归档门禁→loader→两次catalog→原文边界→JSON的完整调用链。
3. 递归字符切分、中文回退、Markdown结构与overlap预算的实际行为。
4. document版本/gold位置稳定、切分指纹/chunk_id变化，完整原文证据与检索命中的区别。
5. size/重复/point/Token/延迟的取舍，怎样设计公平的D12真实对照及报告恶化。

## 8. 下一步计划

先由用户提交并推送D11，随后通过ChatGPT学习；待用户主动要求开始D11手动验证，再单步核对边界快照与一个误用配置的拒绝结果。本人学习/手测尚未确认，不代为登记完成。

D11阶段A/B完成且用户明确开启D12后，按费用约定计划新文本Embedding、复用30个原问题向量，独立候选索引/新run目录比较真实排名、Recall/MRR与耗时，核验原文gold映射，保留失败/恶化；不覆盖V0、不调Prompt、不用保留集选参数。
