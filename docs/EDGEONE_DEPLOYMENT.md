# EdgeOne 受控预发布部署准备

当前main基准 `80302f9`：用户已报告真实来源修复503/503/404；后续顺序以[STAGING交接](STAGING_LAUNCH_HANDOFF.md)为准。既有服务/管理员/卷已准备，不重跑初始化。本轮只准备只读检查和独立静态维护包，**不修改平台配置或部署**。代码门禁要求模型Key非空，当前Key未配置，因此暂不能授权STAGING；[证据工作单](STAGING_EVIDENCE_WORKSHEET.md)区分历史、本地、用户反馈与待真实验收。云端暂停/回退能力边界见[维护方案](STAGING_MAINTENANCE.md)。

默认保持 **CLOSED**。2026-11-05 用户已在真实控制台确认：免费 `edgeone.cool` 默认域名不能配置精准限流和自定义安全规则。本轮采用 Cloud Function 前置校验＋本地 Nginx 统一限流＋数据库业务额度，**仅准备受限 STAGING，不允许该默认域名 PUBLIC**。未启动隧道或切换真实状态；新中式视觉保持。参见[专项报告](DEFAULT_DOMAIN_SECURITY_REPORT.md)。

## 控制台填写

| 字段 | 配置 |
| --- | --- |
| 仓库、分支 | 现有 `main` 会自动生产部署，来源修复PR #4已合并。本轮准备使用独立草稿PR，不合并main；为满足“不触发任何云端部署”，推送前须用户确认预览自动部署已关闭。Node内部URL不证明公网Host；预览写请求仍须固定SITE |
| 根目录、框架 | 仓库根目录；通用/自定义静态站，按实际选项核实 |
| 构建命令、输出 | `node scripts/build_edgeone.mjs`；`dist`；已有输出会拒绝覆盖，平台须用干净目录 |
| 安装 | 前端没有第三方 npm 依赖，不增加未经审查脚本 |
| 函数 | 根目录 `cloud-functions/api/[[default]].js`，Node.js Cloud Function；浏览器只访问同源 `/api/...` |
| 运行时 | 官方当前 Node.js 20.x；真实平台验证 `getSetCookie()` 与多 Cookie |
| 时限、缓存 | `edgeone.json` 配置120秒，内部代理115秒，长任务提交后GET轮询；API响应不缓存 |
| 域名/HTTPS | 用户确认平台域名、证书、地区与预览有效期，origin同步到后端 |

[Cloud Functions官方文档](https://pages.edgeone.ai/zh/document/cloud-functions)列出6 MB请求/响应体、默认30秒、最长120秒；这不是实际平台验收结果。项目非上传 JSON 上限256 KiB，上传正文上限4 MiB+64 KiB，响应上限低于6 MB；文件最大4 MiB，公开PDF继续503。Quick Tunnel没有SLA、不支持SSE、重启换域名，见[Cloudflare限制](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)。

## 最终变量位置

| 完整变量名 | 位置 | 用途/默认 |
| --- | --- | --- |
| `PROJECT_COPILOT_BACKEND_ORIGIN` | EdgeOne函数服务端 | 真正转发前须为本人实际HTTPS trycloudflare origin，不含路径/查询/端口；未启动Tunnel时可留空，仅可验前置拒绝，合法请求503且不读体/联网 |
| `PROJECT_COPILOT_SITE_ORIGIN` | EdgeOne函数服务端 | 新增必填：实际 HTTPS 站点 origin，无尾斜杠；与后端 AUTH_PUBLIC_ORIGIN 完全一致。缺失503；写入或显式Origin不匹配403。内部URL不是公网Origin，GET原Host无法在Node独立证明；不是浏览器密码 |
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

STAGING要求用户主动授权、管理员密码、1–20个已有活跃用户UUID、站点/资源/后端代码指纹及验收工件。通用有效期最长24h，默认edgeone.cool另限2h且禁止PUBLIC；认证、CSRF、归属、配额、依赖健康始终执行。该白名单约束业务路由和任务，login/register/health、仅验证自身身份的me/logout有明确例外，见交接手册。

证据JSON字段：`format=1`、`state`、`resource_id`、`origin`、`code_fingerprint`、`configuration_fingerprint`、最近7天内`verified_at`、未来最多24h的`expires_at`、`allowed_user_ids`及`checks`。本人在容器内运行`inspect`取得当前代码与安全配置指纹；配额、有效期、上传预算或模型运行配置变化后会关闭准入，必须重验。每条check为容器可读绝对`path`和实际`sha256`，以只读挂载交付，库只存摘要。凭据本身不存入这些指纹字段；工具验证身份与字节绑定，不能自动判断证据语义，管理员必须核实。

STAGING工件名：identity_isolation、quotas_limits、storage_restore、retention_recovery、proxy_limits、resource_isolation、edge_protection、restricted_access。PUBLIC另需public_e2e、cookies_csrf、offline_shutdown。

用户先在实际控制台配置并实测[入口保护](EDGEONE_SECURITY_RULES.md)，核对函数、服务端变量、HTTPS、Cookie和非白名单拒绝。第二阶段的真实网络验收记录不可由本地模拟替代。命令、维护和紧急关闭见[运行手册](PUBLIC_DEMO_RUNBOOK.md)，状态见[发布清单](RELEASE_CHECKLIST.md)。

## 默认域名：现有 CLOSED 环境安全升级

本轮没有操作正在运行的演示环境、管理员密码或数据卷。下面命令**供审核后由用户执行**，不是已部署记录。无需重新初始化数据库或创建管理员，不运行 `down -v`。

1. 先审核开发分支/PR。合并 main 会触发 EdgeOne 生产自动部署，必须本人确认；提前在函数服务端准备 `PROJECT_COPILOT_SITE_ORIGIN`，未配置的新代码503是失败关闭。不要把该值或任何密钥通过静态构建注入。部署 origin 变化时，后端 origin、CSRF、证据均须重审。
2. WSL 保持 Docker Desktop 集成，用现有管理员交互执行 `scripts.release_control inspect`，确认 CLOSED / worker_enabled=false。若此前已开放，先用户主动 CLOSED，再停止入口/Worker；本轮未开放。仅备份私有配置与按既有冷备流程保护数据，不改密码。
3. 将审核代码同步到本机；现有 `.env.public-demo` 保留。私有证据目录可读、权限700。执行：

```bash
cd ~/project-copilot
# 只创建新增缺失网络；固定已归属的卷保持原状，不会重建或覆盖。
.venv/bin/python -m scripts.public_resources initialize
.venv/bin/python -m scripts.public_resources check
./scripts/start_public_demo.sh --prepare
```

API镜像会更新代码及只读入口证据文件；新增单个安全入口镜像与专用 `pc-public-v2-ingress` 网络，不发布任何宿主端口。PostgreSQL/Qdrant/Phoenix及原四个卷名字和路径不变；没有新增数据库迁移。新入口 `/health` 仍代理原API准入，CLOSED时unhealthy/503是预期，不降低健康标准。实际就绪/升级错误先查 `docker compose ... logs api ingress`（日志只在本机查看），不要重建卷。

## 默认域名 edge_protection 准入工件

原format1发布文档、所有STAGING检查、管理员密码、AUTHORIZE、资源/代码/配置/origin/身份绑定均保留。`checks.edge_protection` 必须指向管理员已审查的JSON；该JSON字段为：

```json
{
  "format": 1,
  "profile": "default-domain-bounded-v1",
  "origin": "https://<本人实际项目>.edgeone.cool",
  "source_sha256": {"<inspect输出的六个入口文件路径>": "<对应实际SHA256>"},
  "cloud_runtime_artifact": {"path": "/app/release/<实际云端前置验证工件>", "sha256": "<实际文件SHA256>"},
  "local_ingress_artifact": {"path": "/app/release/<已审查本地入口验证工件>", "sha256": "<实际文件SHA256>"},
  "risk_acknowledgement": "Cloud Function invocations remain unprotected; accept only time-bounded restricted STAGING"
}
```

模板是字段说明，不能直接用占位或合成测试文件授权。`release_control inspect` 在新镜像中输出 `entry_source_sha256` 的六个实际文件摘要。云端工件记录实际函数环境、Node运行时、站点origin、无正文读取/无上游调用的非法请求、合法GET离线失败、实际Cookie/上传功能待STAGING的边界；不得写成公网完整验收已通过。两项工件须真实存在、非空、8MiB内，容器路径可读，摘要一致。证据哈希只能验证字节，不能替管理员判断真实性。

**避免授权循环：**先仅部署审核后的函数代码，Tunnel/Worker继续关闭；用无效路径、错误Origin、超大声明Content-Length等受控请求验证真实Node前置拒绝，用合法GET验证目的地未配置时503失败关闭（本机Tunnel未开启）。这不需模型或STAGING。记录函数日志/响应及用量，标明平台是否预缓冲未知；不要把本地模拟当云证据。之后审核本地Nginx结果与函数额度剩余风险，用户显式接受短窗口受限STAGING；最后才获额外授权启动Tunnel、进行Cookie/上传/真实业务端到端验证。

发布文档expires_at最多为授权时刻＋2小时；只允许列出的1–20个已有活跃账号。Worker独立开启仍需主动授权。默认域名的PUBLIC变更及伪造PUBLIC数据库行运行准入均拒绝；不得改域名字符串或伪造证据绕过，未来PUBLIC方案须另行安全审查。

## 函数额度与外部操作

新增唯一函数变量为 `PROJECT_COPILOT_SITE_ORIGIN`。本地无新增必填密钥，Nginx阈值固定在受审配置；原私有数据库、模型、代理密钥保留。Cookie/CSRF由原系统验证，Origin可由脚本伪造，不能当身份。`eo_token`是平台预览访问方式，未经验证不能当API认证。

默认域名没有已证的调用前全局防滥用方案；函数内部403/413/429仍消耗触发/执行资源，Nginx位于函数之后。额度以用户实际套餐及控制台实时说明为准，历史截图不是当前余额或未来价格承诺。用户未找到已验证的一键暂停；不假设平台有自动硬预算、瞬时停用或删除函数路由开关。异常先按已授权流程本地CLOSED并停Tunnel，另按[维护方案](STAGING_MAINTENANCE.md)核实云端可行措施；仅停Tunnel、断Git或停自动部署都不能保护现有函数调用额度。

### Tunnel关闭时的最小真实函数检查

以下仅在用户批准函数部署后本人执行，最多3次请求，不调用模型、不写业务；没有本人Tunnel origin时保持BACKEND_ORIGIN为空，绝不填写随机或他人的地址。`pc_site_origin`填已审核实际origin，结果与函数日志一起保存脱敏工件。配置错误503先核对SITE_ORIGIN和运行环境，不连续重试。

```bash
pc_site_origin='https://<实际项目>.edgeone.cool'
# PATCH不在代理方法/路径白名单，应404；应未连接上游。
curl --silent --show-error --max-time 15 -i -X PATCH "$pc_site_origin/api/jobs"
# 写入Origin不匹配应403；应未读取项目body或连接上游。
curl --silent --show-error --max-time 15 -i "$pc_site_origin/api/jobs" \
  -H 'Origin: https://wrong.example' -H 'Content-Type: application/json' --data '{}'
# 合法GET且BACKEND_ORIGIN未配置应503；只有本人历史合法origin但Tunnel离线时才可能502；不是业务已开放。
curl --silent --show-error --max-time 120 -i "$pc_site_origin/api/health"
```

若平台预览访问先返回401/HTML，先在真实控制台核实默认域名/地区/预览要求，不将它解释为应用认证成功，也不把eo_token当业务身份。多Set-Cookie、真实Session与4MiB端到端上传只能在获授权的完整链路中验证；不要将以上3项称为完整云端验收。请求触发后函数指标可能增加，这正是尚未解决的调用前额度风险。

## 最终来源边界：运行时 URL 只用于路由

用户确认部署PR #3后仍403；此前日志已证明Node接收的是平台内部HTTP URL。现行实现**不再比较任何运行时Origin，不再维护内部域名列表、后缀或固定项目配对**。它依赖Makers将本项目`/api/*`正确路由到本项目handler及相应服务端配置，不声称从Node独立证明原始公网Host。Host/XFH不参与来源、身份或上游选择。

URL仍严格校验：绝对HTTP(S)、无userinfo/fragment/控制字符/反斜杠；原始路径与规范化路径一致、精确方法/路径白名单，不允许点段、编码路径或重复分隔符。查询原始和规范化长度均限4096、最多64项，严格percent/UTF-8/控制字符校验；保留正常业务参数编码/顺序/重复值，只删除解码后名称精确为`eo_token`或`eo_time`的参数（包括编码名称/重复项）。同名预览Cookie也防御性剥除，业务Cookie与重复Session保留；预览参数不作认证，删除前也必须通过长度/格式检查。没有证据声称实际API已携带或泄漏令牌。

SITE_ORIGIN仍是唯一受审HTTPS浏览器Origin：显式Origin必须精确匹配，POST/DELETE缺少合法Origin仍403。Fetch-Site缺失允许以兼容curl/旧客户端；若存在，仅接受same-origin/none，空值、same-site、cross-site或未知值拒绝。Origin/Referer/Fetch Metadata均不是脚本客户端身份。缺Origin的GET仍须后端Session/权限，auth/me、工单、任务、提案均不是公开数据。

同项目别名被平台映射后，Node无法独立区分原始Host；浏览器host-only Secure/HttpOnly/SameSite=Lax会话Cookie、无CORS开放、所有响应no-store、资源归属及发布门禁继续保护业务。代理额外拒绝缺少这些安全属性的`__Host-pc_session`响应Cookie（502），保留正常多Set-Cookie和注销清除。GET提案读取可能物化已到期状态，不能称所有GET纯读，见[最终报告](EDGEONE_ORIGIN_FINAL_FIX.md)。

无需新增变量，不调整本地Docker、数据库或模型。用户审核PR后才决定合并/部署；保持BACKEND空、CLOSED、禁Worker、无Tunnel，真实health/me预期503；该503只证明本次前置校验通过且没有上游，不代表STAGING/PUBLIC验收完成。全部实际命令及边界见[最终报告](EDGEONE_ORIGIN_FINAL_FIX.md)。
