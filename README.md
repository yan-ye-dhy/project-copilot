# Project Copilot

基于RAG与LangGraph的研发知识及工单协作智能体。当前完成D1最小工程入口：独立Python包、配置读取与 `GET /health`；RAG、模型、数据库和业务Agent将在后续阶段逐步实现。

协作流程见 [长期协作规则](docs/COLLABORATION_RULES.md)：Codex开发、自动验证与提交报告，ChatGPT负责代码/架构教学，用户学习后主动要求Codex开始手动测试。以下命令是自助运行参考，Codex仅在用户要求后单步带练。

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
- [三方协作与8项开发报告规范](docs/COLLABORATION_RULES.md)
- [开发计划](docs/DEVELOPMENT_PLAN.md)
- [参考审计](docs/REFERENCE_AUDIT.md)
- [开发日志](docs/DEVELOPMENT_LOG.md)
- [学习笔记](docs/STUDY_NOTES.md)
- [面试笔记](docs/INTERVIEW_NOTES.md)

工程设计参考 [mfmezger/conversational-agent-langchain](https://github.com/mfmezger/conversational-agent-langchain)。原仓库保持只读；新项目保留独立包、环境与Git历史。本次没有移植原仓库整套入口或业务实现。
