# 【Day 16 开发报告】

开发与Codex自动实验日期：2026-10-25。**V3在固定10个Hybrid候选内提高证据MRR@5，同时出现@3覆盖退化；代码与真实实验已完成，阶段A待用户提交、推送，阶段B待ChatGPT学习及用户主动开始手动验证。** 运行`rerank-v3-20261025-01`已完整封存，不切换当前默认方案。

## 1. 今日开发目标

复用D14公开V2的同一30道开发题、每题原序10个候选，比较不重排与固定BGE模型重排。保留完整候选身份/原文/metadata/原RRF score、新raw logit及名次、最终5块、真实计时、错误和逐题指标；先独立warmup，再以前两道dev作为smoke，通过后完成30题。本日不拓展召回，不调用Qdrant或收费模型，不使用保留集选参，不自动进入D17。

## 2. 新增和修改的文件

**新增：**

- [evals/rerank_run.py](../../evals/rerank_run.py)：独立plan/collect/verify运行器，复用D9评分、D12比较、D14公开归档验证及D15重排模块。
- [evals/configs/rerank_v3.json](../../evals/configs/rerank_v3.json)：固定父归档SHA、模型revision、CUDA/float16、512 tokens、batch2、10候选评分深度及独立最终5块合同。
- [tests/test_rerank_run.py](../../tests/test_rerank_run.py)：60项离线用例，使用真实冻结数据/父归档的临时副本及fake scorer。
- [公开V3归档](../../evals/results/rerank-v3-20261025-01/manifest.json)：19份产物与1份manifest，共20文件、3,725,251字节；包含候选与两侧评分、逐题比较、分数/token/资源/预热/状态和父合同快照。
- [V3实验报告](../../evals/reports/RERANK_V3.md)与本报告：保存真实指标、改善/退化案例、计时口径及待人工验收状态。

**修改：** README、开发计划第10节、开发日志、学习笔记、面试笔记与evals/README补充D16入口和结果。无新增依赖；既有D8冻结数据、V0/V1/V2/D15归档与源码、API默认、750/150和Prompt保持。

本次预计交付31文件：3份实现/配置/测试、2份报告、20份公开归档和6份入口/进度文档。私有采集目录`data/evals/rerank-v3-20261025-01`及`data/auto-d16`的命令输出、进程资源记录不加入Git。

## 3. 核心实现内容

1. `prepare_experiment`校验冻结数据、固定父manifest完整SHA、父运行/索引/评分合同与冻结源码/依赖SHA，要求同序完整30道dev题、26可答及4无答案、每题10个不重复且原文/metadata与canonical catalog一致的候选，拒绝改预算、混索引、候选或问题篡改。
2. `plan_experiment`完全离线，预告31次本地score调用/310个pair，不导入Torch/Transformers、读取模型缓存、创建客户端或读取`.env`。真实`collect`必须显式`--local`，前置验证本地固定模型的6文件SHA及CUDA可用性，缺失或失败不下载、不回退、不重试，输出目录存在则拒绝覆盖。
3. 真实采集复用一个`LocalCrossEncoder`实例，通过既有`Reranker`重排；`top_k=10`保留全排名供k=1/3/5/10评分，独立`answer_top_k=5`保存最终前5块。本轮未调用LLM，不能把排名深度10写成给LLM的10块上下文。
4. 首题001的10对独立warmup记录加载及首次耗时，不进入正式评分；随后30题各一次，前两题001/002作为smoke且属于30题。warmup或前两题失败即留partial且不封存；之后失败仍保留空hits/error与完整分母。尝试次数、尝试pair、完整有限分数成功次数和成功pair分别记录。
5. 基线rankings逐字节复制原V2，保留历史timings；V3 ranking严格沿用D9字段，原`RetrievedChunk.score`不变，raw logit/token/原排名/新排名另存详情。V3只记录本轮rerank耗时，其余phase及total为null；两侧调用既有评分与比较函数，不另造评测器。
6. 封存19份产物并生成manifest；公开verify校验字节闭集、配置/父身份、30题与原10候选、稳定logit排序、前5块、预算、gold评分、汇总及comparison。无需GPU、缓存、私有问题向量、原父目录或当前Prompt重建，人工学习/验收状态保持pending。

## 4. 关键设计决策

固定`BAAI/bge-reranker-v2-m3` revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`，使用已在D15确认的CUDA/float16、batch2与512输入预算。两侧共用750/150和原10候选，唯一检索变化是重排序；不因结果不理想追加候选、供应商或调参。模型raw logit独立保存，不当成概率，也不与RRF分数相加。

本轮关注检索排序与固定最终5块的覆盖，不生成回答或评测拒答/引用。V2在@3/@5原已全覆盖，因此V3的MRR提升不能表述为补出新证据；同时保留@3退化，避免只报告有利指标。机器记录的`manual_review=pending`和`day16_accepted=false`保留为采集时快照，不用自动通过勾选用户阶段B。

计时把历史候选检索与本轮重排分开。GPU每次评分前后同步、重置allocator峰值，collect检查全部浮点参数实际为CUDA/float16，另存31份观察。warmup包括本进程首次模型加载；OS缓存未清、模型文件加载前已校验SHA、CUDA context已初始化，因此不称为冷盘或完整应用冷启动。

## 5. 自动测试结果

执行者均为Codex，目录`/home/yanye/project-copilot`；以下不登记为用户学习或手动测试。无真实`.env`读取、外部模型API或Qdrant请求，无新增模型下载/费用。

| 实际验证 | 结果与限制 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_rerank_run.py` | 冻结版60 passed，5.14秒；27个测试函数包含参数化用例。fake scorer覆盖离线门禁、31次预算、原候选、完整排名/前5块、稳定并列、分阶段计时、无答案、前两题停止、后续失败分母、调用计数及重封后的语义篡改拒绝；不是模型效果测试 |
| 相关9文件联合回归，完整命令见下 | 沙箱外266 passed，25.09秒；包含本日新增60项，不再另加为326项；覆盖重排/CLI、D9评分/指标、D12比较、D14归档、RAG API及启动 |
| `HF_HUB_OFFLINE=1 .venv/bin/python -m evals.rerank_run plan --dataset evals/datasets/copilot_v0 --hybrid-dir evals/results/hybrid-v2-20261022-01 --config evals/configs/rerank_v3.json > data/auto-d16/plan.json` | 退出0；offline_plan，30题/26可答/4无答案、预算31次/310对、最终5块，模型未加载、API/Qdrant均0 |
| `.venv/bin/python -m evals.rerank_run collect --dataset evals/datasets/copilot_v0 --hybrid-dir evals/results/hybrid-v2-20261022-01 --config evals/configs/rerank_v3.json --output-dir data/evals/rerank-v3-20261025-01 --local > data/auto-d16/collect.json`（实际离线环境及time前缀见下） | 退出0，真实GPU；31次尝试全部成功、310对，正式30题错误0；Embedding/Chat/外部Rerank API及Qdrant请求均0 |
| `.venv/bin/python -m evals.rerank_run verify --dataset evals/datasets/copilot_v0 --output-dir data/evals/rerank-v3-20261025-01` | 私有归档退出0，verified、19份登记产物，manual_review=pending、day16_accepted=false、model_loaded=false |
| `.venv/bin/python -m evals.rerank_run verify --dataset evals/datasets/copilot_v0 --output-dir evals/results/rerank-v3-20261025-01` | 公开归档退出0，verified及其余状态与私有verify一致 |
| 公开归档复制与独立标准库只读核对 | 20文件/3,725,251字节与私有目录原字节相等；28份源码/配置/依赖及6个模型文件SHA符合；60份排名记录、1,200个逐题指标及summary/comparison/cases一致；31份GPU观察和310对token诊断符合 |
| 导入`evals.rerank_run`后检查`sys.modules` | `torch`和`transformers`均未导入，验证入口本身不触发重模型依赖 |

联合回归实际命令如下，仅记录Codex已完成的自动验证：

```bash
timeout --signal=INT 120s .venv/bin/python -m pytest -q \
  tests/test_rerank_run.py tests/test_rerank.py tests/test_rerank_smoke.py \
  tests/test_eval_comparison.py tests/test_eval_run.py tests/test_eval_metrics.py \
  tests/test_hybrid_run.py tests/test_rag_api.py tests/test_main.py
```

真实collect在既有CUDA依赖环境运行，前缀设置`HF_HUB_OFFLINE=1`、关闭遥测/进度条与Tokenizer并行，Hugging Face、Triton、TorchInductor及CUDA缓存均指向本项目`models`下目录；使用`/usr/bin/time -f 'peak_rss_kib=%M\nwall_seconds=%e' -o data/auto-d16/resources.txt`记录进程资源。完整实际环境命令见[开发日志](../DEVELOPMENT_LOG.md)。没有要求用户执行这些自动验证。公开verify只校验保存的GPU字段字节哈希，不能离线重验模型所在硬件；真实collect逐次检查参数设备/精度，独立审计核对31份GPU观察。

真实模型配置为RTX4060 Laptop、Torch2.14.1+cu130、CUDA构建13.0、Transformers5.20.2，Python3.13.14/Linux x86_64。warmup10对原pair 294～445 tokens，dev300对168～494，310对截断均0；本轮真实数据不覆盖长文截断场景，D15既有截断证据保持。

| 真实结果 | V2历史候选重评分 | V3本轮重排 |
| --- | --- | --- |
| 证据Recall@3 / @5 | 1.000000 / 1.000000 | 0.935897 / 1.000000 |
| 证据MRR@5 | 0.897436 | 0.971154 |
| 全部证据覆盖@3 / @5 | 26/26 / 26/26 | 24/26 / 26/26 |
| 成对结果 | 基准 | 4改善、2退化、20不变；4无答案独立列出 |
| 无答案拒答率 | null | null，未生成回答 |

改善题003/012/024/040的gold排名分别2→1、3→1、2→1、2→1。退化题007从2→4；034三个gold首次覆盖名次从[1,3,3]变为[1,4,4]，首gold的MRR不变，但@3仅覆盖1/3证据。完整10候选下Recall@10逐题守恒；这些是程序依据冻结gold得到的可追溯案例，尚未登记用户人工案例分析完成。

warmup模型加载10,330.751ms、推断1,368.537ms、重排总计11,733.402ms；随后30题load均0，rerank median161.644ms、单轮样本p95169.081ms。V3其余timings/total均null，comparison所有phase配对样本0，不把旧V2耗时加新rerank称为端到端或现场基线对照。31次中最大PyTorch allocator allocated/reserved为1,167,455,232/1,189,085,184字节，含常驻模型，不代表全卡显存；本次采集进程wall28.56秒、host peak RSS4,583,248KiB，单独保存于本地资源记录，不是生产吞吐或P95承诺。

## 6. GitHub 提交信息

- 本日从已同步的main、`e6cc917b5505357317ff5506cfa7e1735e9f6c31`开始，远端为`https://github.com/yan-ye-dhy/project-copilot.git`。
- 真实采集manifest记录该base commit及`dirty=true`，对应本日实现尚未提交时的运行；28份实际源码/配置/依赖SHA用于定位运行内容，不追填未来commit。
- 本日所有改动**待用户审核并亲自add、commit、push**；Codex未执行Git写操作。尚无本日已同步的GitHub提交链接，阶段A未登记完整完成。

## 7. 后续学习提示

1. 从`plan_experiment`→`collect_experiment`→既有`Reranker`→D9/D12评分比较→`verify_experiment`跟踪完整运行链，理解为何不重新检索或Embedding。
2. 区分原RRF score与raw logit、评分深度10与最终上下文5，沿`candidate_rank`/`rerank_rank`追踪完整文档身份和稳定并列规则。
3. 结合007和034解释证据Recall、首条证据MRR、全部证据覆盖的差异，以及重排为何可能提前首条证据却损害多段覆盖。
4. 理解独立warmup、正式前两题smoke、30题分母、尝试与成功计数、失败不回退的关系。
5. 区分历史候选计时、本轮rerank耗时、首次加载、allocator显存、host RSS与端到端延迟，并理解公开verify的证据边界。

## 8. 下一步计划

完成本次文档与Git改动审核，向用户提供准确文件清单和提交说明，由用户亲自提交、推送后核实阶段A；随后交ChatGPT学习。用户主动返回并要求开始手动测试后，再逐步指导离线plan/verify、同候选评分核对及一个退化或不变案例分析；未收到该请求前不登记或启动用户手测。

阶段B及本日A/B都完成后，仍等待用户明确开启D17，再按计划统一比较和冻结最终方案。本日不使用保留集选参，不自动把V3设为默认，不提前做D17最终选择。
