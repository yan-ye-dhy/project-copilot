# Project Copilot

基于RAG与LangGraph的研发知识及工单协作智能体。D1–D3阶段A/B已完成；D4已实现单文件Embedding→独立Qdrant Dense入库与检索、成功清单及CLI。165项离线测试、1项真实HTTP存储测试和两次授权Embedding最小联调通过；D4待用户提交推送、ChatGPT学习及主动手测。回答生成与业务Agent按后续计划实现。

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

API文档位于 `http://127.0.0.1:8002/docs`。`/health`只表示当前应用能响应，不检查模型、Qdrant或PostgreSQL就绪状态；D4索引与检索通过独立CLI验证。

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

以下为运行参考，学习后的手动测试仍等用户主动开启，不要求现在逐条操作：

```bash
docker compose --env-file .env.example up -d qdrant
uv run --frozen python scripts/dense_index.py ingest tests/fixtures/documents/sample.pdf --live
uv run --frozen python scripts/dense_index.py search "local document loading" --live
```

`ingest`只处理一个受控本地文件，`search`只查询一个问题；两者使用智谱Embedding，须有新项目密钥并显式添加`--live`，SDK自动重试关闭。没有`--live`时在读取配置/创建客户端之前退出2。重复入库当前仍重新嵌入，稳定UUID使Qdrant覆盖同一组点，但不等于免除模型费用；Embedding缓存留后续实验阶段。

默认集合为`pc_knowledge_dense_v0`，命名向量字段`dense`、2048维、Cosine，默认`top_k=5`。已有集合的字段、维度、距离、Dense-only配置不匹配时拒绝操作；不自动重建集合。原文保存在payload的`text`，D3元数据完整保留，另用`dense_index_version`记录模型、维度、切分等配置指纹；D3的`metadata.index_version`仍是切分指纹。

成功清单默认在`data/manifests/<collection>.json`，仅在Qdrant确认写入完成后原子保存。检索限定清单内文档和当前Dense版本，再校验具体片段身份；空清单返回空结果，不请求模型。清单保存失败时向量可能已写入，但本次不登记成功。当前支持顺序单进程入库，完整故障恢复及并发问题不在D4完成。

入库输出包含文档/片段ID，检索输出包含实际原文、来源、PDF页索引和相关度。2026-10-13自动联调实际写入三页样本PDF的2个非空片段，查询返回页0/2，首条来自页0；这里只验证工程链路，效果评测留D7–D10。D4没有上传或问答API，也没有BM25、Rerank和聊天模型调用。

真实HTTP测试须单独开启，使用假二维向量，只创建/清理随机`pc_test_`集合，不调用收费模型：

```bash
PROJECT_COPILOT_RUN_QDRANT_INTEGRATION=1 uv run --frozen python -m pytest -q tests/test_qdrant_integration.py
```

普通`uv run --frozen python -m pytest -q`仍是离线测试，默认跳过这项外部服务测试。实际命令、结果与边界见D4报告。
