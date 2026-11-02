# 公开演示运维准备

**不要启动公网服务或隧道。** 当前配置仍处于开发与安全验证阶段：公网后端硬闸返回 503，异步上传代码尚未对公网开放，真实 EdgeOne/Tunnel 连通尚未测试。`scripts/start_public_demo.sh` 是会返回非零状态的安全占位入口，不创建容器或资源。

## 隔离边界

`docker-compose.public-demo.yml` 使用独立 Compose 项目名 `project-copilot-public-demo`、独立 PostgreSQL/Qdrant/Phoenix/清单命名卷，不挂载现有业务数据库或 Qdrant 卷。PostgreSQL、Qdrant、Phoenix 均未发布宿主端口，仅在内部 `backend` 网络；Cloudflare 容器只在手动选择的 `tunnel` profile 中，并且只能连接 API 所在的 egress 网络。API 不发布宿主端口；原有 `start.sh` 及本地开发 Compose 不被修改。

注意：卷名当前依赖 Compose 项目前缀，`-p` 或 `COMPOSE_PROJECT_NAME` 覆盖可能使其碰撞原项目卷；这是开放前必须整改和演练的阻塞项。不要通过修改项目名或重新建卷猜测解决，也不要对已有卷执行删除或迁移。

Cloudflare 镜像使用固定版本和摘要；取得授权后仍需核对官方发布、系统架构和漏洞状态，并在隔离环境验收。Compose 配置的语法检查不代表镜像存在、服务就绪或平台链路可用。

这一隔离堆栈初始没有已有知识和工单，不能自动继承现有业务数据；本阶段不会创建或迁移这些卷，也不会自动从旧卷复制。模型缓存按 `PROJECT_COPILOT_MODEL_CACHE_HOST_DIR` 只读挂载且不自动下载。示例当前使用 CPU/float32，GPU 需要宿主 NVIDIA 驱动、Docker GPU 支持、匹配的镜像及进一步验证；不能仅因主机 GPU 可用就宣称容器 GPU 可用。

`public-demo.env.example` 不含密钥；今后如获授权，可复制为仅本机保存的 `.env.public-demo` 并填写随机数据库密码、与之相符的 SQLAlchemy URL、代理共享密钥、实际站点 HTTPS origin 和模型 API Key。URL 中密码必须经过 URL 编码。**不要提交实际 `.env.public-demo`，也不要在聊天或日志中展示。** 调试 Compose 时不要打印展开后的完整配置，因为其中可能包含密钥。

Compose 包含用于领取任务的 `worker`。当前代码中的管理员 TXT/Markdown 上传先提交任务：文件限制 4 MiB、multipart 限制 4 MiB + 64 KiB；默认最多 64 个片段、32,000 字符。公开 PDF 上传目前返回 503，需先完成隔离解析的资源安全验收；本机 PDF 功能不受此公开模式限制。逻辑请求默认每用户每日 5 次、全站每日 30 次、每用户并发 1 次；单任务默认最多 16 次供应商请求发起。请求发起次数、供应商实际收到次数、返回 token usage 和实际账单不等同，真实费用未取得账单前为未知。

worker 现在以 API 健康为启动依赖，因此当前 503 硬闸会阻止常规 Compose 启动 worker；该依赖只在启动时检查，API 随后不健康不会自动停止已经运行的 worker，手工 `--no-deps` 也会绕过依赖，禁止以此规避发布门禁。

未领取的任务附件保存在隔离 PostgreSQL；领取事务提交后删除附件行。worker 运行时默认清理超过 24 小时仍未领取的任务及附件；worker 停止期间不执行清理。强制杀死进程可能留下上传临时文件，数据库日志或备份也可能保留历史字节。发布前需完成保留与受控清理运维方案；不要自行删除现有数据库或卷。后台捕获到不确定结果时标为 `unknown` 并占用并发额度，进程被强制终止时可能停留在 `running`；已领取任务不自动重试，也不要为了恢复直接重复提交。

## 关闭与故障排查

今后若已获授权启动隔离演示，可使用：

```bash
./scripts/stop_public_demo.sh
```

脚本只选择带 `com.docker.compose.project=project-copilot-public-demo` 标签的运行中容器，未找到时不会影响其他服务；Docker 不可用时明确报错。它不删除容器或卷，不执行 `down -v`。检查实际状态时，应核对该项目名和容器健康状态；不要把当前本地 `project-copilot` 项目的服务误当作公开演示容器。

以下情况均不能视为上线成功：公开探针返回 503、浏览器无法登录、模型缓存缺失、数据库迁移失败、隧道地址更换但函数变量未更新、平台预览链接过期或代理发生 502/504。检查顺序为：先核对公网硬闸和发布授权状态，再看对应容器状态、后端脱敏日志及平台函数配置；不要输出密码、Cookie、模型密钥或代理密钥。出现未知工单确认结果时先查询实际状态，不自动重复确认。

完整发布流程只有在安全验收及用户明确授权后才能补齐并执行。当前未创建云资源、未启动 Quick Tunnel，也未验证公网访问。

## Windows + WSL 后续部署步骤（只供获授权后的准备）

以下是预期操作顺序，不是当前可用的上线命令：**当前 `start_public_demo.sh` 会拒绝启动，API 健康检查会失败，隧道依赖 API 健康状态，切勿绕过。** 需要先完成 [发布清单](RELEASE_CHECKLIST.md) 中的整改和独立验收，再由用户明确同意第二阶段。

1. Windows 启动 Docker Desktop，启用当前 Ubuntu 的 WSL 集成；在 WSL 进入项目目录，检查 `docker version` 和磁盘空间。不要修改原有 Compose 或其数据卷。
2. 用户在 WSL 本机将 `public-demo.env.example` 复制为 `.env.public-demo`，设置文件权限 `chmod 600 .env.public-demo`，并用编辑器填写：数据库随机密码、与之匹配的内部 PostgreSQL URL、后端代理密钥、站点 HTTPS origin、DeepSeek/智谱密钥和只读 BGE 缓存路径。不要把值发到聊天、写入终端历史或提交 Git。URL 的密码应进行 URL 编码。
3. 用户核对独立 Compose 项目名、命名卷、模型目录、备份及恢复策略；确认不会连接原数据库。预期通过 `docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml` 管理本堆栈；`PROJECT_COPILOT_PUBLIC_ENV_FILE` 未覆盖时应用读取同一文件。
4. 在未来硬闸安全开放方案完成并获得授权后，按届时更新并验收过的脚本启动数据库、Qdrant、Phoenix、迁移、API 和 worker；确认数据库迁移、模型加载和依赖健康。仅在隔离数据库上用交互式 `python -m scripts.bootstrap_admin` 初始化管理员，不通过命令行参数、重定向或日志传递密码。
5. 未来明确授权隧道后，才启用 Compose 的 `tunnel` profile 并在本机私下读取临时 HTTPS origin；将 origin 和共享密钥填入 EdgeOne **服务端**环境变量。隧道重启换地址时更新函数配置并核实部署生效；不要在浏览器或日志中公开隧道地址。
6. 用户在 EdgeOne 导入仓库，按 [控制台配置清单](EDGEONE_DEPLOYMENT.md) 填写构建和函数设置，核对是否自动发布。完成独立用户、真实问答、工单确认、Cookie、限额、超时、断线和关闭后的离线提示验收，再公开分享网址。

在发布门禁整改、用户授权及隔离检查全部完成后，预计使用以下命令；现阶段执行启动会因硬闸导致 worker 和隧道依赖不满足，绝不使用 `--no-deps` 强行绕过：

```bash
cd ~/project-copilot
# 确认没有通过 -p 或 COMPOSE_PROJECT_NAME 把项目名覆盖为已有项目。
docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml \
  up -d --build postgres qdrant phoenix migrate api worker
docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml ps
# 在 API 与数据库已准备好后，以交互方式创建初始管理员。
docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml \
  exec api python -m scripts.bootstrap_admin
# 最后，只有明确授权启用公网隧道后才执行。
docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml \
  --profile tunnel up -d cloudflared
```

获取地址时可在本人终端运行 `docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml logs cloudflared`，仅将其中的 `https://…trycloudflare.com` 私下填入 EdgeOne 服务端变量；不要将完整日志、域名、密钥或 Cookie 粘贴到聊天。实际上线操作必须以届时完成整改并重新验收的脚本为准。

### 故障处理边界

- **API 不健康 / 503**：当前是预期的发布硬闸；检查清单和后端脱敏日志，不要绕过或重启隧道当作修复。
- **迁移或数据库连接失败**：检查隔离项目名、数据库 URL 与密码一致性、容器健康及迁移状态；不要删除卷或指向原数据库。
- **静态页面正常但 `/api` 失败**：确认函数已在实际平台识别、服务端变量已更新、域名及 Origin 匹配，检查隧道和上游状态；不要输出秘密值。
- **`running` / `unknown`**：先停止新增请求，确认 worker 已终止并核对模型供应商和工单实际状态；目前没有经过验收的一键恢复，禁止直接释放配额或重试。
- **停止公开入口**：在获授权的未来部署中运行 `./scripts/stop_public_demo.sh`，先停隧道再停 API、worker 和存储；脚本只针对独立项目标签，不删除卷。保留备份及恢复需单独按用户批准的策略执行。
