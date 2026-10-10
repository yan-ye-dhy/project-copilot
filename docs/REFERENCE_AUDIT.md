# 原项目参考审计与阶段0记录

日期：2026-10-09。参考仓库：`/home/yanye/conversational-agent-langchain`；新项目：`/home/yanye/project-copilot`。正式路线见 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)。

路线引用更新（2026-10-09）：本文保留阶段0审计时的原20日规划与历史测试记录；正式计划已调整为34个主任务日＋1个可前移弹性日，每日1.5～2.5小时，已完成审计不重复计日。执行日号与验收以链接的最新版开发计划为准，本次未重新运行本文测试。

后续进度（2026-10-10）：D1已成功初始化新项目独立Git与Python环境，并通过最小健康接口验收，实际记录见 [DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md)。下文Git限制与测试结果仍是阶段0快照，不代表当前新项目状态；未重新运行原项目审计测试。

本轮以本地源码及已安装依赖为准。已读 README、旧开发计划、旧审计、个人贡献记录、关键源码、容器配置与相关测试。进行了少量隔离单元测试和本地失败重现；未启动应用/容器、调用收费模型、下载模型或复验完整在线RAG。所有新文档写在新项目，未修改原应用。

## 1. 工作区与Git快照

两个绝对路径均可读取，新项目本轮文档可写。原项目HEAD为 `878a3a654a4c8120553c19303fa4a88419542419`，当前9个tracked文件修改，合计 `+40/-9`：

```text
.dockerignore
docker-compose.yml
frontend/assistant.py
src/agent/api.py
src/agent/backend/graph.py
src/agent/backend/nodes/grading.py
src/agent/backend/prompts.py
src/agent/utils/config.py
src/agent/utils/embeddings.py
```

另有未追踪 `PERSONAL_CONTRIBUTIONS.md`、`docs/`、`interview-notes/`。旧审计中的“7个文件、+35/-7”是2026-09-29快照；后来的引用编号和Phoenix批量导出修改应结合当前源码和贡献记录查看。

新项目没有有效Git仓库：`git -C /home/yanye/project-copilot status --short` 返回 `fatal: not a git repository`。虽然 `.git` 目录存在，但当前目录权限是只读；本轮没有初始化、创建提交或尝试覆盖该目录。此限制需在首次正式开发时处理。

系统只有 `python3`（3.12.3），直接运行 `python` 返回command not found；后续本地检查使用 `python3`，原项目隔离测试使用其已有 `.venv/bin/python`（Python3.13）。uv与Docker CLI存在，但这不证明Docker daemon或服务已就绪。

## 2. 十一项能力：真实源码位置与复用判断

表格第一列源码位置以原项目根目录为起点；可在IDE按路径和行号跳转。后文简写的 `utils/`、`routes/`、`backend/` 统一位于 `src/agent/`，`nodes/` 位于 `src/agent/backend/`；`embedding_management.py` 位于 `src/agent/backend/services/`。行号描述的是本轮快照。

| 能力 | 文件与关键调用 | 判断与新项目适配 |
| --- | --- | --- |
| FastAPI入口 | `src/agent/api.py:15` 导入routes；`:54` 创建app；`:143` 起注册路由；`routes/rag.py:13` 导入时建图 | 参考路由/Schema分层；新项目使用lifespan管理配置与资源，不直接import旧app |
| DeepSeek对话 | `src/agent/backend/graph.py:33` 按 `deepseek/` 前缀禁用thinking；`:34` 创建ChatLiteLLM；`src/agent/backend/nodes/grading.py:22` 使用function_calling解析Grade | 可参考参数适配；key在config声明但未在此显式传入，工厂应明确模型/key/timeout/retry；Grade不是业务工具调用 |
| 智谱Embedding | `src/agent/utils/embeddings.py:32` → OpenAIEmbeddings，原文本输入、float输出、batch64 | 适配可选取复用；`embedding_size`没有传给这个客户端，collection维度不能决定API输出维度；限制与实际输出后续核验 |
| Qdrant | `src/agent/utils/vdb.py:13/20/29` 导入时创建稀疏模型与客户端；`:52` QdrantVectorStore；`:120` 集合创建 | 客户端/Document映射值得参考；去除导入副作用，Dense先行，集合schema与模型身份显式检查 |
| Loader / Splitter | `src/agent/backend/services/embedding_management.py:39` PDF、`:41` TXT；`:47` 750/200字符递归切分；`:60` add_texts | 复用成熟loader，支持UTF-8/MD；分隔符与来源身份需适配，不能原样复制上传服务 |
| Dense Retrieval | `src/agent/utils/retriever.py:22` → cached embedding/store → as_retriever；`src/agent/backend/nodes/retrieval.py:27` invoke | 具备dense向量路径，但当前store固定HYBRID，没有应用级Dense-only基线；新项目明确设置DENSE |
| Hybrid / RRF | `src/agent/utils/vdb.py:57` HYBRID；安装依赖 `langchain_qdrant/qdrant.py:601` 双路prefetch，`:625` 默认Fusion.RRF | 应用未自己实现RRF；默认行为来自当前安装依赖。中文BM25/RRF需要明确实现与评测 |
| Rerank | `src/agent/utils/reranker.py:53` Cohere；`:23/81` FlashRank；`:116` none也按top_k截断；`src/agent/backend/nodes/retrieval.py:72` 图内调用 | 可参考原文身份保留；基线禁用真实重排，候选数/最终k对照一致；默认FlashRank中文效果没有证据 |
| LangGraph | `src/agent/backend/state.py:11` messages/query/documents/retry_count；`src/agent/backend/graph.py:71` 注册节点；`:86` 条件评分；`:100` compile | 参考State与条件边；当前是文档RAG图，缺业务工具循环、工单、checkpointer和确认流程 |
| Phoenix | `src/agent/api.py:27` register，project=`rag`，batch=True；`:33` OpenInference埋点；Compose提供collector | 批量导出配置可借鉴；新project与collector地址独立，导出成功后才能声称Trace可用 |
| Docker Compose | `docker-compose.yml` 四服务；`:33` 固定qdrant容器名，`:36/67` 旧持久目录；Dockerfile用Python3.13/uv | 参考构建和依赖就绪；改新包入口、镜像版本、端口/卷/网络，不带旧前端和闲置供应商依赖 |

源码默认配置：Embedding仍为Google，model仍为Gemini；这些是代码默认，不能当成运行时实际选择。历史个人记录显示曾成功配置DeepSeek和智谱，本轮不输出密钥，也不以历史成功推断所有新接口兼容。

## 3. 两条真实调用链

**入库：** `frontend/client.py:30` → `POST /embeddings/documents` → `routes/embeddings.py:40` → 临时文件 → `EmbeddingManagement` → loader.load_and_split → 智谱等Embedding → QdrantVectorStore.add_texts。当前HYBRID同时产生BM25稀疏表示，并保存原文/metadata。`:89` 的 `asyncio.to_thread` 将同步加载/切分/入库移出事件循环，但没有任务队列或可靠后台作业。

**问答：** `POST /rag/` → `routes/rag.py:23` ainvoke；流式走 `:40` astream_events。图按消息数量选普通/带历史检索 → reranker → `grade_documents` 条件函数 → 生成或改写后重检索 → 返回答案与片段。普通流是NDJSON；受限 `/v1` 兼容接口使用JSON/SSE，不表示在线模型是OpenAI。

`grade_documents`是条件边函数，不是独立注册节点；最多改写两次后仍进入生成，见 `nodes/grading.py:39`。图compile没有checkpointer，源码未发现业务bind_tools/ToolNode/ToolMessage执行器、interrupt或工单表。多条messages可以进入历史检索，不等于后端会话跨请求持久化；前端当前每轮仅发送一条消息，见 `frontend/assistant.py:93`。

## 4. 影响新项目设计的实际问题

| 问题及证据 | 新项目处理时机 |
| --- | --- |
| 上传直接拼接客户端filename（`routes/embeddings.py:68`），全量读取（`:102`），无统一finally临时清理 | 第一版就采用服务端临时名、类型/大小校验和失败清理；不迁入缺陷 |
| 原splitter缺中文标点与空字符串回退；750不是硬上限，本轮已重现超长片段 | 第一版提供字符回退；中文标点/标题结构留作阶段3实验 |
| source降为basename、add_texts未传稳定IDs（`embedding_management.py:55/60`），依赖生成UUID | 文档/chunk稳定身份从基线保留，阶段4验收重复、同名和失败重试 |
| `EmbeddingManagement(None或空字符串)`未赋collection_name，本轮均触发AttributeError | 新工厂校验非空并明确服务端默认集合 |
| TXT搜索无条件取page（`routes/search.py:29`），空结果返回类型不一致 | Schema让page可空，明确空结果契约；本轮未对真实endpoint重现 |
| 带历史分支用凝练问题检索但state.query返回原问题（`nodes/retrieval.py:64`） | 保留original_question与retrieval_query，防后续重排/评分丢上下文 |
| 重试耗尽仍生成，引用仅由提示约束（`grading.py:39`、`utility.py:119`） | 空证据确定反馈、编号验证、无答案与引用支持评测；不能声称编号正确就事实正确 |
| store cache仅collection名，embedding cache仅provider/model（`vdb.py:87`、`retriever.py:16`） | 配置变更不复用旧客户端/索引；模型、维度、URL和版本身份明确 |
| 默认 `Qdrant/bm25` 安装实现按English处理，未配置中文分词 | 新Hybrid使用经过验证的中文tokenizer，并保护API名称/错误码 |
| 应用导入时构建图、初始化collection、注册追踪 | 新API统一lifespan；测试导入不连接外部库或触发模型下载 |

安装依赖补充证据：`.venv/lib/python3.13/site-packages/fastembed/sparse/bm25.py:98` 默认language=`english`；其tokenizer按空白切词。`langchain_qdrant/qdrant.py:1047`自动UUID；`:1222`集合维度校验可能通过 `embed_documents(["dummy_text"])`发真实请求。因此“只创建store对象”不一定是零API调用。以上描述限于本次安装版本。

## 5. 容器与数据冲突

原宿主端口为8001、8501、6333、6334、6006、4317、4318；固定容器名qdrant与旧目录 `vector_db/phoenix_data/data/logs` 都不能照搬为共享资源。Qdrant TCP探针仅验证端口连接，不能证明业务读写就绪。

新项目采用独立包/.venv/.env、Compose project=`project-copilot`、自动容器命名、专属网络/卷。候选端口8002、6433、6007、5433，gRPC/collector按实际需要才映射；实施时再检查占用。collection采用 `pc_knowledge_dense_v0`等版本名称，测试库/集合另设。详见计划第6节。

旧Phoenix模板、源码和Compose使用的endpoint不同：模板缺 `/v1/traces`，源码默认4318，Compose覆盖6006；新项目在所选版本上验证一种地址并统一模板，不能仅凭端口名猜可用。旧latest镜像与宽泛依赖不作为新项目复现保证。

## 6. 本轮实际验证记录

### 6.1 隔离单元测试

执行目录为原项目；以下命令在测试导入前mock稀疏模型与Qdrant客户端，禁用字节码和pytest cache，没有新增测试文件：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - <<'PY'
from unittest.mock import patch
import pytest
with patch('langchain_qdrant.FastEmbedSparse'), patch('qdrant_client.QdrantClient'), patch('qdrant_client.AsyncQdrantClient'):
    raise SystemExit(pytest.main([
        'tests/unit_tests/test_vdb.py',
        'tests/unit_tests/test_embedding_management.py',
        'tests/unit_tests/test_reranker.py',
        '-q', '-p', 'no:cacheprovider',
    ]))
PY
```

目标与预期：不初始化真实模型/向量库，检查上游现有工厂、集合分支、metadata处理和排序/截断契约。实际：**20 passed，3 warnings，0.45s**。这些是已有单测，并非个人新增20项；mock结果不证明真实Qdrant、智谱、FlashRank、中文质量或完整问答通过。未做操作系统级网络审计，不能夸称测试框架全面阻断所有网络路径。

### 6.2 切分与参数失败重现

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - <<'PY'
from langchain_text_splitters import RecursiveCharacterTextSplitter
splitter = RecursiveCharacterTextSplitter(
    chunk_size=750, chunk_overlap=200, length_function=len,
    separators=['\n\n', '\n', '.', '!'],
)
for label, text in [
    ('continuous_Chinese', '项目任务进度管理知识检索。' * 100),
    ('single_line_English', 'x' * 1500),
]:
    chunks = splitter.split_text(text)
    print(label, 'input_length=', len(text), 'chunk_lengths=', [len(c) for c in chunks])
from unittest.mock import patch
with patch('langchain_qdrant.FastEmbedSparse'), patch('qdrant_client.QdrantClient'), patch('qdrant_client.AsyncQdrantClient'), patch('agent.backend.services.embedding_management.get_embedding_model'), patch('agent.backend.services.embedding_management.init_vdb'):
    from agent.backend.services.embedding_management import EmbeddingManagement
    for collection in [None, '']:
        try:
            EmbeddingManagement(collection_name=collection)
        except Exception as exc:
            print('collection_name=', repr(collection), type(exc).__name__, str(exc))
PY
```

测试目标：验证切分长度和空集合参数的实际边界；待核验假设是“750字符是否必然上限”。实际：中文输入1300字符输出单片段 `[1300]`；英文输入1500输出 `[1500]`；None/空集合均 `AttributeError`。原因是自定义分隔符耗尽后保留长段，且构造函数只在集合名为真时赋实例属性。这是原实现失败记录，不在本轮修复原仓库。

### 6.3 历史证据与未验证范围

`PERSONAL_CONTRIBUTIONS.md`记录用户此前真实问答、2048维Embedding、持久化重启、来源展示和Trace成功，另有历史 `83 passed, 4 deselected, 1 warning`。这些是历史用户报告，不是本轮复跑；10月8日引用/追踪修改后没有完整回归记录。本轮的20项是小范围隔离测试，也不能替代83项全量测试。

原 `tests/conftest.py:124` 替换集合初始化与追踪，单元/集成测试大量mock；VCR录制涉及Gemini/Cohere，并非DeepSeek/智谱专项验证。`RUN_LIVE_E2E=1`和VCR重录可能经后端触发真实收费请求，本轮未运行。

本轮仅读取过 `.env` 的非敏感配置白名单键，不展示值；未读取或输出真实密钥。未启动Compose，未核验正在运行的容器、端口占用、远程CI、API价格、Embedding长度/批量上限或Rerank硬件效果。智谱官方网页本轮访问失败，因此这些限制明确留待实施时核验，不用旧配置代替官方契约。

原仓库已有源码/文档共136个纳入审计指纹的文件，审计前后SHA-256一致，Git porcelain状态一致。该检查覆盖tracked和未忽略的已有文件，不宣称全面审计所有被忽略的运行时数据。

## 7. 阶段0学习记录与面试问题

本次新增：独立新项目路线、11项源码地图、隔离方案、20日验收与评测协议。修改文件仅为新项目两份文档，Git提交尚未建立；没有业务实现或优化效果可写入简历。

本次理解的关键点：Embedding产生检索向量，不训练对话模型；框架默认HYBRID/RRF不等于个人实现；function_calling解析Grade不等于调用工单工具；Mock验证协作契约，不能证明在线质量；character chunk长度与Embedding批量条数是不同参数。

遇到的问题与应对：系统无python命令→使用python3或原虚拟环境；旧审计快照过时→按当前Git差异核对；新 `.git` 只读且无有效仓库→如实记录为首个开发任务前置；超长切分与空collection→本地重现并设计新实现的边界。此阶段只解释与规划，不改旧仓库。

可练习三个面试问题：

1. **为什么新项目先Dense？** 要先冻结最小检索配置和效果，再通过控制变量判断混合检索和重排是否有增益。
2. **有LangGraph为什么仍不算业务Agent完成？** 旧图按固定检索/评分/改写流程运行，尚没有依据业务中间结果选择知识和工单工具的执行闭环。
3. **为什么测试通过仍不能声称智谱接口可用？** 本轮模型与Qdrant对象被替换，只能证明被断言的代码契约；实际输出维度、供应商参数和链路需少量真实smoke核验。

下一步仍是等待用户确认，再开始独立Git/环境与最小API；审计文档不触发正式开发。
