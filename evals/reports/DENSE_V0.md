# Dense V0基线记录

运行：`dense-v0-20261019-01`；完成时间2026-10-19 19:07:20（UTC+8）。执行者Codex，用户在已说明3次Embedding＋最多8次回答及费用后要求“继续完成”；自动采集独立于后续用户学习/手动验证。

## 范围与冻结身份

仅使用已冻结dev30题，26可答/4无答案；5份自建中文语料、16片段，未运行10道保留题。数据较小且由同5份语料反向拟题，不能据高召回宣称生产场景泛化、优化收益或统计显著。

- 数据：`dataset-v1-54806b5174b2f3d2e4bd08b91b57569c5121929731112c3d375d4d42d4306b7e`。
- 完整配置：`baseline-config-v1-2e43988981644b482c5917093586b25e406e5158452e84a0478a3326d793bea1`，见 [原配置](../configs/dense_v0.json)及 [运行副本](../results/dense-v0-20261019-01/baseline_config.json)。System/User原文与模板哈希、Schema、变量/上下文规则、生成参数均保存。
- 代码基点91eea87624778ec88ff71ee65c0e129b1dc8de22，运行时dirty=true；[运行收据](../results/dense-v0-20261019-01/run_manifest.json)保存16份相关源码SHA256，不能冒称当时已有D10干净提交。
- Embedding：embedding-3/2048维/float；750/150切分，无Hybrid/Rerank。独立Qdrant1.19.2集合pc_eval_dense_v0，dense/Cosine，精确16点；原应用集合仍2点。
- 生成：deepseek-flash、temperature0、max_tokens1024、JSON object、thinking disabled、重试0；实际响应model字段为deepseek-flash。完整参数固定，不能据别名保证云端权重永远不变。

## 真实检索结果

k表示返回的原始chunk数量；按26道可答题宏平均，失败若出现仍计分母，4道无答案单独统计。gold映射沿用D9完整原文连续覆盖规则。

| k | 文档Recall | 文档MRR | 证据Recall | 证据MRR | 全部必需证据覆盖 |
| --- | --- | --- | --- | --- | --- |
| 1 | 0.846154 | 0.884615 | 0.666667 | 0.730769 | 0.615385 |
| 3 | 0.980769 | 0.929487 | 0.858974 | 0.807692 | 0.846154 |
| 5 | 1.000000 | 0.929487 | 1.000000 | 0.836538 | 1.000000 |
| 10 | 1.000000 | 0.929487 | 1.000000 | 0.836538 | 1.000000 |

实际30次Qdrant查询无检索错误，4道无答案均返回非空候选；这不等于拒答正确。完整逐题原文/分数/排名/耗时见 [结果](../results/dense-v0-20261019-01/retrieval/retrieval.jsonl)，汇总见 [summary](../results/dense-v0-20261019-01/retrieval/summary.json)。D9评分子收据的model_calls=0只表示评分过程不调用模型，采集模型费用另在外层记录。

## 回答与Bad Cases

按八类首题预先选定001/010/002/003/009/013/017/020，均为pc-d7前缀、全部dev。8次回答使用已保存排名前5条，未重新Embedding或查询；7条通过JSON/Schema/引用编号校验，1条失败，结构校验成功率7/8不能当事实正确率。

- **pc-d7-003：生成返回非法JSON。** citation_numbers字段缺少开头双引号，本地json.loads在字符153拒绝，错误为Expecting property name enclosed in double quotes。原始content和消息已保存，未修补原输出、重试或改变Prompt；即使JSON模式也必须本地校验。此题计入8题失败分母。
- **pc-d7-003、pc-d7-012、pc-d8-034、pc-d8-039：@3证据尚不完整。** 到@5全部恢复覆盖；这是排名靠后/跨证据覆盖的基线案例，不是最大k的未召回。
- **pc-d7-017：返回insufficient_evidence。** 2026-10-20用户核对后确认此次拒答正确；不能据这一题声称4道无答案已全部验证或可靠拒答率100%。

原始回答、完整System/User、实际模型和usage、解析结果或错误见 [answers](../results/dense-v0-20261019-01/answers.jsonl)，案例索引见 [bad_cases](../results/dense-v0-20261019-01/bad_cases.jsonl)。[人工复核记录](../results/dense-v0-20261019-01/review.jsonl)于2026-10-20由用户明确确认：001/010/002/009/013/020事实及引用支持通过，003保留结构失败，017拒答正确。Codex据此拆分登记24项声明及原文支持位置，009“用户管理自己的样本”明确为合理推论，其余23项为直接支持；不把模型原始失败文本当成有效回答。

| 人工抽检口径 | 结果 | 分母与边界 |
| --- | --- | --- |
| 八题端到端通过 | 7/8，87.5% | 六条有效事实回答＋一次正确拒答；003结构失败保留在8题分母 |
| 有效回答声明支持 | 24/24 | 仅六条有效事实回答，含1项用户接受的合理推论；所有需证据声明均登记，未发现缺引用或不受支持项 |
| 抽样拒答正确 | 1/1 | 仅017，无事实声明，支持率N/A；不代表开发集全部4道无答案题 |

这是预选八类每类一题的人工核验，不能把局部声明支持24/24当作8/8成功或30题整体回答准确率。用户批量确认原话、复核时间、每项声明、引用编号与原文子串均在review保存。

## 耗时、缓存与费用

无自动重试。向量准备为冷缓存三批：16片段1168.07ms、smoke两问题246.14ms、其余28问题460.94ms；这包含本地缓存持久化，不等于纯供应商服务时间。先两题真实查询成功才继续剩余批次。

| 逐题阶段 | 有效样本 | 中位数ms | p95 ms | 口径 |
| --- | --- | --- | --- | --- |
| embedding | 30 | 0.725 | 0.845 | 已准备问题向量的本地缓存读取 |
| search | 30 | 5.090 | 6.735 | 真实Qdrant调用及清单范围准备 |
| total | 30 | 6.489 | 8.142 | 缓存读取、向量记录与本次检索，不含批量准备 |
| fusion/rerank | 0 | null | null | 本轮未执行 |

这是入库后顺序查询、没有显式预热的本机样本，不是冷模型端到端或并发吞吐结果。运行环境Python3.13.14/WSL2 x86_64；SDK和依赖版本见收据，具体CPU型号未采集。

实际3次Embedding＋8次DeepSeek。缓存准备miss46，入库及查询复用hit46；采集后另用禁止发请求的delegate核对30个问题向量全部命中，与归档逐值一致，新增模型调用0。后续对照应复用这些向量；完整向量/缓存保留被忽略的本地data，而不提交大数组。

DeepSeek八次（包含JSON失败）合计输入17277、输出687、总17964 tokens，服务端输入缓存命中0。按[本日官方峰值未命中价格](https://api-docs.deepseek.com/quick_start/pricing/)折算约US$0.0060075，仅是保守估算，不是已核对账单；实际低峰价格可能更低。Embedding usage和实际账单未采集，金额保持null；事前按总25k tokens及[智谱标准价](https://docs.bigmodel.cn/cn/guide/start/pricing)估算￥0.0125，不作为实付值。

## 归档与验收状态

完整本地目录`data/evals/dense-v0-20261019-01`的12份机器产物verify通过，review另行维护；公开快照见 [归档清单](../results/dense-v0-20261019-01/archive_manifest.json)。公开副本不含query_vectors大数组及重复的本地清单文件，但保留它们的SHA256与完整原运行manifest；可通过rankings/evaluation_config离线重评分，不应把精简副本当完整本地目录运行baseline verify。

2026-10-19开发提交 [12226d7b2c52b14e1d853288f508a8aff35cc2f6](https://github.com/yan-ye-dhy/project-copilot/commit/12226d7b2c52b14e1d853288f508a8aff35cc2f6)已由用户推送。2026-10-20用户本人通过完整基线核验、公开排名离线重评分、30向量缓存逐值复用及修改Prompt副本拒绝，新增模型调用0；随后确认完成ChatGPT学习并能解释主流程，八题人工复核已登记，D10阶段A/B均完成。本次收尾记录待用户提交，未开始D11或使用保留集选择配置。

status.json、run_manifest.json及archive_manifest.json仍保留采集时human_review=pending/day10_accepted=false，机器文件字节与原清单一致；这些是原运行快照。当前人工状态以独立review.jsonl及本报告为准，不为显示“完成”改写原始排名、回答或冻结配置。
