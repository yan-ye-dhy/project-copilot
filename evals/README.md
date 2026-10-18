# D9：离线检索评测器

本目录当前提供固定排名重放、原文证据映射和指标汇总；不连接模型或Qdrant，不运行聊天生成。默认样本明确为synthetic_fixture，数字只验证评测器数学与工程流程，不是Dense检索效果、冻结V0或保留集成绩。D8的40题、来源、划分及冻结文件保持原样。

## 文件与执行链

| 文件 | 职责 |
| --- | --- |
| `metrics.py` | 构建精确位置目录，计算文档/证据Recall、MRR及全部必需证据覆盖 |
| `run.py` | 冻结门禁、配置/排名校验、逐题重放和新目录结果保存 |
| `configs/d9_replay.json` | 4道开发题、k=1/3/5/10、750/150切分及数据指纹；是D9样例配置，不是D10冻结实验 |
| `fixtures/d9_rankings.jsonl` | Codex手工构造的排名、重复结果、空结果和模拟超时；没有真实请求 |
| `datasets/copilot_v0/` | D8冻结数据；语料、问题、标签、划分及正式清单分开 |

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
