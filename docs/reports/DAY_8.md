# 【Day 8 开发报告】

日期：2026-10-17。D8扩充、划分和冻结工具开发及离线验证完成；阶段A待用户提交/推送，阶段B待ChatGPT学习、新20题人工复核和正式冻结。不提前进入D9。

## 1. 今日开发目标

将已有20题扩充为40题，保留原人工确认记录；按完整问题族准备30开发/10保留划分，提供离线预检、正式冻结与复算工具。实际40题中35可回答、5无答案，八类覆盖；原20题confirmed、新20题pending，正式冻结尚未生成。

## 2. 新增和修改的文件

**新增：**

- `evals/freeze_dataset.py`：划分完整性/同族约束、人工门禁、字节指纹与冻结核验CLI。
- `evals/datasets/copilot_v0/split_manifest.json`：固定问题ID划分，版本copilot-v0-family-v1。
- `tests/test_eval_freeze.py`：临时合成数据的分组、门禁、冻结和变更失效回归。
- 本报告。

**修改：**

- `evals/datasets/copilot_v0/questions.jsonl`、`labels.draft.jsonl`：追加pc-d8-021至040，原20行字节及复核信息保留。
- 同目录`REVIEW_QUEUE.md`、`README.md`：新增题证据视图、人工状态、问题族划分、冻结流程及使用边界。
- 根README、DEVELOPMENT_PLAN第10节、DEVELOPMENT_LOG、STUDY_NOTES、INTERVIEW_NOTES：进度、运行入口、学习和交接。

业务源码、配置、依赖/uv.lock、来源清单及5份corpus未变，没有新增依赖。

## 3. 核心实现内容

- 40题八类数量：知识8、术语5、精确名5、同义5、中文长文4、跨段4、无答案5、追问4。新题覆盖健康边界、片段身份、维度预期、引用组装、Compose与存储边界等已有事实；新增无答案明确缺失的客户端版本、MCP部署及实际HNSW参数。
- 23个family整体分组；保留侧5族10题，开发侧18族30题，两侧八类均覆盖，可答/无答案分别26/4及9/1。较大身份族6题同侧，追问不拆；没有使用任何检索效果选择划分。
- validate_split复用D7检查器，再核对总量30–50、约3:1、ID完整无重复/未知、同族同侧、八类及两侧可答/无答案。
- freeze_dataset只在全量confirmed后独占创建frozen_manifest.json；9份输入原始字节SHA256、split_id与文本定位规则形成总指纹，生成时间不参与。verify_freeze重新检查实际输入，标签复核字段变更也会使原冻结失效，已有冻结拒绝覆盖。
- CLI从项目根按`python -m evals.freeze_dataset`运行：默认只预检，`--freeze`创建冻结，`--verify`复算；不修改标签或连接外部服务。

## 4. 关键设计决策

只扩问题与标签，沿用原5份语料和D3文档身份/位置，避免改变D7已确认依据。gold仍绑定原文证据，不保存当前chunk_id。参考原search路由的text/page/source边界，划分及冻结逻辑自行实现，复用现有检查器与标准库。

人工确认与结构校验分开：D7确认不能扩用到新题，默认预检允许pending，正式冻结必须等用户实际确认。学习后允许按明确题号范围批量确认，无需逐题重复书面依据。冻结指纹覆盖测量输入，README/队列是展示视图，不进入数据身份；D10再冻结模型/Prompt及实验配置。

保留题允许正常人工标注，后续效果不能用于挑参数。D10只跑开发集，D17才比较已冻结V0与最终方案。同5份资料反向拟题、10题保留集仍有明显局限，不宣称通用能力或统计显著。

## 5. 自动测试结果

执行者：Codex；全部离线，没有项目模型API调用、Docker/HTTP服务、索引写入或费用，不代替用户手动验证。

| 实际命令/检查 | 实际结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_eval_freeze.py` | 初版29通过；补冻结清单/文本规则边界后 **35 passed，0.58秒** |
| `.venv/bin/python -m pytest -q tests/test_eval_dataset.py tests/test_eval_freeze.py tests/test_documents.py` | **93 passed，1.20秒**：D7 31项、D8 35项、D3相关27项，无失败 |
| `.venv/bin/python evals/validate_dataset.py --dataset evals/datasets/copilot_v0` | 退出0，5来源/40题、35可答/5无答案、confirmed20/pending20 |
| `.venv/bin/python -m evals.freeze_dataset --dataset evals/datasets/copilot_v0` | 退出0，30开发/10保留、23族，ready_for_freeze=false |
| 数据检查添加`--require-reviewed` | 预期退出1，尚未全量人工确认 |
| 划分工具添加`--freeze` | 预期退出1，不能正式冻结；执行后frozen_manifest.json仍不存在 |
| 逐字节比较原20题/标签及来源/5份corpus | 原20行保持，来源与原文字节未变 |

覆盖同族泄漏、划分重叠/遗漏/未知、数量/类别、人工门禁、完整冻结/复算、防覆盖、合法数据变更失效、展示文件、JSON/路径和LF策略。测试中的确认/成功冻结仅属于临时合成夹具，不代表真实40题已确认或冻结。旧业务全量测试本次未重跑，D9检索映射/指标及D10收费基线未执行。

收尾检查通过：13个提交候选、2份Python AST、85行JSONL、25份Markdown围栏、58张表格、172处本地链接及59处锚点、固定8项报告。计划仅第10节进度变化，35日安排保持原样；实现/分族只读审查通过，git diff --check无输出，暂存区为空，.env及运行数据仍被忽略。

## 6. GitHub 提交信息

- 分支：`main`，跟踪`origin/main`；仓库为`https://github.com/yan-ye-dhy/project-copilot.git`。
- 开发基点：[92513a74bddc5bc07fb78ade3b8e26863a23ce3f](https://github.com/yan-ye-dhy/project-copilot/commit/92513a74bddc5bc07fb78ade3b8e26863a23ce3f)，D7收尾已由用户推送，开启D8时工作区干净。
- D8 commit：**待用户提交**；推送：**待用户执行**；暂无已同步D8提交链接。Codex未暂存、提交或推送。

## 7. 后续学习提示

交ChatGPT结合 [D8学习笔记](../STUDY_NOTES.md#d8按问题族划分与可复算冻结)、本轮代码及后续commit带读：

1. family与逐题随机划分的差别，如何防止同义题/追问链泄漏。
2. Python `-m`模块入口到main、validate_split、D7检查器的实际调用链。
3. 原始字节SHA256、规范化清单JSON、总指纹与生成时间的关系。
4. pending预检、人工确认、独占创建冻结、verify失效检测的边界。
5. 开发/保留使用时机，以及小型自建评测集能支持和不能支持的结论。

## 8. 下一步计划

先由用户亲自提交推送D8开发成果，通过ChatGPT学习。用户主动要求D8手动验证后，再核对新增20题、检查一个跨段证据及同族划分，按明确反馈登记确认；全量通过后由用户执行正式冻结和复算。原20题不要求重做，新题可按实际核对范围批量确认。

当前D8尚未整体验收，正式冻结未生成，不提前进入D9。下一Day在用户明确开启后实现最小检索评测器及证据映射，不自动运行模型或收费基线。
