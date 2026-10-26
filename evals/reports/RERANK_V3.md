# D16：固定Hybrid候选的BGE重排对照

实验日期：2026-10-25；运行`rerank-v3-20261025-01`。同一30道开发题、每题相同10个V2候选，证据MRR@5从**0.897436升至0.971154**，证据Recall@5均为**1.000000**；证据Recall@3从**1.000000降至0.935897**。26道可答题中4题改善、2题退化、20题不变，4道无答案另列。本轮保留排序改善与退化，不据此切换默认检索方案。

D16开发提交1095113已由用户推送，阶段A完成。2026-10-26用户确认学习理解，并明确委托Codex执行本次离线验证及Git提交推送；学习与本次委托验证完成，不登记为用户亲自终端操作或逐案例人工复核。机器归档保持`manual_review=pending`、`day16_accepted=false`作为原采集快照，本次记录另存[委托验证收据](../../docs/evidence/D16_DELEGATED_VERIFICATION_20261026.json)。

## 比较合同与候选来源

完整合同见[experiment_config](../results/rerank-v3-20261025-01/experiment_config.json)，独立参数见[rerank_v3配置](../configs/rerank_v3.json)。两侧沿用D8冻结数据指纹`dataset-v1-54806b5174b2f3d2e4bd08b91b57569c5121929731112c3d375d4d42d4306b7e`、原V0的5份文档/16片段和750/150切分，只评分原顺序30道dev题：26可答、4无答案。保留集未用于评分、调参或方案选择。

| 项目 | 本轮固定合同 |
| --- | --- |
| 父运行 | 公开`hybrid-v2-20261022-01`，manifest SHA-256为`0263150ef53e349415c3a4aad68c5bd218c0fa4bfeb194681338d4b7ce4055ab` |
| 原候选 | 每题原序10个Hybrid候选，完整text、metadata、chunk_id和RRF score；不扩召回或重查向量库 |
| 模型 | `BAAI/bge-reranker-v2-m3`，revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e` |
| 运行参数 | CUDA、float16、batch_size=2、max_length=512、max_candidates=10、local_files_only=true |
| 排名与回答预算 | `RerankConfig.top_k=10`保留完整候选排序，用于k=1/3/5/10评分；独立`answer_top_k=5`，两侧另存前5块，本轮不生成回答 |
| 输入与分数 | 完整query，仅截断模型输入第二段；返回原文不截断；raw logit独立保存，不覆盖或混合原RRF score |
| 预热与smoke | 首道dev题001的10对独立预热一次；随后30题各评分一次，前两题001/002作为smoke且计入30题 |
| 失败规则 | warmup或前两题失败即停止、只留partial且不封存；之后失败保留空排名、error及完整30题分母，不回退或重试 |

`baseline_rankings.jsonl`是父归档`hybrid_rankings.jsonl`的原字节复制，SHA为`9f28eb6001f1a0d39768da128d552fdb8bc3fa391a4e4cf45592b3c38c1b9096`。这是历史V2候选及其历史timings，本轮没有重新执行Dense、BM25、RRF或Qdrant查询。新排序只改变同一候选集合内的顺序，不能找到候选之外的证据。

模型6个本地文件在加载前逐一核对大小及SHA，权重只校验一次；权重为2,271,071,852字节、SHA-256为`d9e3e081faff1eefb84019509b2f5558fd74c1a05a2c7db22f74174fcedb5286`，来源记录引用既有D15模型准备证据。没有下载权重、读取真实`.env`或调用外部模型API。

## 检索指标

下表为26道可答题的宏平均。证据Recall按gold unit完整覆盖计算；证据MRR取首条完整证据的排名倒数；“全部证据覆盖”要求该题所有gold均被前k块覆盖。文档命中、首条证据提前和完整多段证据覆盖分别统计。

| k | V2证据Recall | V3证据Recall | V2证据MRR | V3证据MRR | V2全部证据覆盖题数 | V3全部证据覆盖题数 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0.762821 | 0.916667 | 0.807692 | 0.961538 | 19/26 | 23/26 |
| 3 | 1.000000 | 0.935897 | 0.897436 | 0.961538 | 26/26 | 24/26 |
| 5 | 1.000000 | 1.000000 | 0.897436 | 0.971154 | 26/26 | 26/26 |
| 10 | 1.000000 | 1.000000 | 0.897436 | 0.971154 | 26/26 | 26/26 |

两侧文档Recall@5均为1.000000，文档MRR@5从0.935897升至0.971154；检索错误均0。完整指标见[V2重评分汇总](../results/rerank-v3-20261025-01/baseline_retrieval/summary.json)、[V3汇总](../results/rerank-v3-20261025-01/rerank_retrieval/summary.json)及[成对比较](../results/rerank-v3-20261025-01/comparison.json)。

改善/退化使用D12既有规则，综合所有公开k和指标的逐题变化：改善4题（003、012、024、040），退化2题（007、034），不变20题；本轮没有mixed题。这里的“不变”表示公开检索指标不变，不能推出所有候选顺序或logit不变。每题完整10候选是同一集合，因而证据Recall@10及全部证据覆盖@10逐题守恒，MRR仍可能随顺序变化。

V2原本在@3和@5已经覆盖全部gold，本轮@5的改善是较早排序，不能称为新增召回或回答准确率提高。@3出现明确退化，也不能用@5的MRR提升概括所有k均改善。30题只采一轮，不作统计显著性或一般效果保证。

4道无答案题017、018、036、037，两侧都返回非空候选；其检索指标为null并排除在26道可答题均值之外。没有生成回答、拒答或引用判定，`refusal_accuracy=null`；不能把非空结果或重排分数解释为可回答证据。

## 改善与退化案例

逐题gold、两侧完整指标、前5块及比较结果见[case_details](../results/rerank-v3-20261025-01/case_details.jsonl)，原名次、新名次、raw logit、完整10候选和token诊断见[score_details](../results/rerank-v3-20261025-01/score_details.jsonl)。下表列gold完整证据首次覆盖的名次；位置为冻结原文Unicode字符区间。

| dev题 | 证据位置与问题要点 | V2→V3证据排名 | 观察 |
| --- | --- | --- | --- |
| 003 | 启动文档`[943,1056)`，无密钥启动与客户端创建时拒绝空值 | 2→1 | 首条完整证据提前，@1由未覆盖变为覆盖 |
| 012 | 身份文档`[455,552)`，改名/移动不改变内容身份 | 3→1 | 与D15单题观察一致；本次属于完整dev对照的一题 |
| 024 | 向量文档`[894,987)`，新集合是项目约束而非Qdrant普遍限制 | 2→1 | 原第一块已命中文档，但未覆盖本题gold；文档命中不等于证据命中 |
| 040 | Compose文档`[1028,1157)`，容器与持久卷生命周期 | 2→1 | 同一候选中的gold提前，没有新增证据 |
| 007 | 上传文档`[542,684)`，0字节TXT在模型请求前返回422 | 2→4 | @3证据Recall由1降为0，@5仍完整覆盖；MRR@5由0.5降为0.25 |
| 034 | Compose`[925,1015)`及启动文档`[163,343)`、`[345,485)`，API启动与lifespan配置 | [1,3,3]→[1,4,4] | 首条gold仍在1，证据MRR保持1，但@3仅覆盖1/3证据，全部覆盖由true变false |

007说明模型可能把直接回答问题的证据排到候选内更晚位置；034说明只看MRR会漏掉多段证据退化。这里据已冻结gold及程序映射描述可观测变化，未把程序结果登记为用户已完成逐条人工案例复核。

## 耗时、截断和资源

[warmup](../results/rerank-v3-20261025-01/warmup.json)对第一道001题的原10候选评分一次，专门记录本进程加载及首次推断；这10对不进入30题指标和逐题延迟统计。前两道dev smoke包含在其后30次正式评分中，没有为smoke重复评分。共尝试31次本地score调用、310个pair，31次均成功，成功返回310个有效原始分数；其中正式评分300对、独立warmup10对，外部模型API和Qdrant请求均0。

| 测量范围 | 本轮结果 |
| --- | --- |
| warmup模型加载 | 10,330.751ms |
| warmup模型推断 / scoring_total / 整体重排 | 1,368.537 / 11,731.536 / 11,733.402ms |
| 30道dev模型加载 | 每题均0ms，同一实例复用 |
| 30道dev重排整体耗时 | median 161.644ms，单轮样本p95 169.081ms；包含分词、推断、排序及本轮观察开销 |
| warmup输入token与截断 | 10对，294～445 tokens，截断0 |
| dev输入token与截断 | 300对，168～494 tokens，截断0 |
| 31次中最大PyTorch allocated / reserved峰值 | 1,167,455,232 / 1,189,085,184字节 |
| 本次采集进程wall / host peak RSS | 28.56秒 / 4,583,248KiB；来自本地`data/auto-d16/resources.txt` |

模型输入预算512 tokens；本轮310对均未触发截断，不能据此声称本轮真实验证了长文截断。`only_second`及超长query拒绝由D15合同和既有测试覆盖；返回的完整片段原文始终保留。

机器为Linux x86_64、Python3.13.14、RTX4060 Laptop GPU（compute capability 8.9、设备总内存8,585,216,000字节）；Torch2.14.1+cu130、CUDA编译版本13.0、Transformers5.20.2。其余依赖版本及6个模型文件哈希见[runtime](../results/rerank-v3-20261025-01/runtime.json)。真实collect逐次检查所有浮点模型参数在CUDA且为float16，并保存31份实际参数设备、dtype与allocator观察；独立只读审计核对这些观察。

每次调用前后同步CUDA并重置allocator峰值。首遍峰值包含模型加载，热态峰值包含常驻模型；PyTorch allocator统计不等于全卡、全部CUDA context或系统内存。peak RSS是宿主进程内存，也不是显存。模型文件已存在且加载前计算过SHA，OS文件缓存未清，CUDA context在warmup前初始化；“首次加载”仅指本进程首次模型加载，不能称为冷盘或完整应用冷启动。

V3排名行只有`timings_ms.rerank`为本轮实测，其余embedding/search/fusion/total均为null。V2的search/fusion/total来自2026-10-22历史归档，本轮原样复制，不能与新rerank相加冒充当前端到端耗时。比较文件所有phase的`paired_samples=0`、`excluded_pairs=30`，没有计算配对延迟差；上述单轮p95也不能称为生产P95或吞吐基准。

## 归档、校验与复现边界

[manifest](../results/rerank-v3-20261025-01/manifest.json)保存运行时base commit `e6cc917b5505357317ff5506cfa7e1735e9f6c31`、`dirty=true`、28份实际源码/配置/依赖SHA、父manifest身份、配置指纹、状态与19份产物SHA。公开目录共20份文件、3,725,251字节，与本地`data/evals/rerank-v3-20261025-01`逐文件原字节一致；没有包含模型权重、缓存、私有问题向量或凭据。

产物包括两侧完整rankings及各自3件retrieval评分、experiment/evaluation配置、index/parent manifest、父状态快照、comparison、score_details、case_details、warmup、runtime、status及最终manifest。D8、V0/V1/V2归档、D15证据与核心代码、API默认、750/150和Prompt均保持原样；V3源码在采集后冻结，不为文档收尾回写运行证据。

plan校验公开父归档、冻结数据和源码合同，不导入Torch/Transformers或加载模型；collect必须显式`--local`，缺缓存/CUDA或错误时无下载、回退、覆盖或自动重试。verify只依赖公开结果和冻结数据，重验字节闭集、父身份、原候选、完整排序、raw分数、token/预算、逐题gold评分、汇总与比较，不需要GPU、模型缓存、原私有父目录或当前Prompt重建。

verify对保存的GPU观察做产物哈希完整性检查，不重新检查真实硬件或离线运行模型，也没有逐值重新验证GPU字段。实际设备/精度由本次collect的参数检查和31份观察的独立审计证明；不能把公开离线verify描述为再次进行硬件验收。

新增60项离线测试通过（5.14秒），用确定性fake scorer覆盖候选/预算/失败/错误分母/封存/CLI与篡改拒绝；相关9文件联合回归沙箱外266 passed（25.09秒，包含新增60项）。私有及公开verify均退出0，verified、19份登记产物、model_loaded=false；独立标准库审计重算60份排名记录、1,200个逐题指标及汇总/比较/案例，结果一致。测试命令及真实采集、verify记录见[D16日报](../../docs/reports/DAY_16.md)。自动验证不替代用户学习和手动验证。

本轮没有CPU对照、GPU float32对照、多候选数搜索、重复计时、回答生成或保留集实验，不作跨精度加速和最终默认方案结论。2026-10-26本次委托验证已核对原归档、两侧60条重放、同候选/分数与012/007/034案例，以及三项拒绝路径；没有新增模型推断。D17的统一方案选择及冻结仍等待用户明确开启。
