# 【Day 10 开发报告】

开发日期：2026-10-19；学习与手动验证补记：2026-10-20。D10阶段A/B均完成，开发提交12226d7已由用户推送，学习理解、离线手动验证及八题人工复核已确认；本次收尾记录待用户提交。开发期269项相关离线测试通过，实调3次Embedding＋8次回答；003非法JSON仍保留为失败，八题端到端7/8，不进入D11。

## 1. 今日开发目标

复用D9评测器采集全部30道开发题真实Dense排名，缓存片段/问题向量，冻结V0完整Prompt与参数；预选八类各一题回答并保存待人工复核记录。D8正式语料/标签/划分保持原样，保留集等D17。

## 2. 新增和修改的文件

**新增：**

- `evals/cache.py`：供应商/模型/维度/float/文本指纹缓存及内容校验。
- `evals/baseline_config.py`、`evals/configs/dense_v0.json`：完整Prompt/Schema/模型/索引合同、dev全量及固定分层样本。
- `evals/baseline.py`：plan/collect/verify入口、smoke门禁、真实采集和D9评分、原始回答/人工草稿及机器产物清单。
- `tests/test_eval_cache.py`、`tests/test_baseline_config.py`、`tests/test_baseline.py`：离线缓存、合同漂移和采集边界测试。
- `evals/reports/DENSE_V0.md`及`evals/results/dense-v0-20261019-01/`：真实基线、原始排名/回答、待人工复核及公开归档清单。
- 本报告。

**修改：** 根README、evals/README及开发计划第10节、开发日志、学习笔记、面试笔记。业务src、D3切分、D8冻结文件、D9指标/重放接口、依赖和锁文件均未修改，无新增依赖。

2026-10-20收尾仅补八份文档与独立review.jsonl，共九文件；本地被忽略的review同步登记，不修改原机器快照、模型回答或排名。

## 3. 核心实现内容

1. 确认冻结身份及运行时合同后，采用独立pc_eval_dense_v0集合；复用D4入库/成功清单和D9原文映射，不把应用示例PDF混入评测范围。
2. 默认冷缓存按16/2/28文本三批，先验证两题smoke才继续剩余查询。每题立即保存真实排名或失败，失败仍留在26道可答分母内，4道无答案单列。
3. 文档与问题按原文指纹复用向量；损坏缓存拒绝，原子保存，不读取旧项目.env。完整查询向量另存本地归档。
4. 冻结完整模板/变量/上下文规则、Pydantic Schema、生成参数及配置指纹；运行前拒绝隐式漂移。
5. 八类回答复用同次排名前5条，保存消息、原始content、实际模型/usage、校验结果或失败，不重新Embedding。采集时人工草稿pending，2026-10-20按用户明确反馈补录claims及支持/拒答结果，见第5项。

## 4. 关键设计决策

采集与评分分开：D9仍是零模型调用的重放器，D10保存真实采集调用数及来源。缓存按供应商+模型+维度+编码+文本SHA隔离；统计批耗时和缓存读取，不能摊出虚假的逐题API延迟。当前云端别名仍可能更新，保留运行日期与实际返回模型，不能保证云端权重永久固定。

复用D2–D5协议、D9指标和标准库，自主实现实验缓存/合同/编排；不引入新监控系统或更改业务流程。机器产物按字节封存且拒绝覆盖，review.jsonl独立追加；verify不代表人工引用支持或完整D10验收。

## 5. 自动测试结果

以下首张表为2026-10-19开发期离线记录，执行者Codex，工作目录新项目；fake不代表真实供应商或Qdrant效果。真实自动采集和2026-10-20用户操作分别列出，不混记执行者。

| 实际命令/检查 | 实际结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_eval_cache.py` | 59 passed，0.20秒 |
| `.venv/bin/python -m pytest -q tests/test_baseline_config.py` | 39 passed，1.14秒；初次测试类别预期写错，按冻结题型修正断言 |
| `.venv/bin/python -m pytest -q tests/test_eval_cache.py tests/test_baseline_config.py tests/test_baseline.py tests/test_eval_metrics.py tests/test_eval_run.py tests/test_eval_freeze.py tests/test_eval_dataset.py tests/test_documents.py` | **267 passed，5.07秒**；D10新增112项，D9/D8/D7/D3相关155项 |
| `.venv/bin/python -m evals.baseline plan --dataset evals/datasets/copilot_v0 --config evals/configs/dense_v0.json` | 退出0；5文档/16片段/30题，10330片段字符与1663问题字符，默认冷缓存3批，回答最多8次，model_calls=0 |
| 本地白名单配置检查 | 两密钥均已配置，实际运行参数匹配冻结配置；未输出密钥、未调用模型 |

采集测试初轮13通过/1失败，发现plan将冷缓存请求数写死为3；已按各批唯一文本与batch_size动态计算，batch8时7请求，修复后以上267项通过。覆盖fresh三批/warm零Embedding、smoke停费、失败题保留、原始回答/JSON错误及人工pending、输出防覆盖、字节核验与缺--live先拒绝；不重跑无关旧应用全量测试。

初期Docker CLI报I/O错误、沙箱访问socket/HTTP受限；用户恢复WSL后，Codex启动原Qdrant容器。一次环境恢复后的容器退出与启动中连接重置经检查后解决，真实readyz为HTTP200、服务1.19.2，再发模型请求。下方为实际真实验收，不将前述fake冒充效果。

| 实际命令/检查 | 实际结果 |
| --- | --- |
| 同上八文件pytest组合命令，补强verify后最终执行 | **269 passed，5.35秒**；D10新增114项，相关旧用例155项 |
| `.venv/bin/python -m pytest -q tests/test_baseline.py`，补强封存门禁后 | 16 passed，7.70秒；拒绝清单漏项和未完成status |
| `.venv/bin/python -m evals.baseline collect --dataset evals/datasets/copilot_v0 --config evals/configs/dense_v0.json --output-dir data/evals/dense-v0-20261019-01 --cache-dir data/evals/embedding-cache --live --answers` | 退出0；两题smoke后全30题真实排名，3次Embedding＋8次回答，检索错误0、回答结构失败1；不是全部回答正确 |
| `.venv/bin/python -m evals.baseline verify --dataset evals/datasets/copilot_v0 --output-dir data/evals/dense-v0-20261019-01` | 退出0、verified，12份机器文件；human_review=pending、day10_accepted=false |
| Qdrant集合Schema/精确计数 | 独立评测集合dense/2048/Cosine、16点；原应用集合仍2点 |
| 真实问题缓存复用，delegate禁止发请求 | 30题hit30/miss0/api_calls0，向量与已保存原值逐项一致 |

用户在已说明最多3次Embedding/8次回答与费用后要求“继续完成”，据此执行上述一次范围。30题证据Recall@5=1.0、MRR@5=0.836538；@3证据Recall=0.858974。003的citation_numbers键缺少开头双引号，本地JSON在字符153拒绝，原文/消息/usage保留，未重试、修补或改Prompt。4题@3证据不完整、@5全部恢复；采集时8题人工复核pending，后续结果见下表及 [V0报告](../../evals/reports/DENSE_V0.md)。

DeepSeek八次合计输入17277/输出687/总17964 tokens，含JSON失败；按本日官方峰值未命中价折算约US$0.0060075，是估算，不是实付账单。Embedding usage与实际账单未采集，保持null。完整问题向量/缓存仅在被忽略的data，公开自建原文结果保留归档哈希，无保留题检索。

收尾检查通过29个候选文件（6修改/23新增）、6份Python AST、30份Markdown围栏、72张表格、223处本地链接与64处锚点、JSON/JSONL及固定8项报告；公开12份机器/派生文件和16份运行源码哈希均一致。公开rankings另在新目录离线重评分，所有指标/计数/阶段耗时与原结果逐值相同，新增模型调用0。正式冻结10文件字节未变，计划仅第10节、34主任务＋1弹性日不变；敏感值扫描未发现实际密钥，.env与向量缓存被忽略，git diff --check通过，暂存区为空。

2026-10-20用户主动开启手动验证，实际结果：

| 执行者 | 实际操作 | 结果 |
| --- | --- | --- |
| 用户 | `uv run --frozen python -m evals.baseline verify --dataset evals/datasets/copilot_v0 --output-dir data/evals/dense-v0-20261019-01`及`echo $?` | verified、12文件、退出0；机器pending/accepted=false为原采集快照 |
| 用户运行，Codex只读比对 | `uv run --frozen python -m evals.run --dataset evals/datasets/copilot_v0 --config evals/results/dense-v0-20261019-01/evaluation_config.json --rankings evals/results/dense-v0-20261019-01/rankings.jsonl --output-dir data/manual-d10/replay-01` | 用户反馈已运行；三文件齐全、30题/26可答/4无答案/错误0，逐题与汇总同原基线，本地evaluation_ms另计；model_calls=0、代码12226d7/dirty=false |
| 用户 | 无请求能力的CacheOnly委托给CachedEmbeddings，逐值比较已保存query_vectors | 向量一致30，hit30/miss0/api_calls0；缓存缺失会抛错，不隐式实调 |
| 用户 | 只在`data/manual-d10/changed-prompt.json`副本追加System消息，再运行`evals.baseline plan` | status=error、退出1，按预期拒绝；正式配置未改，无模型调用 |
| 用户人工核对 | 明确确认001/010/002/009/013/020事实/引用支持，003保留结构失败，017拒答正确 | 八行review均confirmed；八题端到端7/8，六条有效回答声明24/24支持（1项合理推论），纯拒答支持率N/A；拒答仅抽检1题 |

用户另确认“已学习，能够解释”，包括采集→缓存→Qdrant→评分→回答契约及003为何仍计失败。上述手动验证没有新增模型调用；本轮仅维护文档和review，未重跑pytest、服务或真实采集。原机器状态/清单的pending/accepted=false保留采集时快照，当前人工状态见独立review，不改写已封存结果。

本次九文件收尾检查通过：8行人工记录/24项声明的引用与原文匹配，公开及本地机器各12文件、16份源码哈希和D8冻结10文件均未变；30份Markdown、75张表格、225处本地链接/64处锚点及固定8项报告通过。计划仅第10节变化，独立只读事实复核通过，git diff --check无输出，暂存区为空。

## 6. GitHub 提交信息

- 分支main跟踪origin/main；开发基点为用户已推送的 [91eea87624778ec88ff71ee65c0e129b1dc8de22](https://github.com/yan-ye-dhy/project-copilot/commit/91eea87624778ec88ff71ee65c0e129b1dc8de22)，开始时工作区干净。
- D10开发commit：[12226d7b2c52b14e1d853288f508a8aff35cc2f6](https://github.com/yan-ye-dhy/project-copilot/commit/12226d7b2c52b14e1d853288f508a8aff35cc2f6)，`feat(evals): add dense V0 baseline and frozen configuration`；用户已推送，输出91eea87..12226d7 main→main，Codex核实本地main/origin/main一致，开始手动验证时工作区干净。
- 本次学习/手动验证与人工复核收尾：**待用户提交/推送**，不预填新commit。Codex未暂存、提交或推送。

## 7. 后续学习提示

用户已确认通过ChatGPT学完并能解释主流程；以下保留为复习重点，结合 [评测说明](../../evals/README.md#d10真实采集与基线合同)和开发commit：

1. 采集器、向量缓存、Qdrant、D9评分器的完整调用关系；D9 model_calls=0为何不等于真实采集免费。
2. 文本指纹与模型/供应商身份，缓存如何避免重复费用、损坏为何停止。
3. 完整Prompt/Schema/参数冻结与运行时漂移校验，为什么模型别名仍有局限。
4. 查询缓存命中时间、批量向量准备时间与真实检索时间的差别。
5. 引用编号有效、事实支持与人工claims复核；拒答N/A为何不能掩盖错误拒答。

## 8. 下一步计划

当前仅指导用户亲自提交/推送九份收尾文件；D10阶段A/B均完成，等待用户明确要求才开启D11。003非法JSON作为V0生成失败保留，可靠性改进留对应阶段，不能重试后覆盖旧基线。
