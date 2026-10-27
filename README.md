# Project Copilot

基于RAG与LangGraph的研发知识及工单协作智能体。D1–D19已按各日实际学习/验证记录收尾；D20整合批次覆盖原D20–D23，2026-10-27用户亲自完成三项手测并确认ChatGPT学习理解，阶段A/B均完成，见[D20报告](docs/reports/DAY_20.md)。当前问答API仍Dense/750/150和rag-answer-v1；工单API独立，不调用模型，D24尚未开始。

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

运行参考、字段口径与样例手算见 [离线评测说明](evals/README.md)。D9开发提交0aeadef已由用户推送；2026-10-19用户亲自运行4题重放、核算013，并确认已完成ChatGPT学习、理解主流程。其余汇总核算、连续覆盖算例及三项异常门禁按用户明确委托由Codex验证通过，分别记于 [D9报告](docs/reports/DAY_9.md)，阶段A/B均完成；收尾提交91eea87已由用户同步。

D9没有模型调用、Qdrant索引或保留集效果实验；D10的真实采集与配置冻结见下方记录。

## D10：真实Dense基线与配置冻结

`evals/baseline.py`复用现有D4/D5与D9，提供plan/显式live采集/verify。仅使用冻结的30道dev题，独立集合pc_eval_dense_v0；按模型/供应商/文本指纹缓存向量，先2题smoke再继续，失败题不丢分母。固定八类各一题回答，保存完整消息/原始content/usage和待人工核验表，不自动确认引用支持。

完整Prompt/Schema/模型参数见 [V0配置](evals/configs/dense_v0.json)，执行链及耗时口径见 [评测说明](evals/README.md#d10真实采集与基线合同)。2026-10-19实际采集5文档/16片段、30题，3次Embedding＋8次回答，无自动重试；证据Recall@5=1.0，回答7条结构通过、1条非法JSON被拒绝并保留。2026-10-20用户人工确认六条有效回答及一次拒答通过，003仍为失败，八题端到端7/8；有效回答24项声明支持通过，其中1项为合理推论。详见 [真实V0报告](evals/reports/DENSE_V0.md)、[复核记录](evals/results/dense-v0-20261019-01/review.jsonl)及 [公开归档](evals/results/dense-v0-20261019-01/archive_manifest.json)。高召回仅针对小型自建开发集；原机器产物保留采集时pending快照，当前人工状态见复核记录。

## D11：单因素切分候选

根据V0四道dev检索Bad Case，准备 [500字符候选](evals/configs/chunk_size500.json)：只改变chunk_size，保留overlap150、分隔符和全部模型/检索/Prompt规则。复用D3切分与D9原文定位，新增只读 [边界检查器](evals/chunking.py)；默认业务切分仍为750/150。

离线结果为16→27片段，四题六段gold仍各自单块包含，承载片段缩短；这不代表真实排名或Recall改善。中文截句、Markdown标题与正文分离、长gold需跨块等限制均有测试。原始位置/片段见 [检查快照](evals/results/chunk-size500-preflight/inspection.json)，假设、开销与D12对照条件见 [候选报告](evals/reports/CHUNK_SIZE500_PREFLIGHT.md)。本日没有模型请求或Qdrant操作，未开启D12。

## D12：真实切分对照

[采集器](evals/chunking_run.py)复用30条历史问题向量，单独建立27块候选索引，在同一轮交错查询V0/候选并复用D9评分。[比较器](evals/comparison.py)记录逐题改善、退化、分类Bad Case和成对耗时；完整Prompt/模型合同及32段原文gold映射保留在 [公开无向量归档](evals/results/dense-v1-size500-20261021-01/experiment_config.json)。

2026-10-21授权执行1次Embedding（26条新文本）、60次本地检索，0问题Embedding/聊天调用，两侧错误0。26道可答题6改善/5退化/15不变：证据Recall@1为0.6667→0.7692，@5为1.0000→0.9231；4道无答案另计，未测拒答。012证据完成rank4→1，003却4→6，说明边界更短仍可能掉出top5。2026-10-22用户核对两题原文/排名并完成归档检查、双侧重评分及副本字节变化拒绝，新增模型调用0。默认切分保持750/150，结果与局限见 [V1对照报告](evals/reports/CHUNKING_V1.md)。

## D13：中文BM25最小检索

[词项模块](src/project_copilot/rag/bm25.py)使用jieba搜索分词与rank-bm25的BM25L，统一NFKC/大小写并保留错误码、下划线名称、API路径及命令选项。只读Qdrant成功清单中列出的完整chunk集合，检查原文与稳定ID后保存本地JSON快照；没有重新切分或Embedding。

以下为运行参考；`build`需要现有Qdrant，`tokenize/search`无需启动模型或FastAPI。快照拒绝覆盖，重建应选择新文件名：

```bash
uv run --frozen python scripts/bm25_index.py tokenize "上传HTTP_422 /documents/upload --env-file"
uv run --frozen python scripts/bm25_index.py build \
  --manifest evals/results/dense-v0-20261019-01/index_manifest.json \
  --output data/bm25/copilot-v0-v1.json
uv run --frozen python scripts/bm25_index.py search "/documents/upload" \
  --manifest evals/results/dense-v0-20261019-01/index_manifest.json \
  --index data/bm25/copilot-v0-v1.json --top-k 3
```

`--manifest`必须指向当前目标集合的真实成功清单；以上选用16片段的V0评测集合，不是应用默认的PDF样本集合。查询时清单、词典或依赖版本改变会拒绝旧快照；Qdrant后续内容变更需要显式重建，快照不会自动同步。查询词若以`--`开头，应将全部选项写在前面，再用`-- '--env-file'`分隔位置参数。

开发期相关297项测试通过，真实V0的5文档/16片段及四条固定查询验证见 [自动验收收据](docs/evidence/D13_BM25_SMOKE.json)。2026-10-22用户亲自完成分词、Qdrant就绪、独立快照构建、路径/中文/选项/无匹配查询及修改清单副本拒绝8个环节，另确认已通过ChatGPT学习、能解释主流程；Codex只读核对结果，执行者分别记录。本轮模型调用0，正式清单/快照保持。BM25分数不是置信度，也不与Dense分数直接相加；固定样例不代表全dev提升，D14仍等待用户明确开启。

## D14：RRF与三路真实对照

[RRF](src/project_copilot/rag/fusion.py)按每路从1开始的排名计算`Σ1/(60+rank)`，同ID去重，原文/元数据冲突拒绝，并列按UUID稳定排序。[Hybrid编排器](src/project_copilot/rag/hybrid.py)绑定同一成功清单，固定两路候选预算；既有Dense/问答逻辑保留，未复制服务端默认融合。

[独立采集器](evals/hybrid_run.py)复用原30个问题向量，读取相同16片段，Dense/BM25各最多10候选、RRF输出深度10，最终证据预算仍5块。每题各检索一次后共享候选，实际30次Qdrant查询、30次BM25查询、30次融合，模型请求及Qdrant写入均0。

真实dev的26可答题中，三路证据Recall@5均1.0，证据MRR@5依次为Dense0.8365、BM250.9327、Hybrid0.8974。Hybrid相对Dense6改善/0退化/20不变，但相对BM252改善/4退化/20不变；4无答案另计，未生成回答。完整逐题排名、候选来源和耗时见 [V2实验报告](evals/reports/HYBRID_V2.md)及 [公开归档](evals/results/hybrid-v2-20261022-01/manifest.json)。Hybrid总耗时为共享分支与融合的顺序合计，不能视为独立端到端实测或据单轮判断泛化/提速。2026-10-23用户完成归档校验、三路离线重评分、012计算反馈及副本变化拒绝，确认学习理解；原归档机器pending状态保留，人工结果另记于D14报告。方案选择留D17，D15等待明确要求。

## D15：本地Cross-Encoder最小重排

[重排适配器](src/project_copilot/rag/rerank.py)只给现有候选评分，默认最多10块、输出5块；另存raw logit和候选原排名，原文、metadata及原检索分数保持。模型首次非空评分才从项目本地缓存加载，缺缓存/加载或评分失败明确报错，不自动下载或回退。没有接入默认问答API。

固定`BAAI/bge-reranker-v2-m3`的revision为`953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`，共用batch2、512 token输入预算。早期验证使用CPU float32、4线程；用户明确选择后，已在RTX 4060 Laptop GPU上实测CUDA float16，并核对全部浮点模型参数实际为`cuda:0`/`float16`。`only_second`保留完整query，仅截passage；query用尽预算时拒绝。512是项目选择，不是模型最大长度；输出仍为完整原片段。

可选组`rerank`使用PyTorch CPU，`rerank-gpu`使用官方CUDA 13.0源及其CUDA依赖；两组互斥，基础服务不要求安装或加载它们。以下保留GPU方案自助参考；用户已于2026-10-25完成手测，实际范围见D15报告。本机已缓存相同固定模型，无需重新下载权重：

```bash
uv sync --frozen --group rerank-gpu
uv run --frozen --group rerank-gpu python scripts/rerank_smoke.py --local --device cuda --dtype float16
```

新环境尚无固定模型缓存时，先显式准备一次；下载约2.29GB至忽略目录`models/`：

```bash
uv run --frozen --group rerank-gpu hf download BAAI/bge-reranker-v2-m3 \
  --revision 953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e \
  --cache-dir models/huggingface \
  --include config.json model.safetensors tokenizer.json tokenizer_config.json special_tokens_map.json sentencepiece.bpe.model
```

推断脚本只读固定本地快照，无`--local`退出2。CPU方案可单独使用`uv sync --frozen --group rerank`及`uv run --frozen --group rerank python scripts/rerank_smoke.py --local`；CPU组不能执行CUDA，不同时安装两组。模型与用法见 [官方模型卡](https://huggingface.co/BAAI/bge-reranker-v2-m3)，源配置见 [uv官方说明](https://docs.astral.sh/uv/guides/integration/pytorch/)。

2026-10-23 Codex早期CPU验证复用D14公开012的10个候选，首/热各评分一次；gold块从候选第3排到第1，两遍分数一致，原文/身份逐值保持。另有合成长文2120→512 tokens诊断和超长query推断前拒绝。共21个本地pair，外部模型API/Qdrant请求0；[CPU历史收据](docs/evidence/D15_RERANK_SMOKE.json)原字节保留，其源码/依赖SHA表示当时身份，新增GPU组后的`pyproject.toml`/`uv.lock`另属当前执行身份。

GPU实测详见 [GPU验收收据](docs/evidence/D15_GPU_RERANK_SMOKE.json)：Torch 2.14.1+cu130、CUDA编译版本13.0，首/热推断约1112.377/160.087ms，首/热整个rerank约12007.819/174.468ms，首次模型加载10856.167ms。每遍Torch分配器峰值allocated为1166032896字节（约1.09GiB）、reserved为1189085184字节，属于当前进程分配器统计，不是整卡显存峰值；首次模型加载也不是冷盘或完整进程启动。

GPU新增首/热20个pair，gold仍从3到1，选中原候选名次均为`[3,4,2,1,5]`；GPU首热分数相同，与CPU float32的最大logit差约0.087717。同一组235项相关离线回归在CPU/CUDA环境均通过，其中70项为D15新增，不将重复执行累加为更多不同用例。CPU float32与GPU float16同时改变硬件和精度，单次耗时差不能归因为单因素加速或生产吞吐；两份单题工程证据均不代表完整dev或回答效果，完整对照仍留D16。

2026-10-25用户亲自完成默认门禁、CUDA小张量计算、GPU单题首/热重排、片段身份/分数观察和缺缓存拒绝五个环节，并确认学习理解。手测新增20个本地pair，首模型加载约11.50秒、热态重排约158.90ms，原文/metadata/原score逐值保持；没有新增外部模型API或Qdrant请求。此次结果与开发期自动测量分别记录，未增加长文截断、显存插桩或全量效果手测，D16未开启。

## D16：固定候选的V3重排对照

[运行器](evals/rerank_run.py)核验D14归档及D8冻结数据，复用30道dev题各10个原Hybrid候选，不重查向量库或问题Embedding。使用D15同版本CUDA float16模型，独立预热一次，再按原序运行两题smoke及其余28题；共31次成功本地评分、310个pair，外部模型API/Qdrant请求0。完整排名深度10供k=1/3/5/10评分，最终上下文预算仍5块。

26可答题的证据MRR@5由0.897436升至0.971154，Recall@5保持1.0；Recall@3却由1.0降至0.935897。逐题为4改善、2退化、20指标不变，4无答案另列。007的gold排名2→4，034的多段证据完成排名[1,3,3]→[1,4,4]，保留退化而不直接切换默认方案。该结果没有回答质量或候选外召回结论。

30题暖态本地重排中位数161.644ms、p95 169.081ms；本轮仅测重排组件，旧V2检索时间是历史快照，未组合成端到端耗时。配置、全部原始分数/原文、模型与GPU证据及逐题差异见 [V3报告](evals/reports/RERANK_V3.md)和 [20文件公开归档](evals/results/rerank-v3-20261025-01/manifest.json)，命令见 [评测说明](evals/README.md#d16固定候选的v3重排对照)。2026-10-26学习已由用户确认；本次按用户明确授权由Codex完成离线验证，见 [委托验证收据](docs/evidence/D16_DELEGATED_VERIFICATION_20261026.json)。不登记为用户亲自终端操作，D17仍等待明确开启。

## D17：最终配置冻结与保留集验收

[开发集选型](evals/reports/FINAL_SELECTION.md)及[最终配置](evals/configs/final_v1.json)先提交推送e676d50，再运行保留集`final-holdout-20261026-01`，不根据保留结果改选。原V0与最终Hybrid＋BGE方案使用同一文档索引、同一问题向量及最终5块；本次不生成回答，也不切换HTTP API默认路由。

9道可答题的证据Recall@5均1，MRR@5由0.944444升至1；1改善、8指标不变，另有1无答案。033两段证据从[3,2]提前为[2,1]，MRR=1仍不意味着第一块支持完整答案。实际Embedding API1、Dense查询10、GPU11次110pair（含开发题预热）；本轮后不再选参，旧开发集退化案例保持。

完整指标、分类、成本和时延边界见 [保留集报告](evals/reports/FINAL_HOLDOUT.md)，[公开归档](evals/results/final-holdout-20261026-01/manifest.json)包含24文件；[独立核对](docs/evidence/D17_HOLDOUT_AUDIT.json)重算800个指标并核对10个缓存向量，无新增模型调用。运行器/命令见 [评测说明](evals/README.md#d17最终方案与保留集)。

2026-10-26用户确认已学习并能解释D17主流程，本次委托Codex完成9条CLI验证、两侧20条评分重放、033/005/038原文核对及10个缓存向量检查，新增模型/API/GPU评分均0，见 [委托验证收据](docs/evidence/D17_DELEGATED_VERIFICATION_20261026.json)。实际执行者分别记录，D18仍等待明确开启。

## D18：入库失败恢复与幂等重传

已完成文档重传会先只读核对原片段，再复用原完成记录，不再发Embedding请求或重写向量/清单；同内容改名保留首次成功来源名，同名不同内容仍是独立文档。新写入回读精确ID、原文/metadata和索引版本通过后才登记成功；失败残留点不进入Dense/BM25可检索清单，显式同ID重试不增加重复点。

本地验收参考（不读.env，不调用模型）：

```bash
uv run --frozen python scripts/ingestion_recovery_smoke.py --local \
  --output-dir data/manual-d18/recovery-01
```

先启动现有Qdrant容器，输出目录必须尚不存在。脚本使用合成二维向量和自己新建的随机pc_d18_recovery_集合，主动触发部分写入/清单保存前失败、显式重试、重传及同名异内容，最后清理该测试集合；实际结果写report.json。默认不加--local退出2。

开发期完整离线1069项通过，默认跳过的1项真实HTTP集成另行通过；真实存储8阶段显示点数[1,2,4,4,5,7,7,7]、完成文档[1,1,2,2,3,3,4,4]，模型API0。证据见[恢复收据](docs/evidence/D18_RECOVERY_SMOKE.json)和[存储保护/历史回归](docs/evidence/D18_STORAGE_AUDIT.json)。合成向量仅验证工程边界，旧D17配置/结果不改；详细行为与学习交接见[D18报告](docs/reports/DAY_18.md)。

2026-10-26按用户本次授权，Codex另行完成代操作验证：真实Qdrant恢复8阶段、上传失败/重试的2项ASGI测试、缺--local及输出防覆盖门禁、4份历史归档verify均符合预期。原3个集合及点数保持，测试集合已删除，新增模型API调用0；见[委托验证收据](docs/evidence/D18_DELEGATED_VERIFICATION_20261026.json)。用户另行确认已通过ChatGPT学习、能够解释主流程，阶段A/B均完成；本次不重复运行完整1069项测试，也不登记为用户亲自终端操作。

## D19：回答可靠性与Prompt小对照

模型输出先经严格JSON、Pydantic回答Schema和本次上下文编号校验，三类错误分别记录为invalid_json、invalid_schema、invalid_citation；API继续返回脱敏502，空证据由后端确定返回insufficient_evidence且不调用聊天模型。TXT/Markdown的page=null保持，合法引用仅说明来源映射正确，不代表事实支持已通过。

独立实验使用5个新编中文样例（可答、弱证据、纯注入、事实混合注入、空证据），比较冻结v1与一个Few-shot候选。两侧固定问题、证据、User消息、Schema和生成参数，只改变System模板；不使用保留集选提示词，不覆盖V0或D17结果，不自动上线候选。

离线入口参考（不读.env，不访问模型或Qdrant）：

```bash
uv run --frozen python -m evals.prompt_reliability plan
uv run --frozen python -m evals.prompt_reliability offline --output-dir data/manual-d19/offline-01
uv run --frozen python -m evals.prompt_reliability verify --output-dir data/manual-d19/offline-01
```

offline使用明确注入的固定响应，仅验证管线，不能用其“通过率”说明Prompt效果。真实对照使用collect并显式加--live，需遵守费用约定；最多8次DeepSeek请求、无Embedding/Qdrant或自动重试，上游失败即停止，原始回答和失败项保留。当前实测范围与限制见[实验报告](evals/reports/PROMPT_RELIABILITY_D19.md)；本次委托验证另行记录，D20不自动开启。

本轮经用户单独授权，实际8次DeepSeek生成完成；两版各4个非空场景的结构/状态/口号检查通过，空证据各1条由后端确定拒答。该小样本未显示候选更好，保留默认v1；本次受托原文复核由Codex执行，原人工标注快照保持。最终完整离线1105项通过、1项真实Qdrant默认跳过，冻结数据/配置/历史结果保持，见[真实对照审计](docs/evidence/D19_PROMPT_LIVE_AUDIT.json)。

2026-10-26用户授权Codex直接验证并提交：两份归档重放、5项拒绝门禁及临时Uvicorn的12次真实本地HTTP检查通过，原回答/引用与原文核对一致，详见[委托验证收据](docs/evidence/D19_DELEGATED_VERIFICATION_20261026.json)。HTTP重放已保存回答与受控错误，新增模型API/Embedding/Qdrant调用0；233份受检非Markdown文件保持。用户另行确认ChatGPT学习完成，操作执行者为Codex，不登记为用户亲自终端操作。

## D20整合批次：PostgreSQL工单后端

覆盖原D20连接、D21模型/迁移、D22 Service/API、D23事务/持久化，当前计划为31个主任务批次＋原D35弹性日，共32；D24–D34保留原编号。新增SQLAlchemy、Psycopg和Alembic，不改RAG默认与历史评测。

- `POST /tickets`：JSON仅title（≤200）、description（≤10000），非空；201返回PG生成的UUID、open及带时区时间。
- `GET /tickets`：status、q、limit（默认20，最多100）、offset（最多10000），返回items/limit/offset；按创建时间/ID倒序，关键词绑定参数并转义字面%/_。
- `GET /tickets/{UUID}`：真实详情或404；输入非法422，数据库未配置/不可用/提交失败503，保留X-Run-ID与脱敏日志。

应用PG使用127.0.0.1:5433和project-copilot专属postgres_data卷；测试实例在5434、test profile及独立tmpfs中，测试只创建/清理自己的随机数据库。无连接配置时原health/RAG仍可用；工单请求返回503。启动不自动建表，先显式升级：

```bash
docker compose --env-file .env.example up -d --wait postgres
PROJECT_COPILOT_DATABASE_URL='postgresql+psycopg://project_copilot:local-dev-only@127.0.0.1:5433/project_copilot' \
  .venv/bin/python -m scripts.ticket_db upgrade
```

连接串可保存到新项目.env，示例口令仅用于本机演示；保留原.env密钥，不替换整个文件。Engine归lifespan管理，每请求独立Session；Service flush后，由function作用域依赖提交/回滚并关闭，成功提交后才发201。无状态更新/删除API、Agent工具或确认机制；本批次不承诺创建幂等。

最终全量1192 passed、1 skipped（原Qdrant opt-in），其中本批次87项、36项真实PG。真实PG17.11的迁移/约束/API/回滚与8次Uvicorn HTTP验收通过，两个不同API进程查到同一已提交工单；自己的测试库及进程已清理，模型调用0。证据见[PG原始收据](docs/evidence/D20_POSTGRES_SMOKE.json)和[批次审计](docs/evidence/D20_BATCH_AUDIT.json)。

2026-10-27用户亲自完成[创建/查询、受控回滚、API重启持久化三项](docs/D20_MANUAL_VERIFICATION.md)：201创建后API与psql查到相同工单；探针回滚无残留、连接归还；API进程56610→58981后原ID/时间保持，详见[手动验证记录](docs/evidence/D20_MANUAL_VERIFICATION_20261027.json)。用户确认已通过ChatGPT学习并能解释主要流程，阶段A/B均完成；本次收尾文档待用户提交，不自动开始D24。系统教学入口见[D20报告](docs/reports/DAY_20.md)和[学习笔记](docs/STUDY_NOTES.md#d20整合批次postgresql工单后端原d20d23)。
