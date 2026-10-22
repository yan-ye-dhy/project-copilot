# D14：Dense、BM25与RRF Hybrid三路对照

实验日期：2026-10-22；运行`hybrid-v2-20261022-01`。**Hybrid在本轮的证据Recall@3达到1.000000，但BM25的证据MRR@5更高；保留默认Dense问答及750/150，暂不提升候选为默认方案。** 当前为Codex自动采集结果，待用户Git同步、ChatGPT学习和手动验证，不能登记为D14阶段A/B均完成。

## 配置与公平比较

完整合同见 [experiment_config](../results/hybrid-v2-20261022-01/experiment_config.json)，入口见 [hybrid_v2配置](../configs/hybrid_v2.json)。复用D8冻结数据指纹`dataset-v1-54806b5174b2f3d2e4bd08b91b57569c5121929731112c3d375d4d42d4306b7e`与原V0的5份同源文档/16片段，只使用30道dev题：26可答、4无答案。保留集未用于调参、选择或检索。

| 固定项 | 本轮约束 |
| --- | --- |
| 切分 | size750/overlap150，空行、换行、空格及字符回退；D12的500候选未晋升 |
| 原文与身份 | 同一V0成功清单、chunk_id、document_id/版本、页及原文位置 |
| Dense | pc_eval_dense_v0，命名向量dense、Cosine、2048维；复用D10原30条问题向量 |
| BM25 | D13的BM25L，k1=1.5/b=0.75/delta=0.5；相同中文/技术词规则，无停用词、HMM关闭 |
| 候选预算 | Dense最多10、BM25最多10；并集理论上限20，当前语料总数16 |
| 融合 | 应用层RRF，常数60、从1起算，同ID去重、相同内容合同、并列按UUID字符串 |
| 评分/回答预算 | 保存深度10，评分k=1/3/5/10；answer_top_k=5不变，本轮不生成回答 |
| Prompt与生成参数 | 原V0版本/模板/Schema/参数完整保存在合同内，无隐式改动 |

每题只查询一次Dense和一次BM25，再把两份**相同候选**提供给Hybrid；两路先后顺序按题交替，原始分数保存在各自rankings，融合只使用排名。实际有30次Qdrant query_points、30次BM25查询、30次RRF；另有集合/原文读取、计数和版本检查，没有写入或重新Embedding。预加载检查实际Qdrant片段与冻结原文一一一致，不读取文档向量。

参考原项目`utils/vdb.py`的HYBRID入口、当前安装SDK的融合源码及 [Qdrant混合查询文档](https://qdrant.tech/documentation/search/hybrid-queries/)。本项目明确采用从1开始的排名与c60，自主实现去重、身份约束和并列规则，没有把服务端默认参数当作本轮合同。

## 三路实际指标

以下为26道可答题的宏平均；证据Recall按gold unit覆盖，证据MRR按首条完整证据的排名倒数。多段证据题的文档命中不代表完整证据已命中。

| k | Dense证据Recall | BM25证据Recall | Hybrid证据Recall | Dense证据MRR | BM25证据MRR | Hybrid证据MRR |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0.666667 | 0.852564 | 0.762821 | 0.730769 | 0.884615 | 0.807692 |
| 3 | 0.858974 | 0.961538 | 1.000000 | 0.807692 | 0.923077 | 0.897436 |
| 5 | 1.000000 | 1.000000 | 1.000000 | 0.836538 | 0.932692 | 0.897436 |
| 10 | 1.000000 | 1.000000 | 1.000000 | 0.836538 | 0.932692 | 0.897436 |

三路文档Recall@5均1.000000，全部gold覆盖题率@3依次22/26、25/26、26/26。三路检索错误均0。原始评分与全部字段见各自的 [Dense汇总](../results/hybrid-v2-20261022-01/dense_retrieval/summary.json)、[BM25汇总](../results/hybrid-v2-20261022-01/bm25_retrieval/summary.json)、[Hybrid汇总](../results/hybrid-v2-20261022-01/hybrid_retrieval/summary.json)。

| 成对比较，后者相对前者 | 改善 | 退化 | 不变 | 无答案，独立计数 |
| --- | --- | --- | --- | --- |
| Dense→Hybrid | 6 | 0 | 20 | 4 |
| Dense→BM25 | 7 | 2 | 17 | 4 |
| BM25→Hybrid | 2 | 4 | 20 | 4 |

改善/退化按所有公开指标及k的成对变化判断，不能只挑某个@k。比较文件分别为 [dense_vs_hybrid](../results/hybrid-v2-20261022-01/dense_vs_hybrid.json)、[dense_vs_bm25](../results/hybrid-v2-20261022-01/dense_vs_bm25.json)、[bm25_vs_hybrid](../results/hybrid-v2-20261022-01/bm25_vs_hybrid.json)。本轮Dense与D10历史的26道可答题指标全部一致，见 [historical_v0_recheck](../results/hybrid-v2-20261022-01/historical_v0_recheck.json)；保留历史结果，不以本轮覆盖V0。

4道无答案三路均返回非空候选。本轮没有回答生成、拒答或引用判定，`refusal_accuracy=null`；检索返回内容不能证明这些问题可答。

## 代表案例与来源排名

逐题gold/指标与结果文件引用见 [case_details](../results/hybrid-v2-20261022-01/case_details.jsonl)；每条候选原分数、两路排名及RRF分数见 [candidate_sources](../results/hybrid-v2-20261022-01/candidate_sources.jsonl)，完整命中原文在三份rankings及三套retrieval目录中。

| dev题及完整gold原文位置 | Dense证据排名 | BM25证据排名 | Hybrid证据排名 | 观察 |
| --- | --- | --- | --- | --- |
| 012：文件改名与内容身份，`[455,552)` | 4 | 1 | 3 | 融合比Dense提前，但低于BM25；支持共识不能保证gold排名第一 |
| 007：空TXT与请求前拒绝，`[542,684)` | 2 | 4 | 2 | Dense补足BM25早期证据，Hybrid保留rank2 |
| 023：预期维度不是模型参数，`[742,892)` | 1 | 2 | 1 | 两块RRF同分，相关块UUID字符串靠前 |
| 040：持久卷生命周期，`[1028,1157)` | 2 | 1 | 2 | 同样的rank1/rank2交换同分，相关块UUID靠后，未保留BM25的rank1 |

以012为例，同一完整gold块两路排名4/1，RRF为`1/64+1/61=0.0320184426`；最终第一块两路排名1/3，分数`1/61+1/63=0.0322664585`。这解释了为何gold虽在词项检索第一，融合后仍排第三。分数计算只依据排名，不使用BM25原分数28.87或Dense的Cosine作直接求和。

023与040两项交换的1/2排名均得到`1/61+1/62`。UUID并列规则保证复现，不含gold信息，也不保证相关块优先；不能通过人工标注临时改变并列规则来改善结果。以上为程序可追溯案例，用户本人原文核对与手算仍待主动开启手动验证后记录。

## 耗时、模型与边界

同进程按题交替执行两路、无显式预热，30题只采一轮。下表是已保存的实际组件统计，单位ms：

| 路线/阶段 | samples | 中位数 | p95 |
| --- | --- | --- | --- |
| Dense search/total | 30 | 5.345 | 6.229 |
| BM25 search/total | 30 | 0.459 | 0.621 |
| Hybrid search，两路逐题之和 | 30 | 5.815 | 6.628 |
| Hybrid fusion | 30 | 0.052 | 0.063 |
| Hybrid total，逐题search加fusion | 30 | 5.865 | 6.675 |

Hybrid total是共享候选的顺序组件组合，**不是独立Hybrid请求或含云端Embedding的端到端延迟**；各列中位数不是简单把两个统计中位数相加。预加载/离线合同与快照准备约852.423ms单列，不含在逐题延迟。Dense→Hybrid成对total差中位数约+0.506ms，但此差本来就包含额外BM25及融合，不能据此声称测量了独立线上请求的性能。

本轮Embedding、问题Embedding、Chat、Qdrant写入均0，没有读取.env或产生新增模型费用；Embedding及Rerank阶段没有发生，耗时保持null。词项在进程内、Dense访问本机Qdrant，部署方式不同；少量热态查询不能推出一般提速、显著性或生产P95结论。

当前自建语料很小，技术名词和错误码较多，BM25在本轮MRR领先不代表能泛化到任意自然语言问题。RRF候选预算、常数及并列规则固定，没有对本轮结果反复选优，也没有用保留集选参数。默认仍沿用Dense及750/150，D17再按预定实验链路统一选型与冻结；下一模块D15需要用户在完成D14阶段A/B后明确开启。

## 归档与验收状态

[manifest](../results/hybrid-v2-20261022-01/manifest.json)记录采集时间、环境、base commit、23份源码SHA、完整合同指纹与23份产物SHA；公开目录共24份文件，与本地`data/evals/hybrid-v2-20261022-01`保持原字节一致，不包含原问题向量。Qdrant server为1.19.2、SDK为1.19.1，jieba/rank-bm25/NumPy版本为0.42.1/0.2.2/2.5.4。

运行从用户已同步的D13收尾`ca769dc4ea85010e30193c42f5c04991333b5268`开始，采集时D14新增内容未提交，故`dirty=true`。机器状态为complete/recorded、manual_review=pending、day14_accepted=false；verify实际通过23份文件，不能据此自动确认人工学习与验收。Git提交由用户执行，学习和手测完成后另在日报/日志补记，不修改封存产物或追写未来提交ID。

本轮没有改动D8冻结数据、原V0/V1归档、D13算法或旧运行源码。新实验校验保存的旧基线合同指纹，历史verify不通过当前Prompt重建它；相关未来Prompt回归用例已覆盖。自动测试及实际CLI命令见 [D14报告](../../docs/reports/DAY_14.md)，手动验收仍等待用户主动开始。
