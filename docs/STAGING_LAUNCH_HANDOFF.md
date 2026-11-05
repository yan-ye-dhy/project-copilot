# STAGING 发布交接与紧急回退

本轮基准main `80302f9`（PR #4已合并），实际新提交见PR。**只做准备，保持CLOSED、worker=false、无Tunnel、不配置或调用付费模型。** 本手册取代旧文档中“暂停平台即可止损”的未核实建议。所有标 `REQUIRES_EXPLICIT_USER_AUTHORIZATION` 的操作本轮均未执行，用户应逐项单独授权，不能整段复制执行。

当前结论：`READY_FOR_CLOSED_CHECKS`；本轮审核材料可供 `READY_FOR_USER_REVIEW`；**`READY_FOR_STAGING_AUTHORIZATION=false`**。缺真实模型Key的现有门禁、受审工件、云端负例、可执行云端止损验证及本人风险接受。下面后半段仅为条件满足后的操作说明。

## 1. CLOSED 最终预检

WSL进入项目，用现有锁定环境。不要初始化数据库、创建管理员、运行迁移或重启容器；这些用户报告已完成。原四个卷和缓存保持。

```bash
cd ~/project-copilot
# 标准库、默认只读；显式 --docker 也只执行只读探针。
.venv/bin/python -m scripts.staging_preflight --docker
# 既有归属检查：读取本地私有配置但只输出脱敏结果，不输出 compose config。
.venv/bin/python -m scripts.public_resources check
# 交互管理员复核实际容器指纹；不改变release状态，不调用模型。
docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml \
  exec api python -m scripts.release_control inspect
```

预检有任何FAIL先排查；`NEEDS_MANUAL_CHECK`须人工补证，不等于PASS。目录/文件权限、cache存在只证明可读，不证明完整模型可推断。CPU/内存/磁盘数字是快照，没有足够依据时不自动判容量充足。工具不打印私有文件内容/完整Docker环境。主机本轮开发中dirty属真实状态，正式预检必须在审核提交后干净；dirty不是理由覆盖用户改动。

应见CLOSED/worker=false，PG/Qdrant/Phoenix健康，migrate退出0，API/ingress可运行但unhealthy；这不需修health。Worker/Tunnel不能运行，演示容器不得发布宿主端口；WSL/Windows额外端口转发、防火墙、路由器及平台状态需人工检查。inspect若失败不猜成CLOSED。

## 2. EdgeOne 前置负例与额度监控（仍无上游）

用户报告真实health503、me503、PATCH404已完成，整理原件，不盲目重复。SITE保持固定HTTPS origin，BACKEND仍未配置。Node URL仅用于路由，不收集内部主机名单。平台预览token不作业务认证。

**REQUIRES_EXPLICIT_USER_AUTHORIZATION — 新增真实云端测试请求**（一次错误来源负例，平台若先401/HTML则记录未达函数，不携秘密重试）：

```bash
curl --silent --show-error --max-time 15 \
  https://project-copilot-2v2ym9bf.edgeone.cool/api/jobs \
  -H 'Origin: https://wrong.example' -H 'Content-Type: application/json' \
  --data '{}' --write-out '\nHTTP %{http_code}\n'
```

预期应用JSON403「请求来源不允许。」。浏览器不能任意设置Origin，禁止把被忽略的header当负例。其他头/体积/路径测试只选有必要的小样本，由用户看真实Node日志和请求数确认；不能从响应推断平台未预缓冲正文。缺目的地与源码早拒绝可以证明设计无上游分支，平台实际内部行为无观测则记未知。

**REQUIRES_EXPLICIT_USER_AUTHORIZATION — 控制台设置变更**：用户在项目设置→环境管理分别核对生产/预览自动部署、变量作用环境和部署commit；关闭自动部署只阻止以后触发，不停止现存服务。不得把解除Git关联视为停止流量。

只读查看真实套餐、函数次数/GB-s、构建数及可用告警：测试前后、窗口期间按固定少量频次查看，记录UTC时间和统计延迟。默认域名无精准限流/自定义规则，不写不存在的控制台配置；403/413/503仍可能计函数用量。后端Nginx只保护到达它的请求，不能保护调用前额度。若无已验证的自动硬上限/瞬时停用，不宣称有。

## 3. 工件、hash及人工审核

按 [证据工作单](STAGING_EVIDENCE_WORKSHEET.md) 的8项盘点。已有历史真实恢复演练与本版本源码摘要不同；当前卷归属、恢复、受限身份、前置负例和本地入口记录须有实际受审文件。私有目录700/文件600，不提交Git，工件不能含密码、Cookie、token、Tunnel地址、数据库数据或完整平台日志。

哈希只绑定文件，不证明其结论。先取得真实原件→脱敏→人工审核→固定SHA，再人工编写顶层授权和默认域名edge receipt；本轮只给不可授权工作单，不自动生成可用 `staging.json`。容器路径 `/app/release`，主机 `data/release-evidence`。用实际镜像inspect的code/config/六入口摘要与本地/云端版本对照，不能用本地checkout代替运行镜像。

## 4. 准入判断与顺序阻塞

现有实现真实要求：

- `validate_evidence`：精确8 checks、1–20个去重active账号UUID、资源/origin/代码/安全配置/时效绑定，verified≤7天；默认edgeone.cool expires最长2小时、PUBLIC禁止。
- `dependencies_ready`：两个模型Key非空、冻结BGE三文件、Manifest目录、Qdrant就绪；trace开启另验Phoenix。**STAGING授权、worker-enable及每次公网HTTP准入均调用该检查。** 当前Key未配置，连登录/CSRF非AI链路也无法通过，不允许假Key、禁探针或强改数据库。
- 授权不需要Tunnel已经运行。前置云端离线记录+本地入口验证可以先审核；真实Cookie/上游业务在窗口内验，因此不存在必须先伪造公网成功再授权的循环。当前问题是模型就绪与所有HTTP路径的耦合。
- 最小后续选择：用户另行授权在后端安全配置真实Key（配置不等于授权调用，首次网络验证仍禁止AI任务）；或独立提出非AI/AI就绪分层的安全变更并审查。本轮不实现或降低原准入标准。
- 白名单限制业务及任务，不等于所有其他账号都无法登录/me；邀请策略、Session/CSRF/owner/context继续独立执行。

**只有**工件语义/字节、当前配置/存储就绪、云端止损方案、指定用户和本人剩余风险接受全部审核后，才可重新评估 `READY_FOR_STAGING_AUTHORIZATION`。本地测试成功本身不满足。

## 5. 用户交互授权短期 STAGING（当前不可执行）

**REQUIRES_EXPLICIT_USER_AUTHORIZATION — 写入真实发布状态**：仅在上一节满足时，由本人在TTY运行，输入现有管理员、密码、精确 `AUTHORIZE STAGING` 和无秘密原因。授权期限建议短于2小时，禁止通过过期时间滚动延长而不重验。

```bash
docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml \
  exec api python -m scripts.release_control STAGING --evidence /app/release/staging.json
```

该命令成功后worker仍false。失败不自行重试或改库；inspect核对实际结果。入口/API健康等待正常通过，不用no-deps、不弱化health、不把普通env当发布开关。

## 6. 单独授权 Worker、Tunnel 与服务端目的地

首轮无需模型的真实浏览器验证可先**不启Worker**，先独立许可Tunnel。Tunnel依赖ingress健康，ingress依赖API准入；因此必须先完成合法STAGING。任何到期/依赖问题都应关闭。

**REQUIRES_EXPLICIT_USER_AUTHORIZATION — 启Tunnel并配置云端变量**：

```bash
docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml \
  --profile tunnel up -d cloudflared
```

用户在自己终端私下读取本人的HTTPS trycloudflare origin，填EdgeOne函数服务端BACKEND_ORIGIN；不得粘贴聊天、Git或工件。**配置变更和为生效所需的新部署也各需用户许可**，旧部署变量不会自动改变。SITE和代理密钥仍服务端固定，Tunnel只到ingress、不直连API/数据库；隧道不是身份认证。

**REQUIRES_EXPLICIT_USER_AUTHORIZATION — 开Worker（后续AI阶段才需要）**：

```bash
docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml \
  exec api python -m scripts.release_control worker-enable
./scripts/start_public_demo.sh --serve
```

`--serve`先检查worker-ready再依赖API健康，不能在CLOSED启动；每次任务领取/外呼仍核对发布状态。不得为了试登录先排入模型任务。

## 7. 窗口内真实端到端（不提前打勾）

| 顺序 | 无需模型的受控项目 | 留证及失败处理 |
| --- | --- | --- |
| 1 | 登录/退出、Secure+HttpOnly+host-only+SameSite Cookie、多Set-Cookie、Session失效、CSRF缺失/错误拒绝 | 只记录属性和状态，不保存凭据/CSRF原值；不截完整HAR |
| 2 | 两名受限账号工单/任务/提案context及归属隔离、非法方法/路径、错误Origin、直接Tunnel缺secret拒绝 | 不用真实私有数据；用户自己操作，不把same-origin当身份 |
| 3 | 真实Nginx共享限流/GET轮询、受控HTTP断线/超时、Tunnel断开后失败关闭、函数用量前后变化 | 不压测云端，不提交AI任务；超时不重复POST；若平台统计延迟记未知 |

**REQUIRES_EXPLICIT_USER_AUTHORIZATION — 单独的模型/业务验收**：只有用户配置真实供应商Key并另外授权小规模调用后，才开Worker测试Hybrid+Rerank/RAG→Agent查工单→提案预览→独立确认→相同ticket_id幂等。记录实际调用/usage和可核实账单；PDF仍503、不放开放注册、不调高配额、不启PUBLIC、不降Dense。非AI确认/幂等可用既有受控提案在获业务写入许可后测试，不冒充模型验收。

## 8. 到期、紧急关闭与恢复

**REQUIRES_EXPLICIT_USER_AUTHORIZATION — 关闭真实服务**：先交互CLOSED（同时worker=false）；成功与否均核对，锁等待5秒失败不代表关成功。DB故障直接停入口/进程，保留诊断。

```bash
docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml \
  exec api python -m scripts.release_control CLOSED
./scripts/stop_public_demo.sh
```

stop仅按演示项目标签依次停Tunnel→ingress→API→Worker（最多360s宽限）→Phoenix/Qdrant/PG，不删卷。CLOSED不撤销已提交供应商请求，断HTTP也不证明取消；running/unknown不能重放或无条件释放额度。完全stop后PG也停，不能马上inspect-job；先在CLOSED且PG运行时调查，或另授权恢复隔离存储后再走既有管理员维护工具。恢复必须证明API/Worker/migrate已退出、核对供应商和工单/提案结果、最新inspection/停机签名/工件摘要，通过 `terminal_no_retry` 审计，证据不足继续保留占槽。

**云端止损是另一个边界。** 目前用户未找到可核实一键暂停；关闭本机/Tunnel、改secret、断Git、停自动部署均不停止已有函数调用。静态维护候选及云端验证步骤见 [维护回退](STAGING_MAINTENANCE.md)。发布失败旧版可能继续运行，旧部署URL也可能仍活跃；不能凭首页出现维护字样就宣布额度受保护。

恢复业务版本、恢复自动部署、重启存储/API、重新授权STAGING/Worker/Tunnel均属于 **REQUIRES_EXPLICIT_USER_AUTHORIZATION**。恢复前核对原件、实际部署commit/配置、8项证据时效、代码/配置/入口指纹、任务调查和余额；绝不自动回PUBLIC。
