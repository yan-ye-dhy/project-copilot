# Project Copilot

基于RAG与LangGraph的研发知识及工单协作智能体。D1–D8阶段A/B已完成，D8收尾056eff8已同步。D9已实现离线排名评测器，开发集固定样例验证文档/证据指标及结果保存；待用户Git交接、学习和主动开启的手动验证，见 [D9报告](docs/reports/DAY_9.md)。真实Dense基线留D10，业务Agent按后续计划实现。

协作流程见 [长期协作规则](docs/COLLABORATION_RULES.md)：Codex开发、自动验证、提供报告并指导Git操作；用户亲自提交和推送GitHub，随后通过ChatGPT学习代码/架构，学习后主动要求Codex开始手动测试。以下命令是自助运行参考，Codex仅在用户要求后单步带练。

## 本地运行

需要Python3.13与uv。先在新项目根目录同步独立环境：

```bash
cd /home/yanye/project-copilot
uv sync --locked
uv run --frozen uvicorn project_copilot.main:app --host 127.0.0.1 --port 8002
```

另一个终端验证：

```bash
curl --fail http://127.0.0.1:8002/health
```

默认返回：

```json
{"status":"ok","service":"Project Copilot","environment":"development"}
```

API文档位于 `http://127.0.0.1:8002/docs`。`/health`只表示当前应用能响应，不检查模型、Qdrant或PostgreSQL就绪状态；D4索引与检索通过独立CLI验证，D5问答入口为`POST /rag/ask`。

## 配置与D1练习

没有 `.env` 也可按默认值启动。需要自定义时，可把 `.env.example` 复制为新项目根目录 `.env`。应用读取该明确路径，不随终端当前目录寻找其他项目的 `.env`。

配置优先级为：显式构造参数 → 进程环境变量 → 新项目 `.env` → 默认值。变量均使用 `PROJECT_COPILOT_` 前缀；名称不能为空或只有空格，environment只允许development/test/production。修改配置后重启后端。

手动测试阶段的参考练习：预测以下命令启动后health的service值，再按Codex单步指导亲自验证；当前不自动安排操作：

```bash
PROJECT_COPILOT_APP_NAME="Copilot Learning" uv run --frozen uvicorn project_copilot.main:app --host 127.0.0.1 --port 8002
```

默认端口8002由Uvicorn命令决定，与参考项目8001隔离；D1未增加一个不生效的“端口配置”字段。

## 开发与学习记录

- [D1设计、代码与运行导读](docs/D1_WALKTHROUGH.md)
- [D1开发报告与验收交接](docs/reports/DAY_1.md)
- [D2开发报告与模型验证状态](docs/reports/DAY_2.md)
- [D3开发报告与文档切分验收](docs/reports/DAY_3.md)
- [D4开发报告与真实Dense索引验收](docs/reports/DAY_4.md)
- [D5开发报告与结构化问答交接](docs/reports/DAY_5.md)
- [D6开发报告与上传/Dense模块验收](docs/reports/DAY_6.md)
- [D7开发报告与语料/人工复核](docs/reports/DAY_7.md)
- [D8开发报告与划分/冻结交接](docs/reports/DAY_8.md)
- [D9开发报告与离线评测交接](docs/reports/DAY_9.md)
- [三方协作与8项开发报告规范](docs/COLLABORATION_RULES.md)
- [开发计划](docs/DEVELOPMENT_PLAN.md)
- [参考审计](docs/REFERENCE_AUDIT.md)
- [开发日志](docs/DEVELOPMENT_LOG.md)
- [学习笔记](docs/STUDY_NOTES.md)
- [面试笔记](docs/INTERVIEW_NOTES.md)

工程设计参考 [mfmezger/conversational-agent-langchain](https://github.com/mfmezger/conversational-agent-langchain)。原仓库保持只读；新项目保留独立包、环境与Git历史。本次没有移植原仓库整套入口或业务实现。

## D2：Embedding与自动测试参考

客户端位于 `src/project_copilot/infrastructure/models.py`。使用原文本数组请求智谱embedding-3，显式要求float，按每批文本条数分批；检查返回模型、条数、index、有限数值与预期维度。`embedding_batch_size=64`不是文档切分长度；`embedding_expected_dimensions=2048`用于本地校验，没有向请求添加dimensions参数。

配置示例见 `.env.example`。真实密钥仅写入新项目根目录 `.env` 的 `PROJECT_COPILOT_ZHIPU_API_KEY`，Git忽略该文件。缺key时健康接口仍可启动，创建Embedding客户端才提示配置缺失。timeout为网络操作超时，默认20秒；SDK自动重试关闭。

自动测试参考（真实SDK配假HTTP传输，不调用模型）：

```bash
uv run --frozen python -m pytest -q
```

最小真实验证参考：

```bash
uv run --frozen python scripts/embedding_smoke.py --live
```

脚本仅请求一条固定中文，输出状态、模型、条数和实际维度，使用with关闭客户端；不输出密钥或完整向量。官方embedding-3标准价为0.5元/百万tokens，实际按账号用量计费，见 [官方定价](https://docs.bigmodel.cn/cn/guide/start/pricing.md)。不加`--live`会显示参数错误并退出；没有新项目key则不创建模型客户端。此处命令供参考，学习后的手动测试仍由用户主动开启再逐步指导。

## D3：离线查看文档与片段

`rag/loaders.py`按UTF-8读取TXT/MD，以PDFium提取PDF文字层；`rag/splitters.py`逐页递归切分，默认750字符、目标重叠150字符，分隔符依次为段落、换行、空格和最终字符回退。字符数按Python `len`计算，不等于模型token数；自然边界不保证每对片段精确重叠150字符。

每份文件保留原始字节SHA-256与内容身份；同内容改名不改变document ID，同名不同内容不会混为一份。片段另有UUID、整份文档中的序号和切分指纹，PDF的`page`为0起始真实页索引，TXT/MD为`null`。`index_version`在D3仅表示切分指纹，D4还需记录模型、维度和实际索引配置。

离线参考命令（不读取密钥、不请求模型）：

```bash
uv run --frozen python scripts/inspect_document.py tests/fixtures/documents/sample.pdf
```

输出JSON包含按页原文、片段文本、来源、ID及长度；示例PDF有三页，中间空白页保留为页1，片段来自页0和页2。其他自编样本及来源说明见 [测试样本](tests/fixtures/documents/README.md)。可用`--chunk-size`与`--chunk-overlap`观察其他配置，要求`0 <= overlap < size`。

空文档返回`status=empty`和空片段列表；非UTF-8文本、损坏PDF或不支持的类型明确报错。PDF只处理文字层，没有OCR或复杂版面还原。原社区loader依赖已[停止维护](https://github.com/langchain-ai/langchain-community/issues/674)，因此采用标准库和同款PDFium的薄适配，保留成熟递归切分器。用户已完成D3手测与学习确认，实际范围见 [D3报告](docs/reports/DAY_3.md)；下一Day仍等用户明确要求。

## D4：单文件Dense索引与检索

使用官方`qdrant-client`1.19.1与固定服务器镜像`qdrant/qdrant:v1.19.2`。Compose项目名为`project-copilot`，HTTP仅映射`127.0.0.1:6433`，使用独立named volume。WSL用户需先开启Docker Desktop及当前发行版的WSL Integration。

以下保留运行参考；D4用户手测已完成，无需因阅读此处再次运行收费命令：

```bash
docker compose --env-file .env.example up -d qdrant
uv run --frozen python scripts/dense_index.py ingest tests/fixtures/documents/sample.pdf --live
uv run --frozen python scripts/dense_index.py search "local document loading" --live
```

`ingest`只处理一个受控本地文件，`search`只查询一个问题；两者使用智谱Embedding，须有新项目密钥并显式添加`--live`，SDK自动重试关闭。没有`--live`时在读取配置/创建客户端之前退出2。重复入库当前仍重新嵌入，稳定UUID使Qdrant覆盖同一组点，但不等于免除模型费用；Embedding缓存留后续实验阶段。

默认集合为`pc_knowledge_dense_v0`，命名向量字段`dense`、2048维、Cosine，默认`top_k=5`。已有集合的字段、维度、距离、Dense-only配置不匹配时拒绝操作；不自动重建集合。原文保存在payload的`text`，D3元数据完整保留，另用`dense_index_version`记录模型、维度、切分等配置指纹；D3的`metadata.index_version`仍是切分指纹。

成功清单默认在`data/manifests/<collection>.json`，仅在Qdrant确认写入完成后原子保存。检索限定清单内文档和当前Dense版本，再校验具体片段身份；空清单返回空结果，不请求模型。清单保存失败时向量可能已写入，但本次不登记成功。当前支持顺序单进程入库，完整故障恢复及并发问题不在D4完成。

入库输出包含文档/片段ID，检索输出包含实际原文、来源、PDF页索引和相关度。2026-10-13自动联调写入三页样本PDF的2个非空片段；2026-10-14用户重入库后精确计数仍为2，查询返回页0/2且首条来自页0，维度配置与清单不一致时在Embedding前拒绝。用户已确认理解，实际操作及覆盖边界见 [D4报告](docs/reports/DAY_4.md)。这里只验证工程链路，效果评测留D7–D10。D4没有上传或问答API，也没有BM25、Rerank和聊天模型调用。

真实HTTP测试须单独开启，使用假二维向量，只创建/清理随机`pc_test_`集合，不调用收费模型：

```bash
PROJECT_COPILOT_RUN_QDRANT_INTEGRATION=1 uv run --frozen python -m pytest -q tests/test_qdrant_integration.py
```

普通`uv run --frozen python -m pytest -q`仍是离线测试，默认跳过这项外部服务测试。实际命令、结果与边界见D4报告。

## D5：基于检索证据的结构化问答

问答复用D4集合及成功清单，不再次入库。固定System消息描述角色、证据和JSON契约；问题与编号、来源、页索引、原文作为User消息中的JSON数据绑定。默认Zero-shot，格式骨架不是具体任务的输入/输出示例；Few-shot效果对照留D19。

新项目`.env`另配置`PROJECT_COPILOT_DEEPSEEK_API_KEY`，无此key仍可启动health。默认`deepseek-flash`、temperature0、max_tokens1024、timeout30秒，显式关闭thinking与SDK自动重试。2026-10-14核验的[当前模型](https://api-docs.deepseek.com/quick_start/pricing/)为V4.1-Flash；旧`deepseek-chat/reasoner`已[退役](https://api-docs.deepseek.com/news/news260424/)，不作为新默认值。保留请求别名与实际返回模型，别名不能保证云端实现永久不变。

以下供学习后复现参考；有效问答通常包含一次查询Embedding和一次DeepSeek生成，HTTP入口没有CLI的`--live`参数，发送POST即发起真实业务请求：

```bash
curl --noproxy 127.0.0.1 --fail --silent --show-error \
  http://127.0.0.1:8002/rag/ask \
  -H 'Content-Type: application/json' \
  --data '{"question":"样本文档第一页描述了什么功能？"}'
# 同一固定问题的CLI参考；两种入口择一使用，避免重复收费
uv run --frozen python scripts/rag_smoke.py --live
```

返回`status/answer/citations`、实际发送的证据数量、Prompt/Schema指纹、Dense索引版本及模型参数/可用usage。引用的`text/metadata`由后端从本次证据取出，PDF的page仍从0开始。没有可用证据时返回`insufficient_evidence`，不调用聊天模型，generation为null；供应商未提供usage时也用null，不填假0。

上下文按检索顺序使用完整片段，按chunk ID去重，累计原文最多6000字符；不截断单片段，超出剩余预算则跳过并继续，重新从1编号。这个字符预算不等于输入token上限。模型只能返回status、answer、citation_numbers；JSON重复字段、非法Schema和本次上下文之外的编号会被拒绝，不自动修复或重试。无效问题HTTP422，配置/清单错误503，上游请求或结果错误502；日志只记录错误类型。

[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)约束JSON格式，本地Pydantic继续检查字段与引用。有效编号只证明引用身份，不证明答案事实正确；证据支持率、弱证据/注入防护及Prompt对照按D19验收。D5没有新增依赖、上传、历史会话或前端。

2026-10-14 Codex自动启动后端并发送一次真实POST，HTTP200返回“第一页描述了本地文档加载功能”，引用来自`sample.pdf`页索引0，原文与D3元数据匹配；只复用原有2点索引，随后关闭自动启动的后端。DeepSeek返回输入358、输出24、总计382 tokens；Embedding usage及实际账单未采集。这是一个已知样本的工程验收，不是冻结数据集效果评测，也不替代用户D5手测。

2026-10-15用户另行亲自完成CLI保护、容器与就绪、后端启动及health、空白问题422、一次真实问答200与引用核对、临时空白密钥503、本地Schema接受/拒绝练习及服务关闭。此次DeepSeek输入358、输出25、总383 tokens；与前一天自动验收分别记录。用户确认已通过ChatGPT学完并能解释主调用链和密钥拒绝原因，D5阶段B完成；随后用户明确开启D6，见下节。

## D6：单文件上传与请求关联

`POST /documents/upload`接收multipart/form-data，仅接受一个名为`file`的文件字段，支持UTF-8 TXT/MD及文字层PDF。默认文件上限5MiB，可通过`PROJECT_COPILOT_UPLOAD_MAX_BYTES`调整（1字节至50MiB）；整体HTTP body另留64KiB表单余量，按实际接收字节检查，业务复制再限制文件字节。新增[python-multipart](https://fastapi.tiangolo.com/tutorial/request-files/)解析依赖；利用现有[Starlette请求体中间件](https://starlette.dev/middleware/#requestbodylimitmiddleware)限制整个body。

以下为学习后运行参考；上传会真实请求Embedding，问答另请求问题Embedding和DeepSeek，执行前按项目费用约定确认范围：

```bash
curl --noproxy 127.0.0.1 --silent --show-error \
  http://127.0.0.1:8002/documents/upload \
  -F 'file=@tests/fixtures/documents/sample.pdf;type=application/pdf'
```

只有Qdrant写入与成功清单保存均完成后返回`status=indexed`、`mode=DENSE`、文档/片段ID、文件字节数、索引版本及run_id。原名经基本名称/后缀/MIME筛选后作为source_name；临时物理文件使用服务端随机名，所有正常/异常路径均尝试清理。实际内容由原UTF-8/PDF解析器校验；框架可能先将Windows上传路径规范为basename，文件名不会用于构造物理路径。

HTTP415表示不支持的类型，422表示缺文件/非法名/解析失败/空文本，413表示文件或整个body超限，400表示表单结构非法；上传繁忙时409。配置或成功清单问题503，上游请求/结果错误502，临时IO失败500。上传采用进程内非阻塞锁保护完整入库，维持D4单进程顺序契约；跨进程/CLI并发、恢复和跨存储事务按后续阶段处理。同内容改名共享稳定ID，重新上传会覆盖该身份的显示来源。

每个HTTP请求由服务端生成`X-Run-ID`，上传成功正文返回同值；请求完成日志记录run_id、方法、路径、状态、耗时和`trace_exported=false`。未知500响应提供安全detail及run_id；此处用于本地关联，Phoenix导出尚未实现。`PROJECT_COPILOT_TRACING_ENABLED`默认false，当前设true会明确拒绝配置，D33才接入实际导出。完整运行链与复盘问题见 [D6学习笔记](docs/STUDY_NOTES.md#d6上传接入与dense模块复盘)。

2026-10-15用户授权后，Codex完成一次真实上传→问答：两接口均HTTP200，上传2块，引用与自编PDF页0原文及元数据一致；重复上传后集合精确点数仍为2，临时文件已清理，自动启动的后端正常关闭。仅2次Embedding＋1次DeepSeek，无自动重试；DeepSeek输入358、输出24、总382 tokens，Embedding usage及实际账单未采集。配置、Prompt版本、run_id与输出见 [D6自动验收记录](docs/evidence/D6_DENSE_SMOKE.json)。这是已知样本的工程验收；完整效果评测留后续阶段，当时用户学习手测尚待完成，最新结果见下方补记。

2026-10-16用户亲自完成请求关联、类型415/空文件422/实际文件超限413、失败后临时清理、Qdrant就绪、PDF上传、精确点数2和固定问答；引用与D3原文/元数据一致，随后关闭后端并确认临时文件0。手测另消耗授权的2次Embedding＋1次DeepSeek，实际usage358/24/382，与开发期分别记录；完整操作及问题修正在 [开发日志](docs/DEVELOPMENT_LOG.md)中保留。用户确认已通过ChatGPT学习、能解释主链及来源名/临时路径、run_id/Phoenix区别，D6阶段A/B均完成；随后用户明确开启D7。

## D7：语料与证据标注准备

[数据说明](evals/datasets/copilot_v0/README.md)记录5份中文语料的来源、字节版本及D3文档身份；[逐题复核队列](evals/datasets/copilot_v0/REVIEW_QUEUE.md)提供20题问题、参考答案与原文证据候选，覆盖八类题型。来源以已公开项目真实操作记录为主，补Docker/Qdrant官方资料的自编中文笔记；不冒充企业生产事故或真实用户提问集。

离线检查命令：

```bash
uv run --frozen python evals/validate_dataset.py --dataset evals/datasets/copilot_v0
```

2026-10-17用户实际核对后明确批量确认001–020全部通过：18题可回答、2题无答案，confirmed20/pending0。本人添加`--require-reviewed`再次检查通过，退出0、ready_for_baseline=true；复核前该模式曾正确拒绝pending草稿。检查文档版本、原文字符区间与关联不能替代人工判定事实支持；本轮确认依据为用户反馈。用户已确认ChatGPT学习和关键概念理解，D7阶段A/B完成。

后续仅将来源清单列出的corpus文件入库，问题、标签和复核队列不进入知识库。`ready_for_baseline`只表示本目录人工复核门禁满足；D7未执行模型调用、索引写入、指标计算或开发/保留划分。D8的后续扩充和当前状态见下节，D9实现检索评测器，D10运行基线。

## D8：扩充评测集、问题族划分与冻结工具

复用原5份语料并保留D7的20题及人工确认记录，追加`pc-d8-021`至`pc-d8-040`。40题、35可回答/5无答案，八类题型分布为8/5/5/5/4/4/5/4；2026-10-18用户实际核对并明确确认新增20题全部通过，现confirmed40/pending0。相近题与追问按23个family分组，[划分清单](evals/datasets/copilot_v0/split_manifest.json)固定30开发/10保留，同族不跨侧，选择未依据任何检索效果。

项目根目录的离线预检参考：

```bash
uv run --frozen python -m evals.freeze_dataset --dataset evals/datasets/copilot_v0
```

默认只检查划分，当前数据全量已确认；`--freeze`要求全量人工确认，并拒绝覆盖已有冻结。用户已亲自创建 [正式冻结清单](evals/datasets/copilot_v0/frozen_manifest.json)，随后`--verify`返回verified、退出0，指纹一致。后续以`--verify`核验，修改问题、答案、审核元数据或划分都会使原冻结失效。

用户另在data/manual-d8/tampered测试副本仅修改一条合法参考答案，副本`--verify`按预期退出1；正式9份输入仍与清单匹配。用法、指纹、保留集边界及局限见 [数据说明](evals/datasets/copilot_v0/README.md)。D8操作全部离线，收尾056eff8已同步，用户随后明确开启D9。

## D9：离线排名评测与原文证据映射

`evals/metrics.py`分别计算文档/证据Recall@k、MRR@k及全部必需证据覆盖，保留重复chunk的原排名。gold按同文档版本/页的完整原文覆盖映射，可跨多个片段连续拼全；不把字符相交当成命中，无法唯一定位的重复文本明确报错。未修改D3或D8冻结数据。

`evals/run.py`先核验冻结和数据指纹，仅允许开发集，明确选择子集或全部30题；重放排名、逐题耗时/错误及指标保存到新目录，拒绝覆盖。检索失败留在可回答题分母，无答案指标为null并单独统计，追问使用冻结独立问题。固定4题样例为synthetic_fixture，只有评测器验算意义，不是Dense检索成绩。

运行参考、字段口径与样例手算见 [离线评测说明](evals/README.md)。本轮没有模型调用、Qdrant索引或保留集效果实验；D10再完成真实检索采集、向量缓存、基线及Prompt配置冻结。用户Git和ChatGPT学习后，再主动开启D9手动验证，不自动进入D10。
