# 【Day 10 开发报告】

日期：2026-10-19。D10开发、真实Dense采集与机器配置/结果封存完成，相关269项离线测试通过；阶段A待用户Git交接，阶段B待ChatGPT学习、主动验证和人工回答核验。实际3次Embedding＋8次回答，无自动重试；7条回答结构通过、1条非法JSON保留为失败，人工支持率未填，不进入D11。

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

## 3. 核心实现内容

1. 确认冻结身份及运行时合同后，采用独立pc_eval_dense_v0集合；复用D4入库/成功清单和D9原文映射，不把应用示例PDF混入评测范围。
2. 默认冷缓存按16/2/28文本三批，先验证两题smoke才继续剩余查询。每题立即保存真实排名或失败，失败仍留在26道可答分母内，4道无答案单列。
3. 文档与问题按原文指纹复用向量；损坏缓存拒绝，原子保存，不读取旧项目.env。完整查询向量另存本地归档。
4. 冻结完整模板/变量/上下文规则、Pydantic Schema、生成参数及配置指纹；运行前拒绝隐式漂移。
5. 八类回答复用同次排名前5条，保存消息、原始content、实际模型/usage、校验结果或失败，不重新Embedding。人工claims和支持/拒答判断保持pending。

## 4. 关键设计决策

采集与评分分开：D9仍是零模型调用的重放器，D10保存真实采集调用数及来源。缓存按供应商+模型+维度+编码+文本SHA隔离；统计批耗时和缓存读取，不能摊出虚假的逐题API延迟。当前云端别名仍可能更新，保留运行日期与实际返回模型，不能保证云端权重永久固定。

复用D2–D5协议、D9指标和标准库，自主实现实验缓存/合同/编排；不引入新监控系统或更改业务流程。机器产物按字节封存且拒绝覆盖，review.jsonl独立追加；verify不代表人工引用支持或完整D10验收。

## 5. 自动测试结果

执行者Codex，工作目录新项目；以下全部离线，fake不代表真实供应商或Qdrant效果，不代替用户学习后验证。

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

用户在已说明最多3次Embedding/8次回答与费用后要求“继续完成”，据此执行上述一次范围。30题证据Recall@5=1.0、MRR@5=0.836538；@3证据Recall=0.858974。003的citation_numbers键缺少开头双引号，本地JSON在字符153拒绝，原文/消息/usage保留，未重试、修补或改Prompt。4题@3证据不完整、@5全部恢复；8题人工复核均pending，见 [V0报告](../../evals/reports/DENSE_V0.md)。

DeepSeek八次合计输入17277/输出687/总17964 tokens，含JSON失败；按本日官方峰值未命中价折算约US$0.0060075，是估算，不是实付账单。Embedding usage与实际账单未采集，保持null。完整问题向量/缓存仅在被忽略的data，公开自建原文结果保留归档哈希，无保留题检索。

收尾检查通过29个候选文件（6修改/23新增）、6份Python AST、30份Markdown围栏、72张表格、223处本地链接与64处锚点、JSON/JSONL及固定8项报告；公开12份机器/派生文件和16份运行源码哈希均一致。公开rankings另在新目录离线重评分，所有指标/计数/阶段耗时与原结果逐值相同，新增模型调用0。正式冻结10文件字节未变，计划仅第10节、34主任务＋1弹性日不变；敏感值扫描未发现实际密钥，.env与向量缓存被忽略，git diff --check通过，暂存区为空。

## 6. GitHub 提交信息

- 分支main跟踪origin/main；开发基点为用户已推送的 [91eea87624778ec88ff71ee65c0e129b1dc8de22](https://github.com/yan-ye-dhy/project-copilot/commit/91eea87624778ec88ff71ee65c0e129b1dc8de22)，开始时工作区干净。
- 本次D10 commit：**待用户提交**；推送：**待用户执行**，暂无已同步D10提交链接。Codex未暂存、提交或推送。

## 7. 后续学习提示

交ChatGPT结合 [评测说明](../../evals/README.md#d10真实采集与基线合同)和本次commit学习：

1. 采集器、向量缓存、Qdrant、D9评分器的完整调用关系；D9 model_calls=0为何不等于真实采集免费。
2. 文本指纹与模型/供应商身份，缓存如何避免重复费用、损坏为何停止。
3. 完整Prompt/Schema/参数冻结与运行时漂移校验，为什么模型别名仍有局限。
4. 查询缓存命中时间、批量向量准备时间与真实检索时间的差别。
5. 引用编号有效、事实支持与人工claims复核；拒答N/A为何不能掩盖错误拒答。

## 8. 下一步计划

当前仅指导用户亲自提交/推送已审核的D10代码、非敏感基线产物和文档，再交ChatGPT学习。用户主动要求后再开始手动验证和八题人工支持判断；机器封存已完成，不据此确认人工质量或自动进入D11。003非法JSON作为V0生成失败保留，可靠性改进留对应阶段，不能重试后覆盖旧基线。
