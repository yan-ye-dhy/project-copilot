# 【Day 4 开发报告】

开发日期：2026-10-13；Git交接、用户手测及学习确认于2026-10-14补记。首版报告写于提交前；用户现已推送代码、完成8个手测环节，并确认已通过ChatGPT学习且能解释关键流程，阶段A/B均完成。以下保留开发期自动记录，与用户手测分别注明执行者和范围。

**1. 今日开发目标**

复用D2 Embedding、D3文档片段，使一个受控文件真实进入独立Qdrant并通过Dense top-k返回原文证据。保留稳定ID/来源/页码，保存仅成功文档的清单；不实现上传、回答生成或Hybrid。

**2. 新增和修改的文件**

新增11个文件、修改9个文件，共20个；运行数据与模型密钥不提交。

| 类型 | 路径 | 主要作用 |
| --- | --- | --- |
| 新增 | `docker-compose.yml` | 固定Qdrant镜像、独立项目/卷及6433回环端口 |
| 新增 | `src/project_copilot/infrastructure/qdrant.py` | 官方SDK薄适配、集合契约、写入/过滤检索及安全异常 |
| 新增 | `src/project_copilot/schemas/retrieval.py` | Dense配置、成功清单与带相关度的证据契约 |
| 新增 | `src/project_copilot/rag/ingestion.py` | 单文件顺序编排、模型/存储绑定及原子完成清单 |
| 新增 | `src/project_copilot/rag/retrieval.py` | 完成文档范围内的Dense检索与身份核验 |
| 新增 | `scripts/dense_index.py` | 单文件ingest/单问题search、显式live开关与JSON |
| 新增 | `tests/test_qdrant.py` | SDK本地与Mock HTTP的适配/集合/响应/资源契约 |
| 新增 | `tests/test_dense_pipeline.py`、`tests/test_dense_cli.py` | 预处理到检索、失败登记、版本过滤及CLI保护 |
| 新增 | `tests/test_qdrant_integration.py` | 显式开启的真实HTTP存储测试，不调用模型 |
| 新增 | `docs/reports/DAY_4.md` | 本报告与学习交接 |
| 修改 | `src/project_copilot/config.py`、`.env.example` | Qdrant地址/集合/超时、清单目录及top-k |
| 修改 | `pyproject.toml`、`uv.lock` | 新项目安装/锁定官方Qdrant客户端及依赖 |
| 修改 | `README.md`、`docs/DEVELOPMENT_PLAN.md`、`docs/DEVELOPMENT_LOG.md` | 可运行能力、当前进度及真实验证记录 |
| 修改 | `docs/STUDY_NOTES.md`、`docs/INTERVIEW_NOTES.md` | 参考关系、调用链和模块复盘问题 |

**3. 核心实现内容**

- `DenseQdrantStore`：创建缺失集合，核对唯一named `dense`/2048维/Cosine/无sparse或multivector；不匹配时拒绝，不重建。入库使用D3 chunk UUID作为Point ID，`wait=True`且确认completed。
- `DenseIndexSpec`：另形成包含模型、维度、向量字段、距离、切分及集合的Dense版本；保留D3原来的切分指纹。`CompletedDocument`/`IndexManifest`只记录已完成文档与具体片段ID。
- `IngestionService`：先加载/切分、验证清单及集合，之后才发Embedding；写入成功后保存清单，失败不返回成功。空文档不请求模型。
- `DenseRetriever`：用同一模型嵌入问题；按成功文档与Dense版本过滤，再核对chunk ID/内容哈希/切分版本，返回原文及score。空清单不访问外部服务。
- `dense_index.py`：两个子命令均需`--live`；缺开关在读取配置/构造客户端前退出2。with关闭两类客户端，不输出密钥或向量。

**4. 关键设计决策**

参考原`src/agent/utils/vdb.py:94–120`的集合创建/Cosine与`utils/retriever.py:22`的top-k接口；原`vdb.py:13`在导入时初始化BM25，52–59使用HYBRID，D4不移植这些副作用。官方SDK负责存储通信，清单、契约与业务编排为新项目自主实现，原项目只读。

客户端锁定1.19.1、服务器固定v1.19.2，同为1.19系列，依据 [客户端发布](https://github.com/qdrant/qdrant-client/releases/tag/v1.19.1)与 [服务器发布](https://github.com/qdrant/qdrant/releases/tag/v1.19.2)。默认版本探测会构造后台请求，因此显式`check_compatibility=False`，集合就绪与兼容性在可见预检中验证；HTTP客户端`trust_env=False`避免本地Qdrant请求经过系统代理。named向量与过滤使用 [集合](https://qdrant.tech/documentation/manage-data/collections/)和 [过滤](https://qdrant.tech/documentation/search/filtering/)接口。

清单原子替换防止半份JSON，不是Qdrant与文件系统事务。当前仅支持顺序单进程入库；清单保存失败时点可能存在，但新文档不进入检索完成范围。重复入库稳定覆盖点但仍有Embedding费用；多进程并发、完整失败恢复及缓存按后续计划实施。更改模型/维度/切分须使用新集合，不能靠更换清单目录冒充旧集合已迁移。

**5. 自动测试结果**

| 实际命令/检查 | 执行者、实际结果与范围 |
| --- | --- |
| `timeout 45s .venv/bin/python -m pytest -q` | Codex：165 passed, 1 skipped in 4.41s；默认跳过真实HTTP测试，包含D1–D3回归 |
| `timeout 45s .venv/bin/python -m pytest -q tests/test_qdrant.py` | 协作代理：34 passed in 0.48s；SDK本地/Mock HTTP，非真实服务器 |
| `.venv/bin/python -m pytest -q tests/test_dense_pipeline.py tests/test_dense_cli.py` | 协作代理：30 passed in 2.16s；假Embedding及SDK本地模式 |
| `PROJECT_COPILOT_RUN_QDRANT_INTEGRATION=1 .venv/bin/python -m pytest -q tests/test_qdrant_integration.py` | Codex：1 passed in 0.76s；真实Docker HTTP1.19.2、假二维向量、原文/页码/过滤/重复点/配置拒绝；随机pc_test_集合已清理 |
| `UV_CACHE_DIR=/tmp/project-copilot-d4-uv-cache UV_PYTHON_DOWNLOADS=never uv sync --locked --offline --check` | Codex：退出0；Resolved61、Checked58、Would make no changes；原有锁定依赖版本未变 |
| `docker compose --env-file .env.example config --quiet`、`up -d qdrant`与HTTP`/readyz` | Codex：配置/启动成功；project-copilot-qdrant-1，6433回环端口；实际版本1.19.2，就绪200 |
| `uv run --frozen python scripts/dense_index.py ingest tests/fixtures/documents/sample.pdf --live` | Codex，经用户明确授权：退出0；一次普通Embedding请求含2个非空片段，2点写入pc_knowledge_dense_v0，清单成功保存；本次0.6295秒 |
| `uv run --frozen python scripts/dense_index.py search "local document loading" --live` | Codex，同一授权的第二次且最后一次模型请求：退出0；1个问题，top_k5返回2条，页0首条score0.71600187、页2为0.44767696；本次0.5442秒 |
| 只读检索已存Point与清单核对 | Codex：两个向量均2048维且数值有限；仅dense/Cosine/无sparse；原文、全部metadata、chunk ID与版本匹配 |
| 文档/源码/范围检查 | Codex：26份Python AST、15份Markdown围栏/表格、71处本地链接和7处锚点、8项报告通过；原35日任务/工时未变；新增11份文本换行/空白检查与git diff --check通过；.env/data仍被忽略 |

开发期两次真实命令使用同一临时uv缓存，正常输出重定向至被Git忽略的`data/d4-auto/`；本地成功清单在`data/manifests/`。该轮授权限2次模型请求，已全部执行，自动重试关闭；没有调用聊天模型。官方Embedding-3标准价0.5元/百万输入tokens，见 [官方定价](https://docs.bigmodel.cn/cn/guide/start/pricing)，1000 tokens预算估算约0.0005元；未采集usage/账单，不将估算当作实付。后续用户手测是另一轮明确说明范围后由本人执行的请求，见下方补记。

初始WSL未启用Docker集成，沙箱也限制socket/uv网络；用户随后开启集成，受限调用改为必要授权执行后成功。备用官方二进制曾下载并核对发行SHA-256，但没有运行，临时下载已清理；最终真实验收使用Docker。首次镜像拉取成功，独立volume保留自动验收的2个样本点，未删除应用数据或接触旧服务。

开发期真实语义检索只验证一个合成PDF及一个问题，不报告Recall/MRR或普遍效果；这些自动结果本身不计作阶段B。

用户手动验收（2026-10-14）：用户主动开启手测，亲自完成8个环节；Codex读取用户输出文件并比较。保护开关退出2；Docker运行、readyz200、仅dense/2048/Cosine集合；真实PDF重入库2片段且清单1文档；精确计数仍2；查询返回页0/2原文、全部metadata及稳定ID，首条相关度0.71589005，另一条0.4474684；临时预期维度3与原清单冲突，安全错误退出1。正常步骤退出0；命令与结果表见开发日志，输出在被Git忽略的`data/manual-d4/`。

用户本轮按事先说明的2次请求范围亲自带live运行：入库含2个片段一次，查询含1个问题一次，分别0.9466秒和0.77秒；没有自动重试。搜索命令第一次拼错重定向目录，在Bash启动脚本之前失败，未增加模型请求。维度冲突由ManifestStore.load拒绝，早于ensure_collection、Embedding及upsert；这不是用户另测了服务端集合维度拒绝。没有用户重新读取全部向量数值、完成评测集或获得检索质量指标的证据；历史165项/HTTP测试未重跑。

用户另行明确确认“已学习，能够解释”，能说明入库/检索主链、成功清单的配置约束，以及Qdrant HTTP与本项目FastAPI的区别，阶段B据此完成。

**6. GitHub 提交信息**

分支`main`；D4开始时的已推送基线为 [f50abd48a21edcc65cca76a46aa4007534467d8d](https://github.com/yan-ye-dhy/project-copilot/commit/f50abd48a21edcc65cca76a46aa4007534467d8d)。用户于2026-10-14亲自提交、推送 [4349c106030649eeeeee92fc747b04664ecb8bd0](https://github.com/yan-ye-dhy/project-copilot/commit/4349c106030649eeeeee92fc747b04664ecb8bd0)（feat(rag): add Qdrant dense indexing and retrieval，20个文件）；Codex核实GitHub main、本地HEAD/origin/main一致且工作区干净，阶段A完成。当前仅验收补记待用户提交/推送，Codex未执行Git写操作。

**7. 后续学习提示**

1. 从CLI ingest到loader/splitter、Embedding、PointStruct、completed和清单的完整调用链。
2. Qdrant集合、named vector、Point ID、payload的职责；2048维和Cosine如何与模型对应。
3. 文档/片段身份、D3切分指纹与D4完整Dense版本为何分开；更换配置为什么要求新集合。
4. 问题向量→版本/完成范围过滤→top-k→原文证据；score为何不是答案置信度。
5. 同步客户端with、Protocol与完成清单的作用；稳定upsert、原子替换和跨存储事务的边界。

**8. 下一步计划**

D4代码Git交接、学习确认及用户手测均已完成；当前待用户亲自提交/推送本次验收记录。下一项为D5 DeepSeek问答、消息模板与引用契约，仅在用户明确要求后开始，仍按90～150分钟及原费用规则推进。
