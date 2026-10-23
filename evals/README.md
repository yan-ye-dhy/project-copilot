# 检索评测：离线评分、冻结基线与逐步对照

D9的`run.py`提供固定排名重放、原文证据映射和指标汇总，不连接模型或Qdrant。默认样本明确为synthetic_fixture，数字只验证评测器数学与工程流程。D10新增显式`--live`的`baseline.py`采集器，真实执行后再交D9评分；D8的40题、来源、划分及冻结文件保持原样。

## 文件与执行链

| 文件 | 职责 |
| --- | --- |
| `metrics.py` | 构建精确位置目录，计算文档/证据Recall、MRR及全部必需证据覆盖 |
| `run.py` | 冻结门禁、配置/排名校验、逐题重放和新目录结果保存 |
| `configs/d9_replay.json` | 4道开发题、k=1/3/5/10、750/150切分及数据指纹；是D9样例配置，不是D10冻结实验 |
| `fixtures/d9_rankings.jsonl` | Codex手工构造的排名、重复结果、空结果和模拟超时；没有真实请求 |
| `datasets/copilot_v0/` | D8冻结数据；语料、问题、标签、划分及正式清单分开 |
| `baseline_config.py`、`configs/dense_v0.json` | D10完整Prompt/Schema/模型参数、固定开发题与回答分层样本合同 |
| `cache.py` | 按供应商/模型/维度/float/文本指纹复用向量；损坏拒绝，不隐式重收费 |
| `baseline.py` | 离线计划、显式真实采集和机器产物核验；不自动确认人工回答质量 |

执行链为：main解析参数 → verify_freeze → 配置/问题选择 → D3 loader/splitter与chunk目录 → 校验全部排名输入 → evaluate_ranking → 汇总 → 新目录三份结果。引用原项目tests/fakes/rag.py的固定返回及依赖隔离思路，核心映射、指标与运行器自行实现，没有直接导入旧agent包。

## 配置与排名快照

配置完整保存数据指纹、split、question_ids、ks、splitter和ranking_origin。D9只接受dev；question_ids为明确子集，设null才表示全部30道开发题。所选题必须恰有一条快照，遗漏失败题、重复或额外ID均拒绝，不静默缩小分母。样例只选4题，不能写成30题开发集已全量检索。

排名JSONL每行字段如下：

| 字段 | 口径 |
| --- | --- |
| question_id / query | 冻结问题ID；追问使用已标注standalone_question，其他使用原question，须逐字相同 |
| hits | 有序RetrievedChunk，保存完整text、metadata、score；顺序就是原始排名，不按score重新排序 |
| timings_ms | embedding/search/fusion/rerank/total各阶段毫秒，未采集为null；不自动相加推导total |
| error | 成功为null，失败保存type和message且hits为空；完整保留在逐题结果中 |

快照支持synthetic_fixture及recorded_retrieval两种来源声明，后者只表示外部提供的离线排名，不保证已具备完整真实检索记录。D10仍需实际索引清单、模型/向量与缓存、检索配置、Prompt/Schema及运行环境，不能把当前回放收据当成真实基线采集已完成。

## 映射与指标口径

规则版本为exact-quote-contiguous-union-v1。先用配置中的D3切分器生成canonical chunks，每个chunk的完整文字必须在所属文档页原文中唯一出现，才登记Unicode字符左闭右开区间。当前5份语料默认生成16个唯一chunk；重复原文无法唯一定位时明确失败，不随意选择第一次出现。排名片段的文本和全部metadata须与目录一致，错误身份不被当作普通未命中。

gold仍绑定文档版本、页和原文证据，不改成chunk_id。单chunk或同文档版本/同页多个chunk按原文位置无缺口拼接，必须完整覆盖并逐字匹配整个gold quote；只重叠几个字符不算命中。证据的完成排名是首次完整覆盖它的最小排名前缀。此规则是保守的原文覆盖判断，不是模型语义裁判，也不证明最终答案事实正确。

| 指标 | 分母与排名 |
| --- | --- |
| 文档Recall@k | gold相关文档去重，统计前k个原始chunk覆盖的文档比例 |
| 文档MRR@k | 首个相关文档chunk的原排名倒数，未命中0；不压缩重复排名 |
| 证据Recall@k | 独立gold unit完整覆盖数除以gold unit数，重复chunk不增加覆盖 |
| 证据MRR@k | 首次完成任一gold unit的原排名倒数，组合到第4名才完整则为1/4 |
| all_evidence_covered | 每题全部必需gold均被覆盖；汇总为可回答题中的完整覆盖比例 |

这些指标对可回答题宏平均；检索失败保留并按空排名计0。无答案题各项记null，排除Recall/MRR分母；另外保存空/非空结果及检索失败数量。当前不生成答案，refusal_accuracy为null，不能把无答案题返回空片段当成正确拒答。

## 固定样例与手算

| 问题 | 固定原始排名 | 样例作用 |
| --- | --- | --- |
| pc-d7-001 | 无关Qdrant片段 → 启动配置正确片段 → 重复正确片段 | 正确证据首次排名2；重复项占第3名但不增加覆盖 |
| pc-d7-013 | 正确文档但没有完整gold的片段 → 完整含两个gold的片段 | 文档@1已命中，证据直到@2才完整，展示两种口径差异 |
| pc-d7-017 | 空结果，无检索错误 | 无答案单独统计，不进入召回分母 |
| pc-d8-023 | 空结果，simulated_timeout | 没有真实超时请求；可回答失败仍留在分母 |

所以@3的文档Recall=2/3、MRR=(1/2+1+0)/3=1/2；证据Recall=2/3、MRR=(1/2+1/2+0)/3=1/3，完整覆盖率2/3。分母是3道可回答题，而非4道总题或2道成功题。这些是人工构造输入的预期值，不代表项目检索质量。

更细的多文档gold、同文档多unit、跨chunk组合/缺口、重复排名和位置歧义，见 [指标固定算例](../tests/test_eval_metrics.py)；冻结/查询/失败分母与输出流程见 [运行器测试](../tests/test_eval_run.py)。

## 运行参考与输出

从项目根目录运行，结果目录必须是新目录，以下供学习后复现参考：

```bash
uv run --frozen python -m evals.run \
  --dataset evals/datasets/copilot_v0 \
  --config evals/configs/d9_replay.json \
  --rankings evals/fixtures/d9_rankings.jsonl \
  --output-dir data/evals/d9-manual-01
```

生成config.json、retrieval.jsonl、summary.json。保存run_id、来源声明、代码commit/dirty状态、配置/快照SHA256、数据指纹、切分配置与版本、依赖及基本平台信息、逐题原问题/实际query、原始排名/分数/片段/错误、指标和汇总。输出目录存在则拒绝覆盖，非法输入在创建目录前拒绝；运行数据在data下被Git忽略。

阶段耗时仅来自快照声明，median使用已有样本，p95采用nearest-rank即ceil(0.95*n)位置，均同时报告样本数。样例全部阶段耗时未采集，输出samples=0及median/p95=null。evaluation_ms另用单调时钟记录本地指标计算经过时间，不是CPU time或Embedding/Qdrant延迟；缓存、预热、索引及Prompt未参与本轮，分别如实标记未采集/不适用。

CLI成功退出0，输入/输出错误退出1，参数缺失退出2。D9不读取.env、不调用模型，也不支持聊天评分、Hybrid或Rerank。当前对重复文本采取拒绝歧义策略；复杂版面、语义等价改写与高级定位留后续有实际需求时处理，不改写已冻结gold来迁就评测器。

## D10：真实采集与基线合同

D10执行链：验证冻结数据与完整配置 → 检查Qdrant契约 → 全部16片段向量缓存 → 复用D4入库/成功清单 → 先2道开发题smoke → 其余28题 → D9按真实排名评分 → 可选8道分层回答 → 机器文件哈希封存。只使用dev，追问采用冻结standalone_question；独立集合为`pc_eval_dense_v0`，与应用默认集合分开。不会删除或重建旧集合，检索限制为本轮成功清单的5份文档及Dense版本，片段再次匹配canonical目录。

仅查看规模不读取.env或创建客户端：

```bash
uv run --frozen python -m evals.baseline plan \
  --dataset evals/datasets/copilot_v0 --config evals/configs/dense_v0.json
```

以下是取得本次费用授权后才执行的参考，不是自动运行指令；用户学习后的手动验证仍另行主动开启：

```bash
uv run --frozen python -m evals.baseline collect \
  --dataset evals/datasets/copilot_v0 --config evals/configs/dense_v0.json \
  --output-dir data/evals/dense-v0-01 --cache-dir data/evals/embedding-cache \
  --live --answers

uv run --frozen python -m evals.baseline verify \
  --dataset evals/datasets/copilot_v0 --output-dir data/evals/dense-v0-01
```

默认冷缓存最多3次Embedding请求（16片段、2问题、28问题）；不同batch_size按实际分批估计，缓存命中会减少调用。smoke失败保留局部记录并停止后续付费批次。所有客户端max_retries=0；再次生成回答仍会收费，不能因向量已缓存就盲目重跑整个命令。

`dense_v0.json`完整保存System/User模板及哈希、Zero-shot/无Few-shot示例、一次变量绑定、6000原文字符预算/整块跳过/去重/编号、Pydantic JSON Schema及哈希、模型/URL/temperature/max_tokens/JSON模式/关闭thinking、Embedding/distance与切分契约。运行前对照实际代码和设置，改变Prompt必须获得新配置身份；配置生成不表示真实V0已经采集。预选001/010/002/003/009/013/017/020八道dev题，每类首题，不根据模型成绩挑选。

| 产物 | 实际含义 |
| --- | --- |
| baseline_config/evaluation_config/index_manifest.json | 冻结合同及本轮成功文档/片段，保存完整副本 |
| embedding_batches.json、query_vectors.jsonl | 批量向量准备耗时/缓存统计；问题与原向量关联，后续复用不再收费 |
| rankings.jsonl、retrieval/三份D9结果 | 原始真实排名/分数/片段/逐题错误、指标及评分收据；评分器自身model_calls=0不代表采集未调用模型 |
| answers.jsonl | 可选8题，复用排名前5条，保存完整消息、供应商实际模型/usage、原始content、结构化回答或校验错误；不重复检索 |
| review.jsonl | 独立人工复核记录；采集时pending，用户确认后填写声明与引用支持、正确拒答及失败。缺引用事实也需入分母，纯拒答支持率N/A |
| status.json、manifest.json | 保留采集时完整性、调用及环境/哈希快照；其中pending/accepted=false不随人工复核改写，当前人工状态读取review |

逐题embedding时间是已准备查询向量的本地缓存读取时间，provider批量时间单列，不能摊成“独立请求延迟”。query总计包含本地读向量/保存/查询，非冷启动模型端到端；无显式预热，按执行顺序观察。未发生的fusion/rerank保持null，Embedding usage及账单未采集保持null；DeepSeek可用usage保留，估算金额与实际账单分开。

机器哈希清单排除可追加人工判断的review.jsonl；verify只核验原采集产物，没有人工审核就不填支持率或拒答正确率。status/run_manifest/archive_manifest中的pending/accepted=false保留采集时快照；后续人工确认只更新独立review与报告，不能修改原始回答、排名或Prompt。2026-10-20八题人工复核已完成，结果与分母见 [V0报告](reports/DENSE_V0.md)。完整向量与缓存只放被忽略的data，公开时仅挑选自建非敏感原文及结果，保留归档指纹。集合/向量缓存均为顺序单进程实验用途；不扩展为业务并发索引服务。

## D11：切分候选的离线边界检查

[chunk_size500.json](configs/chunk_size500.json)绑定D10归档与配置指纹，唯一变量为chunk_size750→500，overlap150及默认分隔符不变。它是候选描述，不是可交给D10采集器的正式基线配置；不能放宽D10校验或覆盖V0索引来运行候选。

[chunking.py](chunking.py)的只读运行参考（无需密钥、Docker或模型费用）：

```bash
uv run --frozen python -m evals.chunking \
  --dataset evals/datasets/copilot_v0 \
  --baseline-dir evals/results/dense-v0-20261019-01 \
  --candidate evals/configs/chunk_size500.json
```

调用链为main→inspect_chunking→核验归档输入/完整V0合同及D8冻结→限制关注题为真实dev检索Bad Case→同源语料两次build_chunk_catalog→按同页原文位置列交集/单块完整包含→stdout JSON。程序不写数据、不读取.env、不创建客户端；可重定向到被忽略的data目录观察输出，正式 [检查快照](results/chunk-size500-preflight/inspection.json)由Codex本日离线生成。

输出cases中的gold沿用原文版本/页/字符位置，intersecting_chunks含原文、实际区间及ID，single_chunk_covering_ids只表示某块包含完整quote；多块联合覆盖继续沿用D9规则，不能用交集代替完整证据。baseline_evidence_completion_ranks来自封存的真实V0排名；candidate没有排名，retrieval_evaluation/answer_evaluation均为null，model_calls/qdrant_requests均为0。冻结完整性校验会读取全量文件核对指纹，但候选分析只消费这四道dev标签，不做保留集效果分析。

实际16→27片段、累计片段字符10330→10389。较小chunk可能减少混合主题，也可能增加碎片、重复及工具开销；overlap150不是每对相邻段必然重复150字。假设、边界限制、参考来源与D12真实对照前置条件见 [候选报告](reports/CHUNK_SIZE500_PREFLIGHT.md)。本日不生成候选向量/排名，不据此认定优化收益。

## D12：原向量复用与真实切分对照

[chunking_run.py](chunking_run.py)提供plan/collect/verify；[comparison.py](comparison.py)读取两份D9评分记录，输出候选减基线的成对变化。D11描述文件保持原样，运行时结合完整V0合同生成 [experiment_config.json](results/dense-v1-size500-20261021-01/experiment_config.json)，保留Prompt/Schema/生成参数和Embedding合同，只更改size及对应候选/索引身份。D10采集器继续只接受原固定基线，不放宽它的校验。

plan只读冻结语料、V0公开/私有归档及向量缓存，不读.env或创建客户端：

```bash
uv run --frozen python -m evals.chunking_run plan \
  --dataset evals/datasets/copilot_v0 \
  --baseline-dir evals/results/dense-v0-20261019-01 \
  --baseline-private-dir data/evals/dense-v0-20261019-01 \
  --candidate evals/configs/chunk_size500.json \
  --cache-dir data/evals/embedding-cache
```

公开归档哈希→私有manifest→原query_vectors的哈希链须一致，30个dev题的ID/顺序/query/Embedding合同/2048维有限数值严格检查；历史运行源码漂移、损坏缓存、原文gold不能完整映射均在请求前拒绝。问题只读取原向量，缺失不会悄悄补做Embedding。当前费用计划取决于实际缓存，本日初始1命中/26缺失；实调后再plan会显示暖缓存，不能用它改写原调用数。

collect仅在`--live`且用户授权新增文本后运行，`--max-new-texts`默认0；原V0须已经存在且16点/原文/payload完全匹配，候选须为全新`pc_eval_dense_v1_size500`集合。缺失V0不自动创建、既有候选不覆盖；结果目录也拒绝覆盖。先一次文档向量prefill，入库复用缓存；前两题两侧smoke成功才继续，其后错误仍留在分母。每题两侧使用完全相同的已保存向量，先查询哪侧交替；无显式预热、未生成回答。

本轮已在授权下执行一次采集，**无需重跑collect**。公开20份文件含19份封存产物，不含问题向量或密钥，原字节同本地输出；完整性检查可离线运行：

```bash
uv run --frozen python -m evals.chunking_run verify \
  --dataset evals/datasets/copilot_v0 \
  --output-dir evals/results/dense-v1-size500-20261021-01
```

| 产物 | 含义 |
| --- | --- |
| experiment_config.json、两份evaluation_config.json | 完整父合同/候选合同、唯一变量、历史查询向量SHA与各自D9配置 |
| index_manifest.json、index_manifests/ | 候选成功清单；原V0只读，不修改原集合或归档 |
| embedding_batches.json | 初始缓存、prefill后统计、离线准备/批向量耗时，区别于入库后的重复缓存命中 |
| baseline_rankings/candidate_rankings.jsonl、两份retrieval目录 | 本轮60次真实查询、排名/原文/分数/错误、D9逐题及聚合指标 |
| gold_mapping.json、case_details.jsonl | 全32个dev gold原文位置→两套chunk映射，以及逐题两侧原始证据/指标，供用户核验 |
| comparison.json | 同轮26可答题各k指标差值、改善/退化/不变/mixed、分类Bad Case、成对耗时；4无答案另计 |
| historical_v0_recheck.json | 历史V0与本轮V0的指标复核；它的跨日耗时差不当作受控延迟结果 |
| status.json、manifest.json | 原采集状态、实际调用、21份源码哈希/依赖/执行顺序、19份文件指纹；manual_review=pending/accepted=false保留采集快照 |

比较器可记录mixed，防止某项上升掩盖另一项下降；错误转移与无答案单列。2026-10-22用户已亲自verify、双侧重评分、核对012改善/003退化的原文/排名，并验证副本只追加换行仍被拒绝，另确认ChatGPT学习理解；人工记录见报告，不改写机器pending快照。代表案例012完成rank4→1，003完成rank4→6；Recall@5由1降为24/26，当前默认保持750/150。确认范围是两条代表原文映射，全部32段映射的程序检查与此分开。详细数值、人工记录、时间与费用局限见 [真实对照报告](reports/CHUNKING_V1.md)；保留集仍等D17。

## D13：为后续三路对照准备BM25

[词项模块](../src/project_copilot/rag/bm25.py)与 [独立CLI](../scripts/bm25_index.py)用jieba统一中文/技术词，再交rank-bm25的BM25L评分。固定参数1.5/0.75/0.5，无dev调参；与现有750/150的成功清单和原chunk ID完全对应。构建只读get_collection/retrieve，JSON快照及临时输出在忽略目录data/bm25、data/auto-d13；本地查询不需要模型、Qdrant或原问题向量。

快照携带完整成功清单、原文/metadata、分词规则/词典SHA、Unicode/rank-bm25/NumPy版本和内容指纹；加载时核对当前清单，拒绝缺失/陈旧/变更文件。JSON加载重建词频，不保存可执行pickle；清单/存储变化后需要显式新建快照，没有实现自动同步。

[自动收据](../docs/evidence/D13_BM25_SMOKE.json)记录真实V0的5文档/16片段对齐和四条固定查询，仅作最小链路验收。D13不新增完整评测run、不算Recall/MRR收益，也不改D9配置使其冒充BM25评测器。D14再扩展三路采集与RRF，公开各路候选数、最终k、冷构建/查询耗时与Bad Case，沿用D8划分和D10 Prompt冻结，不使用保留集选分词或参数。

2026-10-22用户已亲自完成D13的8个手动环节并确认ChatGPT学习理解：独立快照在data/manual-d13，四条查询分别命中1/3/2/0条，清单副本仅变更spec.splitter_version后旧快照按预期拒绝、退出1。Codex只读确认正式清单原字节、快照指纹和原文/metadata保持，新增模型调用0；本次人工记录见 [D13报告](../docs/reports/DAY_13.md)，自动收据不改写，三路效果仍待D14明确开启。

## D14：固定候选的Dense/BM25/Hybrid对照

[hybrid_run.py](hybrid_run.py)提供plan/collect/verify；[配置](configs/hybrid_v2.json)绑定原V0完整合同，固定750/150、每路候选10、输出深度10、最终5和RRF c60。沿用同一个D9评分配置与原文gold映射，算法/预算身份另存experiment_config，不修改旧评测器或拿新Prompt覆盖V0。

```bash
uv run --frozen python -m evals.hybrid_run plan \
  --dataset evals/datasets/copilot_v0 \
  --baseline-dir evals/results/dense-v0-20261019-01 \
  --baseline-private-dir data/evals/dense-v0-20261019-01 \
  --config evals/configs/hybrid_v2.json

uv run --frozen python -m evals.hybrid_run verify \
  --dataset evals/datasets/copilot_v0 \
  --output-dir evals/results/hybrid-v2-20261022-01
```

plan完全离线并验证原向量哈希链；collect沿用四项输入并提供全新的`--output-dir`，连接本机Qdrant但不调用模型、不写集合。每题只查一次Dense和一次BM25，保存各路原始score/排序后融合，额外候选并集实际10～14。三路各30题，固定评价k=1/3/5/10；错误留分母，无答案独立，不做拒答评测。

[公开V2归档](results/hybrid-v2-20261022-01/manifest.json)含23封存产物＋manifest：三路完整原文排名/评分、三组比较、candidate_sources中的来源名次/score、case_details中的gold/指标及结果文件引用、历史V0重查、完整父合同/索引/BM25快照和机器状态。原问题向量仍只保存在忽略目录，公开配置以SHA绑定。verify不需要私有向量或当前Prompt生成逻辑；机器pending状态不代表用户已完成人工验收。

证据Recall@5均1.0，MRR@5依次0.8365/0.9327/0.8974；Hybrid的Recall@3为1.0，但BM25的MRR@5更高，详见 [V2实验报告](reports/HYBRID_V2.md)。准备/字典构建时间单列，逐题embedding/rerank为null；Hybrid search为两路共享搜索之和、total再加fusion，属于顺序合计而非独立请求计时。2026-10-23用户亲自完成归档verify、三路离线重评分、012计算反馈及副本拒绝，并确认学习理解；90条重评分记录与原结果一致，正式归档及机器pending快照保留。D17再作方案选择，不据这次自建小样本声称普遍提升，D15等待明确开启。

## D15：单题重排适配验收

入口为 [rerank_smoke.py](../scripts/rerank_smoke.py)，使用D14公开归档的固定dev题012及同一10个Hybrid候选；先verify归档，不重新查询Qdrant或问题Embedding。无`--local`时在读取评测输入或加载模型之前退出2；显式启用后只读本地固定快照，评分首/热各一次以区分加载与复用，不将异常重复执行当重试。

```bash
uv sync --frozen --group rerank-gpu
uv run --frozen --group rerank-gpu python scripts/rerank_smoke.py --local --device cuda --dtype float16
```

用户已选择GPU验证并接受额外CUDA依赖与准备时间，现已完成RTX 4060 Laptop上的CUDA float16真实执行。`rerank-gpu`使用官方CUDA 13.0源，与CPU组`rerank`互斥；同一固定模型已缓存，准备方式及CPU运行参考见 [根README](../README.md#d15本地cross-encoder最小重排)。保存10个原分数/名次/原文hash、20个raw logits、每遍选中5块的完整原文和metadata、截断诊断、模型版本/参数和分段耗时；输出不含D16全量指标。

[CPU历史收据](../docs/evidence/D15_RERANK_SMOKE.json)保留原字节，其中另有合成2120→512 tokens与超长query拒绝诊断。[GPU验收收据](../docs/evidence/D15_GPU_RERANK_SMOKE.json)另记实际全部浮点模型参数`cuda:0`/`float16`、Torch 2.14.1+cu130、CUDA编译版本13.0、设备capability、同步计时及当前进程Torch分配器allocated/reserved显存峰值。新增依赖组后的`pyproject.toml`和`uv.lock`使用新的源码SHA，其余25份已登记源码保持；CPU收据继续保留运行当时的历史SHA，不改标为GPU结果。

GPU首/热模型推断1112.377/160.087ms，整个rerank为12007.819/174.468ms；首模型加载10856.167ms，不是冷盘或完整进程启动。每遍分配器峰值allocated/reserved为1166032896/1189085184字节，非整卡峰值；首遍包括模型加载，热遍包括常驻模型基线。GPU新增2次成功评分、20个pair，原CPU另有3次成功评分、21个pair及1次超长query推断前拒绝，合计5次成功评分、41个pair；外部模型API/Qdrant请求均0。GPU选中原候选名次`[3,4,2,1,5]`与CPU相同，首热GPU分数相同，但相对CPU的最大logit差约0.087717。

CPU float32与GPU float16同时改变硬件和精度，首/热两次属于单题观察，不据单次差值宣称单因素加速、P95或生产吞吐。

本轮只验证适配合同，012的gold从3到1不能冒充开发集MRR改善；模型分数不能当置信度。未改D8冻结输入、原V0/V1/V2机器产物或默认750/150和Dense API。D16将使用固定候选作成对实验，仍等待本日A/B完成及用户明确开启。
