# Project Copilot

基于RAG与LangGraph的研发知识及工单协作智能体。D1工程、学习与手测已完成；D2增加智谱Embedding客户端、请求契约测试与单条真实验证脚本，68项离线测试和一次2048维真实请求已通过。D2代码已由用户推送GitHub，三项用户手测符合预期；用户已确认完成ChatGPT学习并能说明调用链；D2阶段A/B均完成。RAG、数据库和业务Agent按后续计划实现。

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

API文档位于 `http://127.0.0.1:8002/docs`。`/health`只表示当前应用能响应，不证明尚未接入的模型、Qdrant或PostgreSQL健康。

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
