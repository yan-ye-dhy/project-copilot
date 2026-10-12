# 【Day 2 开发报告】

日期：2026-10-12。客户端开发、离线验证与单条真实Embedding验收完成；阶段A待用户Git交接，阶段B待学习与手测。开发记录见 [开发日志](../DEVELOPMENT_LOG.md)。

**1. 今日开发目标**

实现智谱embedding-3最小客户端，验证原文本/float请求、分批、响应向量及异常契约；提供显式开启的单文本真实smoke脚本。68项离线测试通过，另由一次真实调用确认当前账号请求成功、向量2048维；包含float参数的请求已通过响应校验。D1已完成全部交接；D2不扩展到Qdrant、文档切分、问答API或Agent。

**2. 新增和修改的文件**

| 类型 | 路径 | 作用 |
| --- | --- | --- |
| 新增 | `src/project_copilot/infrastructure/__init__.py`、`src/project_copilot/infrastructure/models.py` | 无导入初始化的外部适配包，智谱客户端工厂、向量校验、脱敏错误与关闭 |
| 新增 | `scripts/embedding_smoke.py` | 仅--live时发一条固定中文请求，打印验证摘要 |
| 新增 | `tests/test_models.py` | 真实SDK配MockTransport的请求/响应契约 |
| 新增 | `tests/test_main.py` | 无key的生命周期和健康接口回归 |
| 新增 | `tests/test_embedding_smoke.py` | CLI默认不调用模型、缺key不构造客户端 |
| 新增 | `docs/reports/DAY_2.md` | 本报告与真实验收待办 |
| 修改 | `src/project_copilot/config.py`、`.env.example` | 可选SecretStr密钥、模型/URL/timeout/batch/预期维度配置 |
| 修改 | `pyproject.toml`、`uv.lock` | 显式SDK/HTTP依赖与pytest开发依赖，锁定实际安装版本 |
| 修改 | `README.md`、`docs/DEVELOPMENT_PLAN.md`、`docs/DEVELOPMENT_LOG.md` | 当前能力、D1完成确认、D2进度和验证边界 |
| 修改 | `docs/STUDY_NOTES.md`、`docs/INTERVIEW_NOTES.md`、`docs/reports/DAY_1.md` | 学习交接、面试依据和D1最终Git结果 |

本地`.env`从新项目模板创建，真实密钥由用户填写；该文件被Git忽略，不属于提交文件。

**3. 核心实现内容**

- `create_embedding_client()`：显式key/base URL/model/timeout，SDK重试为0；缺智谱key不借用OPENAI_API_KEY。
- `ZhipuEmbeddings.embed_query()` / `embed_documents()`：先验证全部输入，再按最多64条分批发送原文本数组与encoding_format=float。
- 原始JSON校验：模型、数量、唯一合法index、输入顺序、预期维度、有限数值；拒绝bool、数字字符串、NaN/Inf及畸形响应。
- `EmbeddingConfigError` / `EmbeddingRequestError` / `EmbeddingResponseError`：区分配置、HTTP/连接与响应错误，对外消息不拼接原始供应商body。
- with/close管理资源；配置及客户端创建不放进main导入或健康请求。脚本显式--live后只执行一条固定文本，无自动重试。

**4. 关键设计决策**

原 `src/agent/utils/embeddings.py:32` 使用LangChain OpenAIEmbeddings，check_embedding_ctx_length=False、float、chunk_size64。新项目保留这些请求原则，直接使用兼容SDK检查原始JSON；index/数量/维度校验、错误和生命周期由新项目设计。SDK原始响应模式避免类型转换掩盖错误，不复制闲置供应商分支。

新增openai和httpx作为运行依赖、pytest作为开发依赖，实际锁定OpenAI2.54.0、HTTPX0.28.1与pytest8.4.2。密钥可选，让已有/health保持独立；expected_dimensions仅本地校验，未向API发送dimensions。默认20秒是网络操作超时，SDKmax_retries=0便于控制调用次数。

已核实的官方来源：[文本嵌入API](https://docs.bigmodel.cn/api-reference/%E6%A8%A1%E5%9E%8B-api/%E6%96%87%E6%9C%AC%E5%B5%8C%E5%85%A5.md)、[Embedding-3](https://docs.bigmodel.cn/cn/guide/models/embedding/embedding-3.md)、[OpenAI兼容指南](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction.md)、[错误码](https://docs.bigmodel.cn/cn/api/api-code.md)、[定价](https://docs.bigmodel.cn/cn/guide/start/pricing.md)。当前API为每批最多64条、每条3072 tokens、默认2048维；模型概览的8K描述不替代具体API限制。未添加本地供应商tokenizer，长文本由后续切分及API错误处理约束。当前schema未列encoding_format字段，本次实调包含该兼容参数并成功返回数值向量。

**5. 自动测试结果**

| 实际命令 | 结果 |
| --- | --- |
| `timeout 45s .venv/bin/python -m pytest -q tests/test_models.py tests/test_embedding_smoke.py` | 首轮63 passed in 1.91s |
| `timeout 25s .venv/bin/python -m pytest -vv -s -o faulthandler_timeout=10 tests/test_main.py` | TestClient后台portal线程等待，退出124；据诊断改用同进程ASGITransport并显式进入lifespan |
| `timeout 45s .venv/bin/python -m pytest -q` | 最终68 passed in 1.35s；没有跳过健康回归 |
| `OPENAI_LOG= timeout 35s .venv/bin/python scripts/embedding_smoke.py --live` | 一次真实请求：status=ok，model=embedding-3，texts=1，dimensions=2048，encoding_format=float |
| `UV_CACHE_DIR=/tmp/project-copilot-d2-uv-cache UV_PYTHON_DOWNLOADS=never uv sync --locked --offline --check` | Resolved 30 packages；Checked 29 packages；Would make no changes |

pytest使用假key、MockTransport或进程内ASGI，另有单条真实smoke，两类证据分别记录，均不计作用户手测。离线覆盖序列化、中文保留、65条分成64+1、输入拒绝、数量/index/维度/有限数值、畸形JSON、401/429/500、超时/连接、错误展示脱敏与close。CLI保护和无key的/health也已验证；没有扩大声称验证了所有SDK调试日志。

真实smoke使用用户保存在新项目的密钥、一条固定中文和单次请求，返回1条2048维向量，with关闭客户端。未打印密钥或完整向量；官方标准价0.5元/百万tokens，实际按账号用量计费，未读取账单或编造本次金额；本次未进行批量索引/评测。

最终文档/提交范围检查：`git diff --check`通过；11份Markdown围栏闭合、51处本地链接目标存在、8项报告完整；9份Python文件AST可解析。17个拟提交变更文件未包含真实key，.env未跟踪且被忽略；Git索引仍无暂存改动。

**6. GitHub 提交信息**

分支main，origin为 [yan-ye-dhy/project-copilot](https://github.com/yan-ye-dhy/project-copilot)。当前已同步基线为D1提交 [4c04a29d0dff34c19660958ecd2111d1cafe2d3b](https://github.com/yan-ye-dhy/project-copilot/commit/4c04a29d0dff34c19660958ecd2111d1cafe2d3b)。D2新增改动尚未暂存、提交或推送，**待用户亲自操作**，暂无D2 commit ID或已同步链接；实际结果在用户操作后补充。

**7. 后续学习提示**

1. 原文本请求、Embedding向量、batch条数与文档字符切分的区别。
2. SDK客户端工厂、SecretStr和显式配置如何避免错误密钥回退。
3. 原始JSON、index配对、维度和有限数值为何都需要校验。
4. 超时、限流、认证失败和HTTP200畸形响应分别如何处理。
5. 真实SDK+MockTransport能验证什么，真实供应商smoke与手动学习测试如何分别记录。

**8. 下一步计划**

用户在Codex逐步指导下完成Git提交/推送，然后通过ChatGPT学习新增代码与测试。学习后仅在用户主动要求时开始手动测试。D2的A/B都完成且用户明确要求后，才进入D3文本加载与切分；Qdrant仍留D4。
