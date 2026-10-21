# 【Day 11 开发报告】

开发日期：2026-10-20；学习与手动验证补记：2026-10-21。**D11阶段A/B均完成**，开发提交d53fe15已由用户推送；本人四项手动验证及ChatGPT学习理解已确认，本次收尾文档待用户提交。无新增依赖、模型费用或Qdrant操作，未开启D12。

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

2026-10-21收尾只补根README、开发计划第10节、开发日志、学习/面试笔记及本报告，共6份Markdown；evals说明、源码、配置与机器快照不改。

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

2026-10-21用户主动开启D11手动验证，以下由用户本人操作；Codex只读核对反馈/本地文件，未重跑检查器或将开发期自动测试登记为用户操作。

| 执行者与实际操作 | 实际结果 |
| --- | --- |
| 用户：`uv run --frozen python -m evals.chunking --dataset evals/datasets/copilot_v0 --baseline-dir evals/results/dense-v0-20261019-01 --candidate evals/configs/chunk_size500.json > data/manual-d11/inspection.json`及`echo $?`；Codex读取输出文件 | 退出0；与开发期检查快照逐值一致，5文档/9331原文字符、16→27块、model_calls/qdrant_requests均0，两项效果评估null |
| 用户在inspection.json观察003题的gold和两种承载块，反馈“已看到”；Codex只读核对原文切片 | gold区间`[943,1056)`不变，baseline`[499,1181)`682字符、candidate`[796,1181)`385字符，完整quote均逐字相符；旧rank4仍只属于V0 |
| 用户运行带练内联Python：三段各300字、双换行分隔，保存paragraphs.txt，以500/150切分并打印原文位置 | 实际300/302/302字符，区间`[0,300)`、`[300,602)`、`[602,904)`，相邻重叠均0；Codex只读确认样本904字符，与用户命令一致 |
| 用户在data/manual-d11/two-factors.json副本只把overlap改100，再用相同命令将candidate路径换为该副本并查看退出码 | status=error，提示“D11候选只允许改变chunk_size，不得同时改变overlap或分隔符”，退出1；Codex核对副本仅该字段不同，正式候选不变 |

用户另明确确认“已学习，能够解释”，涵盖冻结校验→两种切分→原文位置→边界JSON、overlap预算及边界包含gold不等于检索效果改善。结合已同步开发和本轮手动验证，阶段A/B均完成。测试样本/副本留在被忽略的data目录；本轮只更新收尾文档，没有重新采集候选向量/排名/回答、运行pytest或启动服务，模型调用为0。

本次六份收尾文档检查通过：Markdown围栏/空白、42张表格、158处本地链接及12处锚点，固定8项报告和计划第10节外不变。V0公开12产物/16运行源码SHA、D8冻结10文件、D11正式配置/检查器/边界快照均保持一致；三个手动文件仍被忽略。独立只读事实复核通过，git diff --check无输出，暂存区为空，无未忽略新增文件。

## 6. GitHub 提交信息

- 分支：main，跟踪origin/main；远端`https://github.com/yan-ye-dhy/project-copilot.git`。
- 开始时工作区干净，本地/跟踪分支一致于D10收尾 [1b69fbefd83c0998dfaf0976b91f44a7f484d274](https://github.com/yan-ye-dhy/project-copilot/commit/1b69fbefd83c0998dfaf0976b91f44a7f484d274)，用户此前已推送。
- D11开发提交：[d53fe15fc0101e893076fe8dc8595d7a091eb605](https://github.com/yan-ye-dhy/project-copilot/commit/d53fe15fc0101e893076fe8dc8595d7a091eb605)，`feat(evals): add single-factor chunking candidate and boundary checks`；用户已推送，输出1b69fbe..d53fe15 main→main，2026-10-21Codex只读核实main/origin/main一致、开始手动验证时工作区干净，阶段A完成。
- 本次学习/手动验证收尾：**待用户亲自提交/推送**，不预填新commit；Codex未进行Git写操作。

## 7. 后续学习提示

用户已确认通过ChatGPT学完并能解释关键区别；以下保留为复习重点，结合本日报、[学习提纲](../STUDY_NOTES.md#d11一个切分假设的边界与代价)及实际commit：

1. 从真实Bad Case提出假设，单因素配置与运行合同为何分开。
2. main→inspect_chunking→冻结/归档门禁→loader→两次catalog→原文边界→JSON的完整调用链。
3. 递归字符切分、中文回退、Markdown结构与overlap预算的实际行为。
4. document版本/gold位置稳定、切分指纹/chunk_id变化，完整原文证据与检索命中的区别。
5. size/重复/point/Token/延迟的取舍，怎样设计公平的D12真实对照及报告恶化。

## 8. 下一步计划

当前仅指导用户亲自提交/推送六份收尾文档；D11阶段A/B均完成，等待用户明确要求才开启D12。

D11阶段A/B完成且用户明确开启D12后，按费用约定计划新文本Embedding、复用30个原问题向量，独立候选索引/新run目录比较真实排名、Recall/MRR与耗时，核验原文gold映射，保留失败/恶化；不覆盖V0、不调Prompt、不用保留集选参数。
