# 【Day 14 开发报告】

开发与自动验证日期：2026-10-22。**D14功能实现和自动验证完成，阶段A待用户亲自提交并推送；阶段B待ChatGPT学习及用户主动开启手动验证。** 本日实现应用层RRF并比较Dense/BM25/Hybrid，保留750/150和现有Dense问答默认路由，未开启D15。

## 1. 今日开发目标

复用D13统一分词、原V0成功片段和D10原问题向量，实现按排名融合与独立混合检索编排；在相同30道dev题、切分和候选预算下，保存三路效果、逐题来源与耗时。RRF常数固定60，不在本日追加参数搜索、Rerank或回答生成。

## 2. 新增和修改的文件

**新增：**

- `src/project_copilot/rag/fusion.py`：RRF纯函数、片段去重、身份冲突拒绝与稳定并列顺序。
- `src/project_copilot/rag/hybrid.py`：`HybridRetriever`，约束两路预算、成功清单和片段内容，组合现有Dense及BM25接口。
- `evals/hybrid_run.py`：独立的plan/collect/verify入口，原向量复用、只读真实采集、三路离线评分、对照和封存。
- `evals/configs/hybrid_v2.json`：绑定冻结数据/V0配置、两路各10候选、RRF常数60及固定回答预算5。
- `tests/test_fusion.py`、`tests/test_hybrid.py`、`tests/test_hybrid_run.py`：分别44、32、31项新增用例。
- [HYBRID_V2实验报告](../../evals/reports/HYBRID_V2.md)、本报告及[公开运行归档](../../evals/results/hybrid-v2-20261022-01/manifest.json)。归档共24份文件，其中23份产物由manifest登记SHA；不含向量或密钥。

**修改：** 根README、evals/README、开发计划第10节、开发日志、学习笔记和面试笔记。没有新增依赖、修改旧业务源码或默认API；D10冻结的`retrieval.py`和既有评测脚本保留，新编排放在独立模块。

## 3. 核心实现内容

1. `reciprocal_rank_fusion`按每路从1开始的原始排名计算`1/(60+rank)`，同chunk_id跨路相加，不使用Cosine与BM25原分数直接相加。同路重复只计首次且不压缩后续排名；同ID原文或元数据冲突即失败，分数并列按UUID字符串排序。
2. `HybridRetriever`将Dense/BM25候选预算与最终top_k分别约束；构造及请求前后检查两套成功清单相同，校验候选原文和元数据。有效无命中可为空，执行异常直接传播，不把失败包装成正常空结果。该接口留给后续组合使用，默认问答链没有切换。
3. 三路实验复用原30条2048维问题向量、5份文档的同一16块原文。每题只采集一次Dense和一次BM25，再用这两份原候选融合；按题交替先执行Dense或BM25，避免给Hybrid单独重查出另一批候选。
4. 复用D9证据位置映射/评分与D12成对比较，封存三份rankings、三套retrieval结果、三组成对比较、候选来源排名/RRF分数、原文快照、gold及运行合同。verify只校验保存的合同与封存字节，不依赖未来当前Prompt重新生成旧配置。

## 4. 关键设计决策

本日仍以750/150、原分隔符和相同V0集合为基础，只引入检索与融合因素。每路最多10候选，并集理论上限20、当前16块语料下实际不超过16；三路均保存深度10供k=1/3/5/10评分，回答预算仍为5块，本轮没有生成回答。

RRF与编排为新项目自主实现，沿用D3/D4身份契约、D13分词/快照；独立运行器保留D10源码及Prompt冻结规则。新增`hybrid.py`替代在旧`retrieval.py`上直接扩展，避免使旧基线的运行源码哈希失效；没有引入新服务或重复建设评测器。

本轮证据Recall@5三路均1.000000，证据MRR@5为Dense **0.836538**、BM25 **0.932692**、Hybrid **0.897436**。Hybrid的证据Recall@3达到1.000000，但BM25的证据MRR@5更高；不能据此声称融合总是最优。默认方案不因这一轮小语料结果自动切换，统一选择及冻结仍在D17。

## 5. 自动测试结果

执行者Codex，工作目录新项目，日期2026-10-22。以下是开发期自动验证，未登记为用户学习或手动验证。

```bash
.venv/bin/python -m pytest -q tests/test_fusion.py tests/test_hybrid.py tests/test_hybrid_run.py tests/test_bm25.py tests/test_bm25_cli.py tests/test_dense_pipeline.py tests/test_qdrant.py tests/test_documents.py tests/test_chunking_run.py tests/test_eval_comparison.py tests/test_baseline_config.py tests/test_eval_run.py tests/test_eval_metrics.py tests/test_eval_freeze.py tests/test_eval_dataset.py
.venv/bin/python -m pytest -q tests/test_hybrid_run.py -k future_prompt
```

第一条实际为**465 passed，32.26秒**；随后新增历史verify不受未来Prompt更新影响的回归用例，第二条实际为**1 passed、30 deselected，2.30秒**。合计466项不同用例通过，其中D14新增107项；没有将分两次执行写成单次完整466项。覆盖排名公式、空路/重复/并列、参数、身份冲突、失败传播、陈旧清单、原向量合同、三路共享候选及封存篡改拒绝。

实际CLI共同参数如下，plan与collect均使用同一组输入：

```bash
.venv/bin/python -m evals.hybrid_run plan \
  --dataset evals/datasets/copilot_v0 \
  --baseline-dir evals/results/dense-v0-20261019-01 \
  --baseline-private-dir data/evals/dense-v0-20261019-01 \
  --config evals/configs/hybrid_v2.json

.venv/bin/python -m evals.hybrid_run collect \
  --dataset evals/datasets/copilot_v0 \
  --baseline-dir evals/results/dense-v0-20261019-01 \
  --baseline-private-dir data/evals/dense-v0-20261019-01 \
  --config evals/configs/hybrid_v2.json \
  --output-dir data/evals/hybrid-v2-20261022-01

.venv/bin/python -m evals.hybrid_run verify \
  --dataset evals/datasets/copilot_v0 \
  --output-dir data/evals/hybrid-v2-20261022-01
```

| 实际自动验证 | 结果与边界 |
| --- | --- |
| 离线plan | 原冻结输入、成功清单及30条原问题向量合同校验通过；不允许缺失时回退请求模型 |
| 启动既有Docker Qdrant后collect | 真实5文档/16片段与冻结原文完全对齐；30次query_points、30次本地BM25、30次融合；三路检索错误均0，无自动重试 |
| Qdrant只读范围 | 另有集合配置、精确payload读取、count和server info检查；不是只有30次HTTP请求。没有写集合、读取文档向量或重新入库 |
| 原V0历史对照 | 26道可答题全部指标不变，4道无答案独立保留；历史结果未覆盖 |
| 私有及公开结果目录verify | 均verified，23份封存产物；manual_review=pending、day14_accepted=false，尚未代用户确认 |
| 模型与费用 | Embedding、问题Embedding、Chat均0；没有读取.env、收费调用或新增费用 |
| D8 freeze verify、原私有V0 baseline verify、公开V1 chunking_run verify | 均verified；数据指纹及12份V0/19份V1封存文件保持，旧机器pending状态不改写已完成的人工记录 |

真实服务为Qdrant 1.19.2、SDK 1.19.1；沿用jieba 0.42.1、rank-bm25 0.2.2、NumPy 2.5.4。预加载/离线准备约852.423ms单列；Hybrid耗时是同题两路search加fusion的顺序组合，非独立端到端请求，未计云端Embedding。详见[效果与耗时口径](../../evals/reports/HYBRID_V2.md)。4道无答案三路均有非空命中，未生成回答，拒答正确率为null；没有保留集调参、人工复核或泛化提升结论。

独立只读审查用标准库重算全部30题RRF、90条排名的原文连续区间gold覆盖及指标，均与产物一致。公开24文件与本地原字节一致；D14的23份源码/23份产物、V1原21份源码/19份产物以及D13收据6份源码/依赖SHA保持。8份Markdown的空白/围栏、56张表列数、232处本地链接/11锚点及固定8节报告检查通过，计划第10节外和历史日志/笔记保留；git diff --check无输出、暂存区为空，本轮39个文件待用户审核提交。

## 6. GitHub 提交信息

- 分支main跟踪origin/main；远端`https://github.com/yan-ye-dhy/project-copilot.git`。
- 起点为用户已推送的D13收尾 [ca769dc4ea85010e30193c42f5c04991333b5268](https://github.com/yan-ye-dhy/project-copilot/commit/ca769dc4ea85010e30193c42f5c04991333b5268)，任务开始时本地/跟踪分支一致、工作区干净。
- 本日改动**待用户亲自add、commit、push**；暂无D14已同步提交链接，阶段A尚待Git交接完成。Codex未执行Git写操作。
- 运行归档保留实际base_commit及dirty=true，因为采集发生在D14新增文件尚未提交时；23份源码SHA用于对应实际运行内容，不追填未来提交ID。

## 7. 后续学习提示

1. 同一问题→原Dense/BM25候选→RRF→原文证据→离线指标的调用链，以及生产编排与缓存向量实验入口的区别。
2. RRF公式、从1起算、去重后保留原排名、缺一路与UUID并列顺序；结合012的两个实际分数自己核算一次。
3. 为什么必须对齐原文/清单/切分，为什么原始分数和融合分数都不是答案置信度。
4. 候选预算、排名深度、固定回答预算与证据Recall/MRR的区别；012为何融合仍落后BM25、023/040为何同分但结果不同。
5. 同一批候选的公平比较、组件耗时与真实端到端耗时，以及小型自建技术文档语料对结论的限制。

## 8. 下一步计划

先指导用户审核并亲自提交推送本日文件，核实后补充实际Git信息；再由ChatGPT结合代码与提交讲解。等待用户主动发送“开始D14手动验证”后，再分步验证封存、三路重评分、实际候选/RRF及副本拒绝；此时不提前登记本人手算或案例核对完成。

D14阶段A/B都完成后，仍等待用户明确开启D15，再进行Rerank最小适配。本日不加载Reranker、不调第二组RRF参数、不修改默认问答方案，35日节奏不变。
