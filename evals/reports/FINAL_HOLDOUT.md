# D17：冻结最终方案与保留集验收

日期：2026-10-26。最终实验配置为 **Hybrid＋BGE Rerank，750/150、最终5块**。只根据30道开发题选择，并先提交推送[e676d5021d84d0bcaa7eeb700287833d844e703a](https://github.com/yan-ye-dhy/project-copilot/commit/e676d5021d84d0bcaa7eeb700287833d844e703a)，随后运行唯一一轮真实保留集比较；运行`final-holdout-20261026-01`的code记录该commit及dirty=false。

## 开发集选择与冻结

完整采集前决策见[FINAL_SELECTION.md](FINAL_SELECTION.md)，配置见[final_v1.json](../configs/final_v1.json)，指纹为`final-selection-v1-d77720ad02c9ac3caa83b3fa6981c6d197b1c7238d220cf64cca3dd5ced2f450`。V0/V1/V2/V3及BM25独立路线均重新以原文gold验证，最终k固定5。先比较证据Recall，再比较全部证据覆盖和MRR；V3还须满足已测重排中位数≤250ms、reserved≤2GiB。该规则写于既有开发实验之后、保留集运行之前，不声称开发实验前预注册。

V1的500字符方案在dev的证据Recall@5为0.923077，未晋升。Hybrid与V3在dev的Recall@5均1，MRR@5分别0.897436与0.971154，因此选择V3；同时保留V3在@3的007/034退化。D12的012改善不能掩盖003退化；D14保留BM25排名领先的案例；D16仍说明重排只能改变候选内顺序。这些失败和取舍不因最终选择而删除。

冻结D10的Embedding、Prompt/Schema和生成参数；本轮不生成回答。两侧使用同一保留问题向量、同一V0索引、同一语料与750/150，保存10名、最终取5块。Final复用一次Dense的10候选及同题BM25的最多10候选，RRF从1开始、c60，融合保留10，再用固定BGE revision、CUDA/float16、batch2、512输入tokens重排。D17比较完整最终组合与V0，不把组合收益归因于单一算法。

## 保留集实际结果

保留集为D8冻结的10题：9可答、1无答案，8类题均有代表；不改变分族划分，不把失败题从分母移除。以下是9道可答题的宏平均：

| k | V0证据Recall | Final证据Recall | V0证据MRR | Final证据MRR | V0全部证据覆盖 | Final全部证据覆盖 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0.888889 | 0.944444 | 0.888889 | 1.000000 | 0.888889 | 0.888889 |
| 3 | 1.000000 | 1.000000 | 0.944444 | 1.000000 | 1.000000 | 1.000000 |
| 5 | 1.000000 | 1.000000 | 0.944444 | 1.000000 | 1.000000 | 1.000000 |
| 10 | 1.000000 | 1.000000 | 0.944444 | 1.000000 | 1.000000 | 1.000000 |

两侧检索错误均0；综合全部k及五类指标，1题改善、8题指标不变，1无答案另列，没有退化或mixed题。分类结果和全部原文见[comparison](../results/final-holdout-20261026-01/comparison.json)与[case_details](../results/final-holdout-20261026-01/case_details.jsonl)。这批自建文档/10题只运行一轮，不能推广为普遍效果、统计显著提升或回答准确率。

| 题号 | 原文依据与变化 | 结论 |
| --- | --- | --- |
| pc-d8-033，中文长问 | startup原文[798,927)、Compose原文[1314,1551)；两条gold首次完整覆盖名次[3,2]→[2,1] | 首条证据提前，@1证据Recall由0到1/2，MRR由0到1；仍需第2块才覆盖全部gold |
| pc-d7-005，上传跨段 | upload原文[183,361)、[542,684)；两条gold两侧均在rank1 | 指标不变，不等于所有其他候选顺序都相同 |
| pc-d8-038，无答案 | 语料没有实际HNSW m/ef_construct值，gold为空，两侧都有非空候选 | Recall/MRR为null、排除于9题均值；未生成回答，refusal_accuracy仍null |

本次逐题原文核对和程序分析由Codex完成，未登记为用户已完成D17学习或人工案例复盘。看到保留集后未修改配置、Prompt、阈值或再选方案；选型文件与采集前Git提交原字节一致。

## 调用、成本与计时

用户明确批准最多1次Embedding、10次本地Dense查询及110对本地GPU评分。本轮实际符合：一次10问题批量Embedding（1189 UTF-8字节、缓存miss10/API1），10次query_points、10次BM25、10次RRF；开发题001预热10对，随后保留题100对，共11次评分/110对，全部成功。另有集合、完成片段、点数和版本的只读预检；没有写库、重索引、模型下载、回答生成或自动重试。

按[官方Embedding-3价格](https://docs.bigmodel.cn/cn/guide/models/embedding/embedding-3)0.5元/百万tokens，授权前以3万输入tokens预算约¥0.015；实际usage和账单未采集，均null，预算不等于实付。两侧共享新问题向量，未分别收费；独立缓存核对10条逐值指纹一致、hit10/miss0/API0。文档向量沿用10月19日索引；云端模型别名不能保证权重永久不变，本轮日期/参数及问题向量SHA已记录。

| 实际组件 | median / p95或一次耗时 |
| --- | --- |
| 一批10问题Embedding | 1008.739ms，整批单列，不均摊冒充逐题测量 |
| V0本地Dense查询 | 37.660 / 672.216ms |
| Final本地重排 | 164.558 / 375.744ms |
| Final共享候选组件之和 | 202.743 / 1202.886ms，逐题search＋fusion＋rerank，不含批量Embedding/LLM |
| 独立开发题warmup | 总11873.864ms，模型加载10312.918ms、推断1521.046ms，不进10题统计 |

同一进程先Dense再BM25/RRF/重排，候选是共享的，Final不是独立线上请求。Dense没有单独预热，10题单轮的p95就是样本最大值；这些数字不能称生产P95、端到端SLA或跨运行的可靠提速。保留逐题时延，不只展示中位数；本轮没有因较慢样本重跑。

RTX4060 Laptop、Torch2.14.1+cu130/CUDA13.0、Transformers5.20.2，11次观测均cuda:0/float16。110对实际模型输入294～487 tokens，截断0；不据此新增长文截断验收。最大PyTorch allocated/reserved为1,167,384,064/1,189,085,184字节，包含常驻模型，不等于全卡显存。采集进程wall28.74秒、host peak RSS4,528,648KiB，资源记录保存在本地data/auto-d17/resources.txt。

## 归档、复现与未完成项

[公开manifest](../results/final-holdout-20261026-01/manifest.json)封存23份产物，目录共24文件、2,037,047字节，与私有目录逐字节一致。保存冻结选型、两侧及中间候选/评分、原分数/logit/名次、原文gold、批次缓存/向量SHA、warmup/runtime/status；不包含密钥、模型权重或私有向量。11份执行源码/依赖SHA与选择配置SHA可核对。

私有与公开verify均退出0；重算排名、BM25、RRF、logit排序、gold指标、成对结果、案例和预算。另由[独立原文/缓存审计](../../docs/evidence/D17_HOLDOUT_AUDIT.json)重算4路40题记录×4k×5指标共800个值，检查10个缓存问题向量、11次GPU观察及公开/私有文件，均一致；该审计不新增模型请求。plan/verify不加载Torch或模型、不读.env；collect必须--live，先检验冻结/服务/缓存，前两题执行失败即停止，后续失败留完整分母，无回退。

本轮新增27项离线用例，使用内存Qdrant和fake向量/评分；7文件相关回归共275 passed、21.42秒，包含新增用例，不与中间重复运行累加。具体命令见[D17日报](../../docs/reports/DAY_17.md)。未改变D8、V0/V1/V2/V3、D15或D16验收收据。

当前冻结的是最终实验方案，HTTP问答API继续使用既有Dense；未宣称优化链已接入在线请求。学习和用户主动开启后的手动验证属于阶段B，尚未完成；机器manual_review=pending/day17_accepted=false保留采集时快照。D18只在用户明确要求后开始，后续可靠性修复不能冒用本轮保留集身份或用其结果继续调参。
