# 【Day 3 开发报告】

日期：2026-10-12。TXT/Markdown/PDF加载、递归切分和离线查看器已实现；101项自动测试通过。本报告为提交前交接：阶段A待用户亲自提交/推送，阶段B待ChatGPT学习和用户主动开启手测。实际过程见 [开发日志](../DEVELOPMENT_LOG.md)。

**1. 今日开发目标**

把本地文档转成可核对原文、来源、页码与稳定身份的片段。采用750字符上限、150字符目标重叠，支持无空格中文的字符回退；空文档有明确结果。今天只完成文档预处理与检查，不执行Embedding、Qdrant入库、上传接口或模型请求。

**2. 新增和修改的文件**

共新增14个文件、修改7个文件（21个）；2026-10-13交接时补充PDF二进制属性。包入口只说明职责，不创建客户端或执行加载。

| 类型 | 路径 | 作用 |
| --- | --- | --- |
| 新增 | `.gitattributes` | PDF按二进制保存、比较和合并，避免定长xref行被按普通文本检查或转换换行 |
| 新增 | `src/project_copilot/rag/__init__.py`、`src/project_copilot/rag/loaders.py`、`src/project_copilot/rag/splitters.py` | 文档预处理包、单文件读取与递归切分 |
| 新增 | `src/project_copilot/schemas/__init__.py`、`src/project_copilot/schemas/documents.py` | 原文件、逐页文本、片段与metadata契约 |
| 新增 | `scripts/inspect_document.py` | 离线打印原文、切分配置、片段及JSON摘要 |
| 新增 | `tests/test_documents.py`、`tests/test_document_cli.py` | 真实小文件、身份、中文边界、CLI与无模型调用验收 |
| 新增 | `tests/fixtures/documents/sample.txt`、`sample.md`、`sample.pdf`、`README.md` | 自行编写的三种合成样本及来源说明；均位于该目录 |
| 新增 | `docs/reports/DAY_3.md` | 本报告与学习交接 |
| 修改 | `pyproject.toml`、`uv.lock` | 新项目独立安装并锁定切分器、PDF解析器及其依赖 |
| 修改 | `README.md`、`docs/DEVELOPMENT_PLAN.md`、`docs/DEVELOPMENT_LOG.md` | 可运行能力、D3实施调整与实际进度 |
| 修改 | `docs/STUDY_NOTES.md`、`docs/INTERVIEW_NOTES.md` | 原源码参考关系、运行链和模块面试要点 |

**3. 核心实现内容**

- `load_document()`：一次读取原文件字节，计算SHA-256和内容寻址的UUID5文档身份，再从同一份字节解析。TXT/MD显式UTF-8；PDF按页提取文字层，保留0起始页索引和空白页。
- `DocumentPage`、`LoadedDocument`、`ChunkMetadata`、`DocumentChunk`：原文件身份与解析文本分开保存；TXT/MD的`page=None`，PDF来源页码不因空白页而变化。
- `SplitConfig` / `split_document()`：默认750/150，按段落、换行、空格、最终字符回退递归切分，不跨PDF页合并。拒绝非法边界配置；空白内容不生成片段，片段不超过当前字符上限。
- 稳定片段ID：UUID5包含文档身份、切分指纹、页号、片段顺序和文本指纹。文件改名不改变内容身份，同名不同内容及重复位置有不同身份；切分配置变更会更新片段ID。
- `inspect_document.py`：输出原文、metadata、片段数和最大字符数；无文本时`status=empty`、退出0，路径/配置/解析错误时安全JSON、退出1。

**4. 关键设计决策**

参考原 `src/agent/backend/services/embedding_management.py:39–60` 的TXT/PDF读取与递归切分思路；旧逻辑加载后直接入库，切分为750/200且没有最终字符回退，还把来源降为文件名。新项目自主分离读取、切分和数据契约，遵循现有基线750/150，补UTF-8 Markdown、字符回退和稳定身份；原项目始终只读。

实施中核验到官方 `langchain-community` 已停止维护并归档，因此调整“复用原TXT/PDF loader”的具体依赖选择：使用标准库UTF-8解码和`pypdfium2`的薄读取适配，保留成熟的`RecursiveCharacterTextSplitter`。不引入community/classic的广泛依赖。官方依据与API入口：[community维护公告](https://github.com/langchain-ai/langchain-community/issues/674)、[递归切分器](https://docs.langchain.com/oss/python/integrations/splitters/recursive_text_splitter)、[PDFium Python API](https://pypdfium2.readthedocs.io/en/stable/python_api.html)。

实际锁定`langchain-text-splitters`1.1.3、其传递依赖`langchain-core`1.6.9、`pypdfium2`5.14.0。PDFium调用采用模块锁和显式资源关闭；PDF只读取文字层，不执行OCR。TXT/MD保留原文，PDF统一换行。150是目标重叠，按段落边界切分时不保证每对片段恰好重叠150字符。`index_version`在D3只是切分实现/配置指纹，后续索引仍须记录模型与向量维度。

**5. 自动测试结果**

| 实际命令/检查 | 执行者与结果 |
| --- | --- |
| `timeout 45s .venv/bin/python -m pytest -q` | Codex，101 passed in 2.63s；包含D1/D2回归、D3文件与CLI测试 |
| `.venv/bin/python -m pytest -q tests/test_documents.py` | 协作代理，27 passed in 0.12s；属于自动验证，不是用户手测 |
| `UV_CACHE_DIR=/tmp/project-copilot-d3-uv-cache UV_PYTHON_DOWNLOADS=never uv sync --locked --offline --check` | Codex，退出0；Resolved 52 packages、Checked 50 packages、Would make no changes |
| 实际加载三种合成样本并核对片段原文、页码和字符长度 | Codex：TXT为6片段，长度96/10/750/750/271/38；MD为5片段，长度178/750/750/151/55；PDF加载页0/1/2，非空片段位于页0/2，长度49/62 |
| 文档、源码和提交范围初检（2026-10-12） | Codex：17份Python AST、14份Markdown围栏、61处本地链接、报告8项通过；当时`git diff --check`仅覆盖已跟踪差异，未覆盖新增PDF；.env仍被忽略且未跟踪，原有锁定依赖版本未改变 |

2026-10-13用户暂存后，`git diff --cached --check`发现合成PDF的xref定长行尾空格被当作普通文本报错。补充`.gitattributes`的`*.pdf binary`规则，保留原PDF字节；当前范围为21个文件，待用户补暂存该规则和报告/日志后复核。此次仅修改Git属性与记录，业务源码和PDF内容未变，未重复模型调用或业务测试。

测试覆盖无空格中文的长度、重叠和原文重构；改名/重跑/跨进程身份稳定；同名不同内容与重复片段区分；改变切分配置更新ID；空TXT/MD/PDF、真实空白页、非法UTF-8、损坏PDF、错误路径与参数。CLI测试还阻止Settings读取、模型客户端创建和HTTP请求，证明离线检查独立于模型配置。本次没有收费模型调用；未将这些助手验证登记为用户学习或手测。

**6. GitHub 提交信息**

当前分支`main`；D3开始前，用户已推送的D2验收基线为 [1602f5fdb0028b2525fb8b33d07758d0cbc1ef2c](https://github.com/yan-ye-dhy/project-copilot/commit/1602f5fdb0028b2525fb8b33d07758d0cbc1ef2c)。D3增量待用户亲自暂存、commit和push，暂无D3已同步提交链接。Codex未执行Git写操作；之后依据用户实际输出补全会话交接，不伪造提交ID或推送结果。

**7. 后续学习提示**

1. 文件字节、逐页解析文本、DocumentChunk各承担什么职责，为什么先加载再切分。
2. 递归切分如何选择边界，`""`为什么能限制无空格中文长度；字符上限与D2的batch条数有什么区别。
3. SHA-256、UUID5、`document_id`与`chunk_id`分别如何形成稳定身份，哪些变化会让ID改变。
4. PDF页索引、空白页、文字层与OCR的区别，为什么不能给TXT/MD伪造页码。
5. 从`uv run`到查看器、loader、schema、splitter、JSON的完整运行链；本地测试证明什么、D4还需验证什么。

**8. 下一步计划**

先审核本次范围并指导用户亲自提交、推送。随后用户通过ChatGPT学习代码；主动提出“开始今天的手动测试”后，逐环节观察三种原文/片段、中文长度和稳定ID，记录实际反馈及理解确认。D3阶段A/B完成且用户明确要求后，才进入D4独立Qdrant单文件Dense索引与检索；本日不提前执行。
