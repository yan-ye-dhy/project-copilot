# 公网演示改造：阶段进度与验收记录

此页保留初版改造的历史验证记录。后续阶段1.5已更新发布控制、运维和验收状态，以[阶段1.5报告](STAGE15_REPORT.md)及[发布清单](RELEASE_CHECKLIST.md)为准。

## 1. 目标与状态

在既有本地工作台上增加新中式界面、账户、多用户隔离、受控任务和 EdgeOne 同源代理。当前为**本地开发及验证阶段，尚未达到发布条件**：公网网关仍对有效代理请求返回 503，启动脚本也主动拒绝启动。没有创建隧道、云资源或发布站点。

## 2. 文件与职责

- `src/project_copilot/web/`：响应式界面、登录/注册、受控任务轮询与来源展示；设计预览见 [桌面](evidence/PUBLIC_WEB_DESKTOP.png)和[移动端](evidence/PUBLIC_WEB_MOBILE.png)。
- `api/auth.py`、`services/auth.py`、`services/auth_rate.py`：Argon2id、服务端会话、邀请注册、CSRF、角色与数据库限流；`scripts/bootstrap_admin.py` 为交互式管理员初始化入口。
- `db/models.py`、`db/jobs.py`、`migrations/versions/0003_accounts.py` 至 `0009_ai_attempts.py`：账户、资源归属、限额、任务和请求审计的增量迁移；历史业务记录保持无归属。
- `api/jobs.py`、`services/quotas.py`、`services/ai_worker.py`、`scripts/ai_worker.py`：原子预留、幂等、任务领取、长任务轮询和失败关闭；`infrastructure/call_accounting.py` 记录上游请求发起次数。
- `rag/deletion.py`、`rag/index_lock.py` 等：清单、Qdrant 与 BM25 的删除恢复；`rag/pdf_sandbox.py` 与 `rag/pdf_worker.py` 提供资源受限解析研究，但公开 PDF 入口仍关闭。
- `scripts/build_edgeone.mjs`、`cloud-functions/`、`edgeone.json`：静态构建及受限同源代理；`docker-compose.public-demo.yml`、`public-demo.env.example` 和启停脚本是独立部署准备。

## 3. 关键流程和限制

浏览器使用 HttpOnly Session 和内存 CSRF 值；后端从会话确定身份，在数据库查询、Agent 工具和提案确认处检查归属。公开模式的 AI 请求提交后台任务，通过 PostgreSQL 原子执行用户、全站及并发限额，再由 worker 最多领取一次。上传限管理员，当前公开任务仅接受 TXT/Markdown；公开 PDF 请求返回 503，本机 PDF 保持原路径。默认上传 4 MiB、64 片段、32,000 字符；每个任务最多 16 次已登记的供应商发起。一次逻辑任务不等于一次供应商请求。

已领取任务不自动重跑；上游或线程终止情况不明时标为 `unknown`，强制退出也可能留下 `running`，继续占据并发槽。上传附件在领取事务提交后删除；worker 运行时会清理默认超过 24 小时仍未领取的任务及附件，worker 停止期间不执行清理。强制终止还可能留下临时文件或数据库日志历史字节。当前 PDF 子进程有 CPU、内存、页数、字符和时长限制，但仍与父进程同 UID，不构成完整操作系统安全沙箱。

已认证且进入任务处理阶段的接受、幂等复用、限额拒绝和部分校验失败分别持久化记录；认证前或框架校验阶段的失败等不在此表内，不能用作全部 HTTP 请求的分母。这些记录不包含问题、文件内容或凭据，也不能替代供应商账单。审计写入默认限制为全站每分钟 60 次、每账户每分钟 10 次；正常审计活动会分批清理超过 7 天的记录。限额内外的恶意请求仍会造成数据库锁和写入压力，无活动时清理也不会运行，公开前仍需外层限流及运维复核。

## 4. 自动验证

- 启用独立 PostgreSQL 的最终全量 `PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 .venv/bin/python -m pytest -q`：**1515 passed、8 skipped、0 failed**；90 条警告为 Starlette 测试客户端逐请求 Cookie 参数的弃用提示。跳过的项目不视为通过。
- 本地 Chromium、前端客户端、静态构建及受控代理：**70 项通过**，所有业务流量均由测试替身拦截，不代表真实公网访问。
- 独立 PostgreSQL 与独立 HTTP Qdrant 上，合成文档的后台入库、清单/BM25 同步、删除及中断恢复：**2 项通过**；Embedding 使用受控替身，不代表真实模型调用。
- 最新上传接收、权限、配额、提案及 worker 安全组合测试：**58 项通过**；最新 worker、脚本、PDF 和既有入库回归：**84 项通过**。不同组合存在测试重叠，不应相加当作独立样本。
- 独立 PostgreSQL 上的迁移、历史资源归属、审计并发限流与保留清理组合测试：**22 项通过**。审计限流仍不能代替公网入口处的防滥用保护。
- `git diff --check` 通过；已知 Starlette 对测试中逐请求 cookies 参数给出弃用警告。

## 5. 容器验证

CPU 测试镜像 `project-copilot:web-validation` 已构建，使用无网络、无数据卷临时容器验证模块可导入及所检查的环境文件/运行清单未进入镜像。独立 Compose 测试项目使用随机合成凭据和独立卷：PostgreSQL、Qdrant、Phoenix 健康，数据库迁移成功；隔离 API 无代理密钥返回 403、有正确密钥仍返回 503。合成工单和向量在各自容器重启后仍存在。发现 worker 停止信号问题后已修复，独立 worker 再次启动和停止，退出码为 0。验证没有访问原业务数据库或既有 Qdrant 集合。

## 6. GitHub 与费用

本报告随代码交付，具体提交及远端同步状态以 Git 记录为准。本轮测试没有调用付费模型；使用 SDK MockTransport 和合成 Embedding 的结果不能写成真实模型成功或实际账单。实际计费仍以供应商账单为准。

## 7. 未完成与发布边界

公开 PDF 仍缺少与主进程权限真正隔离的解析环境；默认 Docker 测试容器内尝试创建用户及网络命名空间返回 `Operation not permitted`。外层请求限流、审计静默期清理、后台停止期间的附件保留、数据库日志及异常任务的人工核查/恢复仍需完善。实际 EdgeOne → Cloudflare Tunnel → 后端、Cookie、超时及完整业务链未测试，且必须先得到用户对云资源、隧道和发布的明确授权。Quick Tunnel 不支持 SSE；本项目当前没有需代理的已验收 SSE 路由。

## 8. 后续工作

保持公开安全闸关闭，完善 PDF 安全边界、附件保留与未知任务恢复的运维方案后再执行安全复核。随后由用户明确授权并参与 EdgeOne/GitHub 授权、密钥配置与最终发布；具体准备见 [EdgeOne 部署](EDGEONE_DEPLOYMENT.md)、[安全模型](SECURITY_MODEL.md)和[运行手册](PUBLIC_DEMO_RUNBOOK.md)。本地自动验证不是用户亲自手测。
