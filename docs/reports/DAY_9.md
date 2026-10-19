# 【Day 9 开发报告】

开发日期：2026-10-18；学习与验证补记：2026-10-19。D9阶段A/B均完成：开发提交已由用户推送，用户完成ChatGPT学习确认、亲自重放和013核算，剩余检查按明确委托由Codex补验。本次收尾文档待用户亲自提交/推送；没有运行真实Dense基线，不提前进入D10。

## 1. 今日开发目标

对可追溯的原始排名计算文档/证据Recall、MRR及全部gold覆盖，保存逐题排名/分数/原文/耗时/错误和汇总。用固定开发题验证数学、映射及保存流程；不改D8冻结数据、不运行模型。

## 2. 新增和修改的文件

**新增：**

- `evals/metrics.py`：唯一原文位置目录、完整gold覆盖、文档/证据指标。
- `evals/run.py`：冻结/配置与快照门禁、离线重放、逐题/汇总/收据输出。
- `evals/configs/d9_replay.json`、`evals/fixtures/d9_rankings.jsonl`：明确synthetic_fixture的4题配置与手工排名，含重复/无答案/模拟失败。
- `evals/README.md`：执行链、字段、指标、样例手算及局限。
- `tests/test_eval_metrics.py`、`tests/test_eval_run.py`：指标与运行器离线回归。
- 本报告。

**修改：** 根README、DEVELOPMENT_PLAN第10节进度、DEVELOPMENT_LOG、STUDY_NOTES、INTERVIEW_NOTES。

业务源码、D3切分、D8语料/问题/标签/划分及冻结清单、依赖和锁文件均未改，无新增依赖。

## 3. 核心实现内容

- canonical目录复用D3切分，在同文档页唯一定位完整chunk；排名text与全部metadata必须一致，歧义或身份篡改明确失败。
- gold须被单chunk或同文档版本/同页无缺口区间并集完整逐字覆盖，不能只凭局部交集计分；记录首次完整覆盖的原前缀排名。
- 文档按相关文档去重，证据按独立gold unit去重；重复排名保留，分别计算Recall/MRR及all_evidence_covered。失败可答题计0不删分母，无答案各指标null并单列。
- run_evaluation先核验冻结及所有输入，仅接受开发集；明确子集或null全30，缺题/重复/额外题、错query/片段及覆盖输出均拒绝。追问使用冻结standalone_question。
- 新目录保存config.json/retrieval.jsonl/summary.json，包括run_id、代码/dirty、输入SHA256、数据/切分指纹及依赖平台；未采集阶段耗时null，本地计算经过时间另记，错误保留。

## 4. 关键设计决策

参考原tests/fakes/rag.py的固定返回与隔离边界，指标、映射及运行器自行实现，复用现有D3/Pydantic和标准库。原文唯一定位与完整quote覆盖是保守规则，不是语义judge或答案事实正确性判定；不为评测器改写gold。

默认只重放排名，synthetic_fixture数字仅验证工具。recorded_retrieval也只是离线输入声明，D10仍需实际模型/索引清单、向量缓存、检索配置及Prompt/Schema等真实运行记录，不能声称已建完整基线。保留集效果等D17，不因脚本可以算指标就提前使用。

## 5. 自动测试结果

以下为2026-10-18开发期自动验证，执行者Codex，工作目录新项目；全部离线，无模型、Qdrant/HTTP服务、索引写入或费用。2026-10-19用户操作及明确委托的补验另列，不混记执行者。

| 实际命令/检查 | 结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_eval_metrics.py` | 初稿36通过，补边界后 **38 passed，0.10秒** |
| `.venv/bin/python -m pytest -q tests/test_eval_run.py` | **24 passed，0.77秒** |
| `.venv/bin/python -m pytest -q tests/test_eval_metrics.py tests/test_eval_run.py tests/test_eval_dataset.py tests/test_eval_freeze.py tests/test_documents.py` | **155 passed，1.70秒**；D9 62项与D7/D8/D3相关93项，无失败 |
| `.venv/bin/python -m evals.run --dataset evals/datasets/copilot_v0 --config evals/configs/d9_replay.json --rankings evals/fixtures/d9_rankings.jsonl --output-dir data/evals/d9-auto-smoke-final` | 退出0；3份结果实际保存，4固定开发题、3可答/1无答案/1模拟失败，明确offline_replay |
| 固定输入手算与输出比对 | @3文档Recall2/3、MRR1/2；证据Recall2/3、MRR1/3；完整覆盖2/3；失败留分母，无答案排除 |
| 冻结/结果文件校验 | 9份冻结输入及冻结清单共10文件字节未改；4条结果、输入指纹及三份输出结构正确 |

覆盖多gold、重复原排名、跨chunk连续覆盖/缺口、错页/版本/原位置、重复文字歧义、数据与快照篡改、缺题/保留题、无答案/失败分母、时间null与nearest-rank p95、输出防覆盖及离线/.env隔离。样例阶段耗时全部未采集，median/p95=null；refusal_accuracy=null，不得当作真实检索性能或正确拒答。初始smoke旧时间标签修正后另用最终新目录验证，没有覆盖历史输出；未重跑旧应用全量测试。

收尾通过13个候选文件、4份Python AST、27份Markdown围栏、65张表格、193处本地链接及60处锚点、配置/4行排名及固定8项报告；计划仅第10节进度变化，35日安排不变。独立只读实现/事实复核通过，git diff --check无输出，暂存区为空，.env及运行结果被忽略。

2026-10-19学习后验证：

| 执行者 | 实际操作 | 结果 |
| --- | --- | --- |
| 用户 | `uv run --frozen python -m evals.run --dataset evals/datasets/copilot_v0 --config evals/configs/d9_replay.json --rankings evals/fixtures/d9_rankings.jsonl --output-dir data/manual-d9/replay-01`，随后`echo $?` | status=ok、退出0；三份结果保存，4题/3可答/1无答案/1模拟失败 |
| 用户 | 核算013的文档与证据指标 | @1文档Recall1、证据Recall0；@3证据Recall1、MRR0.5，均正确 |
| Codex，用户要求“你直接验证吧”后 | 原结果/收据与汇总独立复核，连续覆盖内存算例 | 原输入哈希及0aeadef/dirty=false/model_calls=0一致；证据Recall@3=2/3、MRR@3=1/3；合成跨片段gold首次完整rank3 |
| Codex，同次委托 | CLI分别删除失败题023、将split改为holdout、复用原输出目录 | 三项均退出1，前两项不创建结果目录；原三份结果及D8冻结10文件字节未变 |

完整门禁命令、输入副本及结果保存在被忽略的`data/manual-d9/codex-verify-0i0rmfd1/verification.json`，细节见 [开发日志](../DEVELOPMENT_LOG.md#用户git同步学习及验证补记2026-10-19)。后两行属于Codex补验，未记为用户手动操作；本轮没有重跑pytest或进行模型/服务调用。阶段耗时和拒答准确率仍未采集，4题合成结果仅验证工具。

本次六份收尾文档的Markdown、表格、内部链接/锚点与固定8项报告检查通过，git diff --check无输出；计划仅第10节进度变化，34主任务日＋1弹性日不变。暂存区为空，运行数据未纳入提交。

## 6. GitHub 提交信息

- 分支：`main`，跟踪`origin/main`；仓库为`https://github.com/yan-ye-dhy/project-copilot.git`。
- 开发基点：[056eff8fbab6506915875144827fd8dd2ffea539](https://github.com/yan-ye-dhy/project-copilot/commit/056eff8fbab6506915875144827fd8dd2ffea539)，D8收尾已由用户推送，开启D9时工作区干净。
- D9开发commit：[0aeadef3bd84e66649d72a4358c10cbe62f9f7fe](https://github.com/yan-ye-dhy/project-copilot/commit/0aeadef3bd84e66649d72a4358c10cbe62f9f7fe)，`feat(evals): add offline retrieval metrics and replay runner`；用户已推送，输出056eff8..0aeadef main→main，Codex只读核实HEAD/origin/main一致。
- 本次六份学习与验证收尾文档：**待用户提交/推送**，不预填新commit。Codex未暂存、提交或推送。

## 7. 后续学习提示

用户已确认通过ChatGPT学完、理解主流程；以下保留为复习入口，结合 [D9学习笔记](../STUDY_NOTES.md#d9从原始排名到文档与证据指标)、[评测说明](../../evals/README.md)及开发commit：

1. 文档级与证据级Recall/MRR的分母与原始排名，多gold及跨段的全部覆盖。
2. 原文位置映射、连续quote拼接及歧义拒绝，为什么字符相交不够。
3. 失败/无答案的统计边界，为什么不能只保留成功题。
4. 命令→冻结→目录/快照→逐题→宏平均→三份文件的完整调用链。
5. 固定重放、真实Dense基线、外部记录耗时与本地计算时间各能证明什么。

## 8. 下一步计划

用户已回复“已完成，理解主流程”；结合已推送开发提交、本人重放/013核算及明确委托的剩余补验，D9阶段A/B均完成。下一步仅指导用户亲自提交本次六份收尾文档，不再安排逐项手算，不自动进入D10。

D10在用户明确开启后，再按费用规则准备真实索引/查询向量、缓存、开发集V0与分层回答核验，冻结Prompt及实验配置；本次没有执行收费调用，也不把D9合成数字记作V0或优化结果。
