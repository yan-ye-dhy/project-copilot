# EdgeOne 受控预发布部署准备

阶段 1.5 默认保持 **CLOSED**。本轮没有创建云资源、启动隧道或切换真实 STAGING/PUBLIC。用户明确授权后才进入受控预发布；原生前端及新中式视觉保持。

## 控制台填写

| 字段 | 配置 |
| --- | --- |
| 仓库、分支 | 用户授权连接 `yan-ye-dhy/project-copilot`、`main`；连接前确认自动部署行为 |
| 根目录、框架 | 仓库根目录；通用/自定义静态站，按实际选项核实 |
| 构建命令、输出 | `node scripts/build_edgeone.mjs`；`dist`；已有输出会拒绝覆盖，平台须用干净目录 |
| 安装 | 前端没有第三方 npm 依赖，不增加未经审查脚本 |
| 函数 | 根目录 `cloud-functions/api/[[default]].js`，Node.js Cloud Function；浏览器只访问同源 `/api/...` |
| 运行时 | 官方当前 Node.js 20.x；真实平台验证 `getSetCookie()` 与多 Cookie |
| 时限、缓存 | `edgeone.json` 配置120秒，内部代理115秒，长任务提交后GET轮询；API响应不缓存 |
| 域名/HTTPS | 用户确认平台域名、证书、地区与预览有效期，origin同步到后端 |

[Cloud Functions官方文档](https://pages.edgeone.ai/zh/document/cloud-functions)列出6 MB请求/响应体、默认30秒、最长120秒；这不是实际平台验收结果。项目请求上限4 MiB+64 KiB，响应上限低于6 MB；文件最大4 MiB，公开PDF继续503。Quick Tunnel没有SLA、不支持SSE、重启换域名，见[Cloudflare限制](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)。

## 最终变量位置

| 完整变量名 | 位置 | 用途/默认 |
| --- | --- | --- |
| `PROJECT_COPILOT_BACKEND_ORIGIN` | EdgeOne函数服务端 | 实际HTTPS trycloudflare origin，不含路径/查询/端口 |
| `PROJECT_COPILOT_PROXY_SECRET` | EdgeOne函数服务端 | 随机32–128个base64url字符，与后端代理密钥相同 |
| `PROJECT_COPILOT_PUBLIC_PROXY_SECRET` | 私有`.env.public-demo` | 代理校验及域分离的宿主停机证明签名；不发浏览器 |
| `PROJECT_COPILOT_AUTH_PUBLIC_ORIGIN` | 同上 | 实际EdgeOne HTTPS origin，CSRF/Origin绑定 |
| `PROJECT_COPILOT_PUBLIC_POSTGRES_PASSWORD` | 同上 | 新隔离库随机密码，建议base64url，不复用旧项目 |
| `PROJECT_COPILOT_DATABASE_URL` | 同上 | 仅`postgresql+psycopg://project_copilot_public:<URL编码密码>@postgres:5432/project_copilot_public` |
| `PROJECT_COPILOT_DEEPSEEK_API_KEY`、`PROJECT_COPILOT_ZHIPU_API_KEY` | 同上 | 供应商凭据，仅本地后端使用 |
| `PROJECT_COPILOT_AUTH_OPEN_REGISTRATION` | 同上 | false，邀请码注册；准入也拒绝开放注册 |
| `PROJECT_COPILOT_MAX_DAILY_REQUESTS_PER_USER` | 同上 | 5，UTC日 |
| `PROJECT_COPILOT_MAX_DAILY_REQUESTS_GLOBAL` | 同上 | 30，UTC日 |
| `PROJECT_COPILOT_MAX_CONCURRENT_REQUESTS_PER_USER` | 同上 | 1，原子预留 |
| `PROJECT_COPILOT_MAX_PROVIDER_ATTEMPTS_PER_JOB` | 同上 | 16，区别逻辑任务、请求发起、usage和账单 |
| `PROJECT_COPILOT_PUBLIC_JOB_QUEUE_TTL_SECONDS` | 同上 | 86400，未领取附件上限；停机期间不自动清理 |
| `PROJECT_COPILOT_PUBLIC_UPLOAD_MAX_CHUNKS`、`PROJECT_COPILOT_PUBLIC_UPLOAD_MAX_CHARS` | 同上 | 64、32000 |
| `PROJECT_COPILOT_MODEL_CACHE_HOST_DIR` | 同上 | 固定版本BGE目录，只读挂载 |
| `PROJECT_COPILOT_RELEASE_EVIDENCE_HOST_DIR` | 同上 | 私有验收工件目录，权限700，只读挂到/app/release |
| `PROJECT_COPILOT_TRACING_ENABLED` | 同上 | 默认false，Phoenix只在内网，启用前验脱敏 |
| `PROJECT_COPILOT_PUBLIC_API_IMAGE` | 同上，可选 | 本机已审核镜像；默认由Compose构建 |
| `PROJECT_COPILOT_PUBLIC_RESOURCE_ID` | 后端固定默认身份 | public-demo-v2；不是开放开关，变更需重验 |
| 环境、内部地址、CPU配置 | public-demo Compose固定 | production/public_demo、内部Qdrant/Phoenix、CPU float32、离线模型 |

变量不放前端构建环境；不提交实际值、Tunnel域名、Cookie、权重或备份。私有文件不能含未核实的插值，预检拒绝与宿主导出变量冲突，防止校验文件与实际部署不一致。

## 发布状态与证据

普通环境变量不能开放服务。交互式`scripts.release_control`写入PostgreSQL的CLOSED/STAGING/PUBLIC、独立Worker开关和不可变审计。新库CLOSED：正确代理密钥503，错误403；已接纳操作可能收尾，关闭还须停入口和进程。

STAGING要求用户主动授权、管理员密码、1–20个受限用户UUID、站点/资源/后端代码指纹及验收工件。有效期最长24h；PUBLIC必须先经过STAGING并增加真实公网证据，再次授权。认证、CSRF、归属、配额、依赖健康始终执行。

证据JSON字段：`format=1`、`state`、`resource_id`、`origin`、`code_fingerprint`、`configuration_fingerprint`、最近7天内`verified_at`、未来最多24h的`expires_at`、`allowed_user_ids`及`checks`。本人在容器内运行`inspect`取得当前代码与安全配置指纹；配额、有效期、上传预算或模型运行配置变化后会关闭准入，必须重验。每条check为容器可读绝对`path`和实际`sha256`，以只读挂载交付，库只存摘要。凭据本身不存入这些指纹字段；工具验证身份与字节绑定，不能自动判断证据语义，管理员必须核实。

STAGING工件名：identity_isolation、quotas_limits、storage_restore、retention_recovery、proxy_limits、resource_isolation、edge_protection、restricted_access。PUBLIC另需public_e2e、cookies_csrf、offline_shutdown。

用户先在实际控制台配置并实测[入口保护](EDGEONE_SECURITY_RULES.md)，核对函数、服务端变量、HTTPS、Cookie和非白名单拒绝。第二阶段的真实网络验收记录不可由本地模拟替代。命令、维护和紧急关闭见[运行手册](PUBLIC_DEMO_RUNBOOK.md)，状态见[发布清单](RELEASE_CHECKLIST.md)。
