# 公开演示运维准备

**不要启动公网服务或隧道。** 当前配置仍处于开发与安全验证阶段：公网后端硬闸返回 503，异步上传代码尚未对公网开放，真实 EdgeOne/Tunnel 连通尚未测试。`scripts/start_public_demo.sh` 是会返回非零状态的安全占位入口，不创建容器或资源。

## 隔离边界

`docker-compose.public-demo.yml` 使用独立 Compose 项目名 `project-copilot-public-demo`、独立 PostgreSQL/Qdrant/Phoenix/清单命名卷，不挂载现有业务数据库或 Qdrant 卷。PostgreSQL、Qdrant、Phoenix 均未发布宿主端口，仅在内部 `backend` 网络；Cloudflare 容器只在手动选择的 `tunnel` profile 中，并且只能连接 API 所在的 egress 网络。API 不发布宿主端口；原有 `start.sh` 及本地开发 Compose 不被修改。

Cloudflare 镜像当前使用未固定摘要的 `latest`，仅作待审查占位；取得授权后仍需确认官方镜像和不可变摘要，测试后才能启动。Compose 配置的语法检查不代表镜像存在、服务就绪或平台链路可用。

这一隔离堆栈初始没有已有知识和工单，不能自动继承现有业务数据；本阶段不会创建或迁移这些卷，也不会自动从旧卷复制。模型缓存按 `PROJECT_COPILOT_MODEL_CACHE_HOST_DIR` 只读挂载且不自动下载。示例当前使用 CPU/float32，GPU 需要宿主 NVIDIA 驱动、Docker GPU 支持、匹配的镜像及进一步验证；不能仅因主机 GPU 可用就宣称容器 GPU 可用。

`public-demo.env.example` 不含密钥；今后如获授权，可复制为仅本机保存的 `.env.public-demo` 并填写随机数据库密码、与之相符的 SQLAlchemy URL、代理共享密钥、实际站点 HTTPS origin 和模型 API Key。URL 中密码必须经过 URL 编码。**不要提交实际 `.env.public-demo`，也不要在聊天或日志中展示。** 调试 Compose 时不要打印展开后的完整配置，因为其中可能包含密钥。

Compose 包含用于领取任务的 `worker`。当前代码中的管理员 TXT/Markdown 上传先提交任务：文件限制 4 MiB、multipart 限制 4 MiB + 64 KiB；默认最多 64 个片段、32,000 字符。公开 PDF 上传目前返回 503，需先完成隔离解析的资源安全验收；本机 PDF 功能不受此公开模式限制。逻辑请求默认每用户每日 5 次、全站每日 30 次、每用户并发 1 次；单任务默认最多 16 次供应商请求发起。请求发起次数、供应商实际收到次数、返回 token usage 和实际账单不等同，真实费用未取得账单前为未知。

未领取的任务附件保存在隔离 PostgreSQL；领取事务提交后删除附件行。worker 运行时默认清理超过 24 小时仍未领取的任务及附件；worker 停止期间不执行清理。强制杀死进程可能留下上传临时文件，数据库日志或备份也可能保留历史字节。发布前需完成保留与受控清理运维方案；不要自行删除现有数据库或卷。后台捕获到不确定结果时标为 `unknown` 并占用并发额度，进程被强制终止时可能停留在 `running`；已领取任务不自动重试，也不要为了恢复直接重复提交。

## 关闭与故障排查

今后若已获授权启动隔离演示，可使用：

```bash
./scripts/stop_public_demo.sh
```

脚本只选择带 `com.docker.compose.project=project-copilot-public-demo` 标签的运行中容器，未找到时不会影响其他服务；Docker 不可用时明确报错。它不删除容器或卷，不执行 `down -v`。检查实际状态时，应核对该项目名和容器健康状态；不要把当前本地 `project-copilot` 项目的服务误当作公开演示容器。

以下情况均不能视为上线成功：公开探针返回 503、浏览器无法登录、模型缓存缺失、数据库迁移失败、隧道地址更换但函数变量未更新、平台预览链接过期或代理发生 502/504。检查顺序为：先核对公网硬闸和发布授权状态，再看对应容器状态、后端脱敏日志及平台函数配置；不要输出密码、Cookie、模型密钥或代理密钥。出现未知工单确认结果时先查询实际状态，不自动重复确认。

完整发布流程只有在安全验收及用户明确授权后才能补齐并执行。当前未创建云资源、未启动 Quick Tunnel，也未验证公网访问。
