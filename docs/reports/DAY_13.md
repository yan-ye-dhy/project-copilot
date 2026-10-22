# 【Day 13 开发报告】

开发与学习/手动验证日期：2026-10-22。**D13阶段A/B均完成**，开发提交d4c5bfd已由用户推送；本人八项手动验证及ChatGPT学习理解均已确认，本次七份收尾文档待用户提交。本日只完成中文BM25最小检索，未开启D14，没有模型调用或Qdrant写入，默认切分保持750/150。

## 1. 今日开发目标

复用现有Dense成功清单与片段，建立独立的中文词项索引：文档和问题使用同一分词规则，保留中文、错误码、API路径和参数名的边界，检索结果沿用原chunk_id及来源元数据。先验证最小词项链路，不在本日实现RRF或运行三路效果对照。

## 2. 新增和修改的文件

**新增：**

- `src/project_copilot/rag/bm25.py`：统一分词、BM25L、本地JSON快照、完成清单对齐及只读Qdrant片段读取。
- `scripts/bm25_index.py`：tokenize/build/search三个命令、显式清单、资源释放与脱敏JSON错误。
- `tests/test_bm25.py`：54项分词、评分、身份、快照及Qdrant读取合同测试。
- `tests/test_bm25_cli.py`：15项CLI测试，含真实本地快照与内存Qdrant。
- `docs/evidence/D13_BM25_SMOKE.json`：Codex真实Qdrant只读验收记录，不含完整原文、向量或密钥。
- 本报告。

**修改：** `pyproject.toml`、`uv.lock`，新增jieba 0.42.1和rank-bm25 0.2.2；根README、evals/README、开发计划第10节、开发日志、学习笔记和面试笔记。没有升级既有依赖或修改Dense问答API、旧切分器、原成功清单及历史实验产物。

本地快照`data/bm25/d13-v0.json`及运行输出留在被忽略的data目录，不随本日提交。

本次学习/手动验证收尾只补根README、evals/README、开发计划第10节、开发日志、学习/面试笔记及本报告，共七份Markdown；用户结果留在被忽略的`data/manual-d13/`，业务源码、依赖、自动验收收据和历史产物不改。

## 3. 核心实现内容

1. `ChineseTokenizer`采用NFKC/casefold规范化，中文使用jieba搜索分词、关闭HMM；技术标识整体保留，包括`embedding-3`、`/documents/upload`、`--env-file`及下划线名称。文档与查询共用规则，记录词典SHA、jieba及Unicode版本。
2. `read_completed_chunks`仅调用Qdrant的get_collection/retrieve，按完成清单中的精确ID读取payload，不取向量。验证Dense集合配置、完整片段集合、原文对应的稳定chunk_id及文档/切分版本，拒绝漏项、重复或范围外数据。
3. `BM25Index`使用BM25L固定参数`k1=1.5/b=0.75/delta=0.5`，返回真实原文/元数据；无匹配词和纯标点返回空结果，并列按chunk_id稳定排序，空白问题及非法top_k被拒绝。
4. JSON快照包含完整清单、原文片段、分词与算法参数、rank-bm25/NumPy版本及内容指纹；拒绝覆盖已有文件，清单或依赖漂移须显式重建，不加载pickle。search仅需本地快照和显式清单，不读取.env或构造模型客户端。

## 4. 关键设计决策

采用成熟的[rank_bm25](https://github.com/dorianbrown/rank_bm25)计算词项评分，[jieba](https://github.com/fxsjy/jieba)负责中文切词，项目自主实现身份约束、技术词边界、快照及CLI。选择BM25L处理当前小语料，参数本日固定，尚未通过开发集选优；BM25分数不能作为Dense分数或答案置信度，后续融合使用排名。

原项目`src/agent/utils/vdb.py:13`只作只读接口参考，其全局FastEmbed初始化方式未复制；新实现复用本项目D3/D4的文档和成功清单契约，不直接导入旧包。D12候选未取代750/150，因此本日以原V0的同一组5文档/16片段起步，避免同时改变切分与检索算法。

快照不监听上传或Qdrant变化；使用时绑定显式成功清单，清单改变后拒绝沿用旧快照。当前验证的是建立/查询独立词项索引，上传自动同步和部分失败恢复仍待D18，不扩大本日范围。

## 5. 自动测试结果

以下首表为2026-10-22开发期自动验证，执行者Codex、工作目录新项目，不代表用户手动测试；同日用户本人的学习后验证单独记录在后表。

| 实际命令/检查 | 实际结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_bm25.py tests/test_bm25_cli.py tests/test_dense_pipeline.py tests/test_qdrant.py tests/test_documents.py tests/test_chunking_run.py tests/test_eval_comparison.py tests/test_baseline_config.py tests/test_eval_freeze.py tests/test_eval_dataset.py` | **297 passed，12.56秒**；本日新增69项（核心54＋CLI 15），其余为相关回归；测试使用本地快照/内存Qdrant，无外部模型 |
| 启动既有Docker Qdrant并检查`http://127.0.0.1:6433/readyz` | 服务就绪，复用原集合，没有创建或修改集合 |
| `.venv/bin/python scripts/bm25_index.py build --manifest evals/results/dense-v0-20261019-01/index_manifest.json --output data/bm25/d13-v0.json` | 退出0；只读原V0的5文档/16片段，保存本地快照；BM25L参数1.5/0.75/0.5，NumPy 2.5.4版本写入快照 |
| 四次`.venv/bin/python scripts/bm25_index.py search`，共同使用上述`--manifest`及`--index data/bm25/d13-v0.json` | 查询路径、选项、中文和不存在的词，结果见下表；search全程本地，无Qdrant或模型请求 |
| 再次只读Qdrant集合及精确16个成功ID，与本地快照逐条比对 | 点数仍16，payload与快照一致；本日Qdrant操作仅get_collection/retrieve，向量读取0、写入0、模型调用0 |
| `uv lock --check --offline --cache-dir /tmp/project-copilot-uv-cache` | 退出0，64包；锁文件与项目依赖一致 |
| `.venv/bin/python -m evals.freeze_dataset --dataset evals/datasets/copilot_v0 --verify` | verified，原数据指纹不变，dev30/holdout10 |
| `.venv/bin/python -m evals.baseline verify --dataset evals/datasets/copilot_v0 --output-dir data/evals/dense-v0-20261019-01` | verified，12份基线文件；pending为原机器快照，D10人工完成记录不受影响 |
| `.venv/bin/python -m evals.chunking_run verify --dataset evals/datasets/copilot_v0 --output-dir evals/results/dense-v1-size500-20261021-01` | verified，19份V1文件；保留原机器状态，不改D12人工完成结论 |

| 实际查询 | 命中数 | 首条来源/结果 |
| --- | ---: | --- |
| `/documents/upload` | 1 | `upload_troubleshooting.md` |
| `--env-file` | 2 | `compose_environment.md` |
| `上传文件不能为空` | 3 | `upload_troubleshooting.md` |
| `unknownxyz987` | 0 | 空结果，不返回无关片段凑足k |

测试覆盖小语料常见词、技术词版本区分、中文混合标识、并列稳定性、空清单/空词项、快照篡改及陈旧清单、Qdrant配置/payload不匹配和CLI错误脱敏。开发期发现`b=1`与部分空词项片段组合会产生非法评分，已在构建时明确拒绝并补测；默认b=0.75保持不变。QdrantClient本身不支持上下文协议，CLI使用closing确保异常时也释放资源，最终相关测试均通过。

真实记录见 [D13自动验收](../evidence/D13_BM25_SMOKE.json)。原21份冻结运行源码SHA及V1的19份封存产物未改；没有重新Embedding、生成回答、使用保留集或改动Prompt。四个固定样例只证明词项链路与边界可用，没有全dev的Recall/MRR、三路对照、回答质量或统计提速结论；这些指标本日未采集。

开发交接时检查通过7份Markdown空白/围栏、51张表格列数、195处本地链接/11锚点和固定8节报告；计划第10节外及历史日志原文保持。锁文件只增加两包，旧包版本不变；收据6份源码/依赖哈希一致，`.env`、快照和自动运行输出均被忽略。当时`git diff --check`无输出、暂存区为空，14个开发文件交用户审核提交；独立只读复审发现的进度标题/历史段落归属已修正。

同日用户主动开启D13手动验证，以下八项由本人操作。Codex只读核对实际输出与本地文件，没有代用户运行功能测试、启动服务、运行pytest或请求模型。

| 执行者与实际操作 | 实际结果 |
| --- | --- |
| 用户：tokenize `上传文件 HTTP_422 /documents/upload LOAD_SETTINGS embedding-3 embedding-4 --env-file`并查看退出码 | 退出0、model_calls=0；tokens为上传、文件、http_422、/documents/upload、load_settings、embedding-3、embedding-4、--env-file，技术词整体保留且大小写统一 |
| 用户：启动既有Docker Qdrant并curl `/readyz` | HTTP200，`all shards are ready`；复用现有集合，没有模型请求 |
| 用户：build，使用原V0的`index_manifest.json`，`--output data/manual-d13/index.json`，stdout保存至`data/manual-d13/build.json` | 退出0；5文档/16片段；Codex只读核对全部chunk_id及快照指纹，与开发期自动验收记录一致 |
| 用户：search `/documents/upload`，top_k=3，输出`data/manual-d13/search-upload.json` | 退出0，实际命中1条而非补齐3条；来源upload_troubleshooting.md，原文及metadata与快照一致 |
| 用户：search `上传文件不能为空`，top_k=3，输出`data/manual-d13/search-chinese.json` | 退出0，tokens为上传/文件/不能/为/空，命中3条；首条为upload_troubleshooting.md且含实际空文件提示，全部命中原文及metadata与快照一致 |
| 用户：将search全部选项放在`-- '--env-file'`之前，top_k=3，输出`data/manual-d13/search-compose.json` | 退出0，选项名作为完整查询词，命中2条且均来自compose_environment.md；没有被argparse当成脚本选项 |
| 用户：search `unknownxyz987`，top_k=3，输出`data/manual-d13/search-miss.json` | 退出0、status=ok，hit_count=0、hits=[]；没有返回任意片段凑数 |
| 用户：复制正式清单到`data/manual-d13/manifest-changed.json`，仅给spec.splitter_version追加`-manual-test`，以副本清单及原快照search `/documents/upload`，stderr保存至`data/manual-d13/stale-manifest-error.json` | 退出1，脱敏JSON错误、model_calls=0；Codex只读确认副本仅该字段变化，正式清单与HEAD及原收据SHA一致，快照仍为原build指纹 |

用户明确确认“已学习，能够解释”，涵盖成功清单→Qdrant片段→统一分词→BM25快照→本地查询的主流程，以及清单变更为何拒绝旧快照。结合已同步开发与八项实际操作，阶段A/B均完成；手动范围不包含全角NFKC或空白问题测试，不把开发期对应自动用例登记为用户操作。

本轮手动验证与收尾没有模型调用、Qdrant写入、RRF、回答生成或全dev/保留集效果评测，仍只证明词项链路及本次边界可用。Codex只读确认D13源码/依赖与自动收据哈希一致，正式清单及快照未改；自动验收收据保留原始执行者、base_commit和dirty状态，不追写成用户验收。本次只更新上述七份Markdown，未重跑pytest或功能验收。

收尾文档检查通过7份Markdown空白/围栏、52张表列数、197处本地链接/11锚点和固定8节报告；计划第10节外、历史日志/笔记章节保持。D13收据6份源码/依赖、V1原21份源码及19份机器产物SHA均一致，独立只读事实复审通过；git diff --check无输出，暂存区为空，无未忽略新增文件。

## 6. GitHub 提交信息

- 分支main跟踪origin/main；远端`https://github.com/yan-ye-dhy/project-copilot.git`。
- 开始时本地/跟踪分支一致，工作区干净；起点为用户已推送的D12收尾 [b364f0e9c84f8a745865121c81cfaf1c3c257e73](https://github.com/yan-ye-dhy/project-copilot/commit/b364f0e9c84f8a745865121c81cfaf1c3c257e73)。
- D13开发提交：[d4c5bfd1ab58325dd34db16fe92900f45c9605ef](https://github.com/yan-ye-dhy/project-copilot/commit/d4c5bfd1ab58325dd34db16fe92900f45c9605ef)，已由用户提交并推送；开始手动验证时main/origin/main一致、工作区干净，阶段A完成。
- 本次七份学习/手动验证收尾文档**待用户亲自add、commit和push**，不预填新commit。Codex没有执行Git写操作；真实验收记录保留原base_commit及dirty=true的运行状态。

## 7. 后续学习提示

用户已确认通过ChatGPT学完并能解释主流程；以下保留为结合实际代码与提交复习的重点：

1. build→完成清单→Qdrant精确片段→统一分词→BM25L→JSON快照，以及search→快照校验→问题分词→评分→真实证据的完整调用链。
2. 中文搜索分词、技术标识边界、NFKC/casefold与HMM关闭的取舍，为什么索引和查询必须采用同一规则。
3. 词频、IDF、长度归一化及k1/b/delta的作用；BM25L与Dense分数为何不可直接相加。
4. 成功清单、document_id/chunk_id、原文与内容指纹如何对齐两套索引，为什么上传后不能默默沿用旧快照。
5. 空/未知词、常见词、并列和错误响应的测试设计，以及四条smoke不能代替完整效果评测的原因。

## 8. 下一步计划

当前仅指导用户亲自提交并推送七份收尾Markdown，Codex核实后补充实际Git信息。D13阶段A/B均完成，等待用户明确要求才开启D14。

用户明确开启D14后，再实现RRF并借已有评测器比较Dense/BM25/Hybrid，保持同切分、最终k与公开候选预算，记录逐题效果和耗时。未自动推进D14，保留35日节奏。
