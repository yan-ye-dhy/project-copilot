# 公开演示运行手册（阶段1.5）

默认 **CLOSED**，真实资源未切换STAGING/PUBLIC。以下资源初始化、账号、启动、Tunnel及发布命令仅供用户后续明确授权后使用。本轮测试只使用合成数据和新资源，保留原有数据卷、模型缓存及实验记录。

## Windows与WSL准备

Windows启动Docker Desktop并启用当前Ubuntu的WSL Integration。在WSL进入`~/project-copilot`，核对Git提交、`docker version`、磁盘及固定版本BGE缓存。原`start.sh`只用于原本机环境。安装锁定Python依赖供宿主预检使用。

将`public-demo.env.example`复制为`.env.public-demo`，`chmod 600 .env.public-demo`，用编辑器填写[变量表](EDGEONE_DEPLOYMENT.md)。秘密不放命令参数、截图、日志、聊天或Git。随机密码建议base64url，不用`$`插值；预检会拒绝冲突宿主变量。

本人在本机创建`data/release-evidence`并设为700，只放脱敏且已审核的工件；按模板只读挂到`/app/release`。不要把模型缓存、数据库目录或整个家目录用作证据目录。

## 独立资源与CLOSED准备

用户批准创建新本机资源后：

```bash
cd ~/project-copilot
.venv/bin/python -m scripts.public_resources initialize
.venv/bin/python -m scripts.public_resources check
./scripts/start_public_demo.sh --prepare
```

专用卷为pc-public-v2-postgres、pc-public-v2-qdrant、pc-public-v2-phoenix、pc-public-v2-manifests，专用网络也采用固定外部名字和归属标签。项目名覆盖不能改变这些物理名；发现已存在但归属不明的资源时拒绝接管，不迁移、覆盖或删除。禁止绕过预检使用旧数据库URL。

`--prepare`仅启动存储、迁移、API与Nginx入口；新库默认CLOSED，此时API unhealthy/503是预期，不启动Worker/Tunnel。它不主动关闭已有授权状态，已有演示堆栈须先inspect并由用户显式CLOSED，再进入准备/维护流程。不要降低健康检查或用`--no-deps`绕过。初始管理员及一次性邀请由本人交互输入：

```bash
docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml \
  exec api python -m scripts.bootstrap_admin
# 现有管理员创建一次性邀请：同一命令后加 --create-invite
```

## 获授权的阶段二STAGING

用户在EdgeOne连接仓库前确认自动部署行为，设置函数、服务端变量、HTTPS和[入口保护](EDGEONE_SECURITY_RULES.md)。准备本地验收工件及实际控制台防护/受限身份记录。普通环境变量不会开放。

```bash
# 证据文件须以只读挂载在容器中可见；路径仅为占位。
docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml \
  exec api python -m scripts.release_control inspect
docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml \
  exec api python -m scripts.release_control STAGING --evidence /app/release/staging.json
docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml \
  exec api python -m scripts.release_control worker-enable
./scripts/start_public_demo.sh --serve
# 仅用户另外明确授权启动隧道后才执行；本轮没有执行。
docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml \
  --profile tunnel up -d cloudflared
```

状态变更要求管理员密码、AUTHORIZE <操作>短语和无秘密的原因；STAGING限指定身份并有短有效期。缺工件、过期、代码/资源不匹配、依赖不健康或模型缓存缺失都失败关闭。Worker在每次领取和外呼前复查，Compose启动依赖只是辅助。`--serve`不启动隧道。

实际Quick Tunnel域名只在本人终端私下读取并填EdgeOne服务端变量，不发浏览器或聊天。域名变化后更新并重验。CLOSED时隧道依赖ingress健康（后者仍代理API准入）不能启动；先授权受限STAGING后才按批准流程进行网络测试，不能伪造公网成功工件。

## 业务策略与受控保留

| 对象 | 实际策略 |
| --- | --- |
| AI | 用户UTC日5、全站30、并发1；供应商发起默认16，失败不自动重试 |
| Context | 24h，用户滚动1h最多5、活跃3；PostgreSQL触发器和锁执行 |
| 提案 | 最长15min且不超过Context；用户UTC日10、未过期pending3 |
| 未领取附件 | 24h，Worker有界检查；停机不自动清理，维护工具可处理 |
| 终态任务 | 超过7d受控清输入/结果，保留任务及幂等stub；result_expired不触发重新提交 |
| 请求审计 | 超过7d按正常活动或维护清理；不确定/人工恢复调查证据保留 |
| Context清理 | 过期7d且无任务/提案外键引用才删，有引用继续保留 |
| 业务记录 | 提案/确认至少30d，本轮保留全部；工单、知识和恢复审计不自动删 |
| 备份 | 最多最近7份已验证非调查保留集，默认总容量2GiB；未验证/失败证据不自动删 |

清理是明确执行的维护窗口，不承诺无人运行时按时删除。WAL、日志、备份和强停遗留临时文件另受运维策略约束，删除数据库一行不抹除全部历史字节。不要截断WAL或删除持久卷。

## 异常任务调查与恢复

```bash
.venv/bin/python -m scripts.public_maintenance inspect-job --job-id <任务UUID>
# 用户主动授权CLOSED并确认无执行后：
.venv/bin/python -m scripts.public_maintenance cleanup --apply
```

只读调查输出编号、归属、状态、尝试数、usage及业务绑定，不输出问题/模型原文/凭据。同Context提案是核对线索，不代表精确任务归因。真实计费以供应商账单为准。

running/unknown不能自动重跑或直接释放。先CLOSED，停止入口/API/Worker/迁移；核对供应商最终结果与数据库提案/工单。准备权限600的审查工件和回执，绑定任务、最新inspection哈希、已核实供应商结果和terminal_no_retry决定，再执行：

```bash
.venv/bin/python -m scripts.public_maintenance resolve-job --job-id <任务UUID> \
  --evidence <私有回执JSON> --review-artifact <实际审查工件>
```

宿主核验固定卷全部消费者，API/Worker/migrate须真正退出，且能证明Worker在任务开始后终止；短时签名绑定任务、资源和审查工件。内部核验管理员、CLOSED、禁用Worker、执行排他锁、业务绑定与回执，原子写不可变审计。恢复仅终结本地交付、禁止重放，不推断远端成功或账单；保留原尝试、usage及调查证据。无法证明时继续占槽。

## 一致性冷备与新资源恢复

```bash
.venv/bin/python -m scripts.public_backup backup --path data/public-backups/<新备份名>
.venv/bin/python -m scripts.public_backup restore --path data/public-backups/<备份名>
.venv/bin/python -m scripts.public_backup verify-restored \
  --path data/public-backups/<备份名> --restore-plan <上步输出私有plan路径>
.venv/bin/python -m scripts.public_backup prune --path data/public-backups
```

先由用户授权CLOSED/禁用Worker。工具停止入口和写进程，检查租约、实际业务表、Qdrant原文/向量、Manifest与BM25，再冷停存储归档；不分别复制运行中目录。备份目录700/文件600，包含敏感数据，不提交或分享。备份不含权重和Phoenix历史。

恢复只创建全新pc-restore-UUID卷与无宿主端口堆栈，核验归档与计划绑定，使用一致镜像身份比对全部业务和索引摘要后才标verified。没有覆盖正式卷的恢复命令；失败保留诊断和新卷，源服务不自动重启。容量不足拒绝新备份；prune只删合规的已验证旧集，不删除调查保留/未验证资料。

## 紧急关闭与重新准入

```bash
# 本人交互授权；若数据库不可用，直接先停止入口和进程。
docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml \
  exec api python -m scripts.release_control CLOSED
./scripts/stop_public_demo.sh
```

停止顺序为Tunnel、Nginx入口、API、Worker、存储，不删除卷。CLOSED阻新工作，已发出的上游请求可能收尾；取消HTTP不证明远端取消。恢复前核对健康、任务调查、备份、一致性与证据期限，再由用户重新授权；没有自动回切PUBLIC。

## 受控公网端到端验收

用户按已授权环境验证：中文及移动页面；Cookie/多Set-Cookie/Session/退出/过期/CSRF；代理403与非白名单拒绝、跨用户工单及确认拒绝；管理员TXT/MD上传/原文查询/删除，PDF503；真实RAG与Agent联合查询、提案预览和独立确认幂等；AI与Context/提案限额；GET轮询不误限；平台请求限制、115s代理、断线与后端离线；Phoenix仅内网；换隧道域名后重配；紧急关闭后停止新领取/外呼。

实际公网、控制台和必要小规模真实模型验收分别留证，无自动重试。任一关键失败回CLOSED；PUBLIC必须先STAGING验收并再次用户授权。当前状态见[发布清单](RELEASE_CHECKLIST.md)。

## 默认域名专项：本人审核后的操作顺序

用户已完成的存储/管理员/CLOSED初始化不再重复。原四个固定卷、管理员凭据、私有配置、BGE缓存、数据库迁移和业务功能保持。此次只推独立开发分支；**不要直接在 main 上同步或合并，main 已关联生产自动部署。** 先检查预览部署触发设置：开发分支推送也可能自动触发平台预览，但不是本轮云端操作授权。

1. 本人审查PR后决定何时部署代码。控制台新增函数服务端 `PROJECT_COPILOT_SITE_ORIGIN`（与 `PROJECT_COPILOT_AUTH_PUBLIC_ORIGIN` 完全一致的HTTPS origin、无尾斜杠）；保留现有后端origin/代理secret，不修改管理员密码。变量缺失503；错误写入Origin或GET显式Origin403。Node内部URL无法独立证明公网Host，不能保证同项目别名的无Origin GET仅凭此被拒；业务仍须Session与归属。不得把函数变量注入前端静态构建。
2. WSL 中检查 `release_control inspect`，确认CLOSED与禁Worker；按[安全升级步骤](EDGEONE_DEPLOYMENT.md)运行资源initialize/check和 `--prepare`。只新增入口镜像/网络，不重建已有卷。Nginx与API在CLOSED均应unhealthy/503；不用 `--no-deps` 或改health强行启动。原API需要重建加载代码，PostgreSQL/Qdrant/Phoenix持久数据保持。
3. Tunnel与Worker保持关闭时，在真实Node函数做少量早拒绝/上游离线测试；管理员审查日志、调用额度与本地Nginx测试，生成真实 `edge_protection` 工件。无云端证据时**继续CLOSED**，不能把仓库里的合成收据当准入。
4. 用户明确接受剩余函数额度风险后，按[默认域名证据格式](EDGEONE_DEPLOYMENT.md)交互授权最多2小时STAGING、仅1–20个已有账号；Worker另行授权。仅在另获启动隧道授权后才执行原Tunnel命令。此时 `cloudflared → ingress:8080 → api:8002`，Tunnel所在专网不能直连api或数据库。
5. 在短窗口中执行下面验收；任一关键失败，先CLOSED再停止Tunnel/入口/Worker，保留日志和任务调查；不自动重试可能已提交的写操作。未经独立准入审查，不扩展账号或授权窗口。当前默认域名方案禁止PUBLIC。

### 受限STAGING验收记录（本轮未执行）

| 实测 | 成功判定与排查 |
| --- | --- |
| 函数前置拒绝 | 非白名单404、错误Origin403、大声明body413，实际Node函数日志核对；看不到正文读取/上游连接的内部证据时不要宣称平台未预缓冲 |
| 用户正常链路 | 浏览器登录/退出、多个Set-Cookie、Session、CSRF、2个测试账号隔离；无Cookie泄露，错误返回无Tunnel地址/secret |
| 上传 | 已授权管理员4MiB TXT/MD可提交；超限413、PDF503；不要为验证重复消耗模型quota |
| 轮询 | 正常GET jobs状态不消耗写桶或AI日额；突发全局阈值仍可能429，按Retry-After有界退避，不重新POST任务 |
| 来源与限流 | Tunnel缺key403、错误key403；真实写突发限流429、等待后恢复；Nginx进程共享，不能声称跨主机全局 |
| 故障关闭 | API/Tunnel离线映射502/503/504、无泄密；发布到期/显式CLOSED阻新任务/新外呼；在途任务核对，不推断远端已取消 |
| 业务 | 仅在用户另行授权必要小规模模型请求后做知识/工单联合查询→提案→独立确认→相同ticket_id；Hybrid+Rerank不降级 |

### 默认域名限流的紧急关闭

本地流程仍为交互 `release_control CLOSED`，然后 `./scripts/stop_public_demo.sh`。脚本按标签只停演示容器，不删除卷；如DB不可用先停入口和执行进程。仅停Tunnel不能阻止EdgeOne函数被反复调用：用户还须在控制台核实可用的暂停项目/停用部署/撤销入口方式，必要时撤销该部署，而不是只隐藏URL或换secret。控制台是否提供额度告警、暂停与硬上限须本人实际确认；不承诺未证的平台自动止损。

再次开放前先调查unknown/running和已确认工单、核对额度/备份/索引；恢复按既有管理员工具，绝不重放不确定操作。新授权绑定当前代码、配置和入口文件摘要，旧工件不得沿用。

## 最终路由/来源修复后的最少验证

不再依赖或收集内部运行时域名；URL仅严格解析路由，网站Origin仍固定，敏感GET仍须Session/归属。本轮不改现有变量、Docker、数据库或模型，也不启动任何公网入口。先审查 `fix/edgeone-routing-origin-boundary` 草稿PR，由本人决定合并/部署；main自动生产部署，Codex不合并。

保持SITE原值、BACKEND未配置、CLOSED、禁Worker、无Tunnel。在固定项目浏览器页面按[最终报告](EDGEONE_ORIGIN_FINAL_FIX.md)执行health/me GET与非法PATCH，预期503/503/404。错误来源POST应403，但浏览器不能任意修改Origin，别用被忽略的Origin赋值当作负例验收；受控客户端如先遇平台401，应区分平台访问条件。

合法503不证明整站安全，只证明当前请求前置通过且上游关闭。不得为追求200启动Tunnel或改发布状态。若仍403，已不是内部hostname的准入条件，先核对实际部署及Origin/FetchMetadata运行值；不再追加内部域名。未来STAGING仍需实际前后端版本/证据审核，不因本次修复自动开放。
