# 【Day 6 开发报告】

日期：2026-10-15。实现、离线验证及最小真实自动验收完成；阶段A待用户Git交接，阶段B学习手测待完成。D1–D5阶段A/B及D5收尾cf5e373已同步；本轮仅用户开启的D6。

## 1. 今日开发目标

接入单文件上传，复用已验收的D3/D4入库与D5问答，完善基本上传校验/清理和本地请求关联。复盘文件→向量→证据→回答；实际Phoenix导出保留到D33。

## 2. 新增和修改的文件

**新增：**

- `src/project_copilot/api/documents.py`：单文件上传、校验/临时清理、忙碌保护和HTTP错误。
- `src/project_copilot/schemas/uploads.py`：上传成功契约。
- `src/project_copilot/infrastructure/tracing.py`：run_id、基本耗时日志与框架body上限；无Phoenix客户端。
- `tests/test_upload_api.py`、`test_upload_pipeline.py`、`test_request_context.py`：上传、失败清理、来源/身份、并发与请求关联验证。
- `docs/evidence/D6_DENSE_SMOKE.json`：自编样本的真实自动验收配置、输出、usage与run_id；不含密钥或完整向量。
- 本报告。

**修改：** `config.py`、`.env.example`增加上传额度/追踪开关；`main.py`注册上传、请求关联及安全500；`rag/ingestion.py`增加可选展示来源名、空文本异常与资源工厂；`pyproject.toml/uv.lock`新增multipart及现有Starlette最低版本约束；README、计划、日志、学习/面试笔记更新进度与教学交接。

## 3. 核心实现内容

- `POST /documents/upload`：只接受一个file，按解析后的名称、后缀/MIME与实际字节检查，再调用原IngestionService。物理路径使用随机临时名，原来源仅进入metadata。
- `ingest_file(path, *, source_name=None)`保持旧CLI默认行为和全部身份/索引指纹；只有写库和成功清单均完成才返回indexed/DENSE。
- 表单与复制文件均清理；上传忙时409，进程内锁覆盖完整入库，避免HTTP多写者覆盖清单；未提供跨进程/CLI并发事务。
- 服务端run_id贯穿request.state、X-Run-ID、上传响应与完成日志。实际body上限为文件额度＋64KiB，业务层另检查文件上限；安全500也返回关联ID。

## 4. 关键设计决策

沿用原项目上传路由→服务/线程池的分层思路，自主实现临时名称、受限复制、清理、成功契约和关联；D3/D4/D5主流程继续复用。multipart解析采用官方要求的python-multipart0.0.32；显式约束现有Starlette≥1.7以使用内置body限制，未新建自定义流解析器或监控平台。

上传校验后才进入原入库链；扩展名/MIME不能证明内容，仍依赖UTF-8/PDF解析。source_name不参与内容/片段身份；同内容改名共享身份。tracing_enabled默认false，当前true明确拒绝，trace_exported始终false；D6不宣称Phoenix已启用。

## 5. 自动测试结果

执行者：Codex，工作目录为新项目；模拟模型与真实模型验收分开记录。

- 原358项离线回归通过，1项外部Qdrant测试默认跳过。
- 新增上传/请求测试首轮47通过、1失败：multipart会把Windows路径规范为basename，原测试误要求HTTP拒绝；已改为验证规范后的安全来源及校验器对未规范化路径的防御，并加入真实重叠请求409用例。
- `timeout 45s .venv/bin/python -m pytest -q`：408通过、1跳过；补上默认应用日志初始化后最终全量仍为**408 passed, 1 skipped，6.11秒**。包括三格式、来源/页/ID、非法内容、模型/写库/清单失败清理、并发409、实际body超限与安全500；假二维向量不代表实际语义效果。
- python-multipart0.0.32安装成功，旧包版本未升级；新项目.env两家配置能加载，只输出布尔状态；Qdrant准备时未运行，Codex启动已有容器后readyz就绪。
- 零模型真实HTTP预检通过：临时Uvicorn启动，health200、不支持文件415、空文件422；三条响应的run_id均能在完成日志中找到，trace_exported=false。未构造入库客户端，临时服务正常关闭，数据保存在被忽略的data/d6-auto中。
- 锁定环境检查59个包无需改动；Python AST、Markdown围栏/表格/本地链接与锚点、JSON、固定8项报告及凭据检查通过，35日任务行不变，git diff --check通过，暂存区为空；收尾共19个提交候选。
- 用户明确批准后执行`timeout 150s .venv/bin/python /tmp/project-copilot-d6-live-check.py`：唯一一次上传与问答均HTTP200，分别约0.9844/1.2227秒；上传2块，回答“样本文档第一页描述了本地文档加载功能。”，引用sample.pdf页0，原文及全部metadata与D3一致。重复上传后Qdrant精确点数2，无临时上传文件残留；两条run_id均可关联真实完成日志，自动启动的Uvicorn正常关闭，已有Qdrant保留。
- 本次仅2次Embedding＋1次DeepSeek，日志核对同数，无自动重试；DeepSeek usage为输入358/输出24/总382，cache_hit128/cache_miss230；Embedding usage及实际账单未采集，事前预算不是实付。可提交的[验收记录](../evidence/D6_DENSE_SMOKE.json)保存完整配置/版本与自编样本输出，临时脚本及运行日志留在/tmp、被忽略的data/d6-auto中。
- Phoenix导出、完整检索效果评测均未执行；单个已知事实不产生准确率/Recall/MRR，用户学习/手测未开始。

## 6. GitHub 提交信息

- 分支：`main`，跟踪`origin/main`；远端为`https://github.com/yan-ye-dhy/project-copilot.git`。
- 开发基点：[cf5e373d7df905e196f9324e66d63cfb2dfe795b](https://github.com/yan-ye-dhy/project-copilot/commit/cf5e373d7df905e196f9324e66d63cfb2dfe795b)，D5学习手测收尾已同步。
- D6 commit：**待用户提交**；推送：**待用户执行**；暂无已同步的D6提交链接。Codex不自动暂存、提交或推送。

## 7. 后续学习提示

交ChatGPT结合 [D6学习笔记](../STUDY_NOTES.md#d6上传接入与dense模块复盘)与真实commit带读：

1. multipart/UploadFile、spool与整体body/文件字节的不同限制。
2. 服务端临时路径、来源名与稳定内容/片段身份。
3. async路由/线程池、with/finally资源与进程内写入保护。
4. run_id的请求/响应/日志联系，以及Phoenix实际导出待办。
5. 文件上传→成功清单→问题检索→消息→结构化回答→真实引用的完整链路；Few-shot格式与任务示例可在本次复盘讨论。

## 8. 下一步计划

指导用户亲自审核、提交并推送D6。之后由ChatGPT教学；用户主动要求D6手动测试后再单步带练。

D7是语料来源与第一批人工gold，不自动开始。完整RAG效果评测、失败恢复和Trace按原35日计划推进。
