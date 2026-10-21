# 【Day 12 开发报告】

日期：2026-10-21。当前：实现、离线自动验证及一次授权真实实验完成；**阶段A待用户提交/推送，阶段B待ChatGPT学习与主动手动验证**。默认切分保持750/150，无新增依赖，未开启D13。

## 1. 今日开发目标

对D11的500字符候选做一组真实V1对照：沿用原30个问题向量、同一语料/gold/模型/overlap/分隔符/k及Prompt合同，新建候选索引，在同轮重查V0并记录改善、退化、原文与耗时。保留集不参与选择，没有第二轮参数搜索或回答生成。

## 2. 新增和修改的文件

**新增：**

- `evals/chunking_run.py`：plan/显式live/verify、历史归档/向量合同与预算门禁、独立索引、交错采集及封存。
- `evals/comparison.py`：两份D9评分的严格身份检查、逐题/分类/指标差值与成对耗时。
- `tests/test_chunking_run.py`：19项内存Qdrant/合成向量测试。
- `tests/test_eval_comparison.py`：20项指标、错误、无答案、配置与配对统计测试。
- `evals/results/dense-v1-size500-20261021-01/`：20份无向量公开文件，19份封存产物＋manifest，完整合同、原始排名/评分/证据和本轮V0重查均保留。
- `evals/reports/CHUNKING_V1.md`：真实结果、两类代表原文、费用/时间与局限。
- 本报告。

**修改：** 根README、evals/README、开发计划第10节、开发日志、学习与面试笔记，共6份关联文档。业务src、D8冻结文件、D10/D11原配置与机器产物、依赖及锁文件不变。

## 3. 核心实现内容

1. `prepare_experiment`绑定V0公开归档→私有manifest→query_vectors，校验旧源码、确切30题/query/原Embedding合同/2048维有限数值，复用原值而非重新请求问题向量。
2. 全32段dev gold按同页原文连续并集在付费前检查；候选独立DenseIndexSpec，V0须已存在且16点及原文payload匹配，候选/结果拒绝覆盖。
3. 一次文档向量prefill，D4入库复用缓存；2题成对smoke后全dev，逐题两侧轮换先后顺序，错误按空排名留在分母；D9双侧评分保持26可答/4无答案口径。
4. `compare_retrieval`记录各k的候选减基线、改善/退化/不变/mixed、分类Bad Case、错误转移与成对耗时；历史V0重查另存，原始配置/排名不改。
5. 完整Prompt/Schema/生成参数保存在派生合同中，本轮无聊天；19份必需文件封存，用户后续人工状态与原机器pending快照区分。

## 4. 关键设计决策

D10配置校验保持固定V0，D12在独立模块派生候选；复用D3/D4/D9/D10组件，新实验编排与统计自主设计，不改业务默认或新增依赖。新集合/index指纹用于区分切分身份，不是第二个检索算法因素。

实测证据Recall@1为0.6667→0.7692，但@5由1降至0.9231，26道可答题6改善/5退化/15不变。012证据完成rank4→1，003块虽由682缩至385字符却rank4→6；保持原5块回答预算与750默认，保留失败/取舍，不只挑MRR上升。详细数值见 [实验报告](../../evals/reports/CHUNKING_V1.md)。

## 5. 自动测试结果

执行者均Codex，工作目录新项目；离线测试使用合成向量/内存Qdrant，真实实验另列。用户学习/手测尚未执行，不能据此登记阶段B。

| 实际命令/检查 | 实际结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_chunking_run.py tests/test_eval_comparison.py tests/test_chunking_inspection.py tests/test_chunking_boundaries.py tests/test_baseline_config.py tests/test_baseline.py tests/test_eval_cache.py tests/test_eval_metrics.py tests/test_eval_run.py tests/test_eval_freeze.py tests/test_eval_dataset.py tests/test_documents.py` | **333 passed，9.74秒**；D12新增39项＋相关旧294项 |
| `.venv/bin/python -m evals.chunking_run plan --dataset evals/datasets/copilot_v0 --baseline-dir evals/results/dense-v0-20261019-01 --baseline-private-dir data/evals/dense-v0-20261019-01 --candidate evals/configs/chunk_size500.json --cache-dir data/evals/embedding-cache` | 退出0，初始1缓存命中/26缺失/10129字符/17443 UTF-8字节；预计1次Embedding/60本地查询，复用30原向量；plan模型调用0 |
| `.venv/bin/python -m evals.chunking_run collect --dataset evals/datasets/copilot_v0 --baseline-dir evals/results/dense-v0-20261019-01 --baseline-private-dir data/evals/dense-v0-20261019-01 --candidate evals/configs/chunk_size500.json --cache-dir data/evals/embedding-cache --output-dir data/evals/dense-v1-size500-20261021-01 --live --max-new-texts 26` | 退出0；实际1次Embedding、26新文本，0问题Embedding/聊天，60次真实检索，两侧错误0，无自动重试 |
| `.venv/bin/python -m evals.chunking_run verify --dataset evals/datasets/copilot_v0 --output-dir data/evals/dense-v1-size500-20261021-01`及同名公开目录 | 均verified，19份封存产物；manual_review=pending、day12_accepted=false，人工状态尚待学习后验证 |

初期Docker CLI报I/O错误，用户恢复Desktop/WSL；沙箱socket权限受限，经正常环境审核启动原Qdrant，实际服务1.19.2、V0点16、应用点2、候选不存在后才实调。未读取旧项目.env，白名单检查只输出新密钥是否配置，未输出密钥。离线开发中补齐旧源码漂移和全部gold映射的付费前校验、清单漏项及预算/原集合边界，最终333项通过后执行本轮真实调用。

用户已明确同意本轮1次Embedding及60次本地检索；实际未超范围。Embedding初始hit1/miss26/api1，入库后累计hit28/miss26/api1，问题原向量全部同SHA来源复用。按本日[官方价格](https://docs.bigmodel.cn/cn/guide/models/embedding/embedding-3)0.5元/百万tokens，以3万输入tokens预算约¥0.015；实际usage/账单未采集，保持null。未将累计缓存命中次数、字符数或预算当作实际Token/实付。

真实证据Recall@1/3/5/10为V0的0.666667/0.858974/1/1，候选为0.769231/0.897436/0.923077/1；MRR@5为0.836538→0.858974。6改善/5退化/15不变，4无答案两侧均有命中但未测拒答。32段gold程序映射完整，用户人工代表案例核验仍pending。历史V0逐题指标重查一致；同轮search中位4.380→4.362ms，成对差约−0.028ms，单轮不能声称提速，文档Embedding批1200.433ms与准备41.777ms单列。

公开20文件与本地结果原字节相同，不含向量/密钥；独立只读结果审查确认调用数、32段gold、指标及012/003原文排名。当前无未解决执行失败；效果退化如实保留，云端别名跨日、小型语料、未生成回答和未人工核验均为结论限制。

收尾检查通过32个候选文件（6修改/26新增）、4份Python AST、全部新JSON/JSONL及源码/文档空白；机器快照保持原始字节，D4清单无结尾换行也不为格式化而改写。8份Markdown围栏、51张表、190处本地链接/13处锚点和固定8项报告通过。V0公开12产物、D8冻结10文件、D11候选/快照与原值一致；V1的19产物/21源码哈希及公私20文件字节一致，常见凭据模式检查通过，.env/data/缓存仍被忽略。计划仅第10节变化，无业务/依赖diff；git diff --check无输出、暂存区为空，独立只读事实复核通过。

## 6. GitHub 提交信息

- 分支main跟踪origin/main；远端`https://github.com/yan-ye-dhy/project-copilot.git`。
- 开发起点为用户已推送的D11收尾 [559190f13583765dd3ffbec59c435845fe198737](https://github.com/yan-ye-dhy/project-copilot/commit/559190f13583765dd3ffbec59c435845fe198737)，开始时main/origin/main一致、工作区干净。
- D12：**待用户亲自add/commit/push，暂无已同步的GitHub提交链接**。运行快照如实保留当时commit559190f/dirty=true及21份源码SHA，不预填新提交或成功状态。

## 7. 后续学习提示

交给ChatGPT结合本日报、[学习提纲](../STUDY_NOTES.md#d12从切分假设到真实成对实验)、[完整合同](../../evals/results/dense-v1-size500-20261021-01/experiment_config.json)及实际commit带读：

1. prepare→归档/原向量/gold门禁→缓存入库→交错检索→两次评分→成对比较→封存的完整调用链。
2. 为什么原问题向量须逐值复用，归档哈希链与30题严格身份检查怎样阻止隐式变化。
3. 初始缓存、批请求、入库再次命中的区别；索引/切分版本与新集合关系。
4. 012改善和003/007退化的原文对照，为什么MRR上升不能抵消top5覆盖下降。
5. 失败分母/无答案/mixed口径、交错计时与云端模型别名/未采集成本的边界。

## 8. 下一步计划

先由用户提交并推送D12，再交ChatGPT学习；待本人主动要求开始D12手动验证后，离线核验公开归档/重评分，并亲自对照至少一条改善与退化的原文gold/排名。无需重新收费采集，未确认前不登记阶段B。

本日A/B完成且用户明确开启D13后，再以当前750/150起点进入中文BM25最小检索；D12不实现后续算法或修改Prompt，保留集等D17。
