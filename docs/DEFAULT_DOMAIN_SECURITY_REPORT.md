# 免费默认域名入口防护专项报告

> 后续兼容说明：阶段二真实日志确认Node内部HTTP URL不等于公网SITE。旧“所有未审核预览域名GET必拒绝”的表述不能作为现行保证；Node无法独立证明原Host。现行精确传输组合、保留的身份/来源边界及移除临时诊断见[修复报告](EDGEONE_RUNTIME_ORIGIN_FIX.md)。下文测试和Git为阶段1.5历史快照。

## 1. 目标、基准与结论

基准 `c9dda5343cf9708c1a5190253ab324ce23e297d8`，2026-11-05。用户已在中国站控制台确认，`edgeone.cool` 默认域名不能使用 Makers 精准频率限制/自定义安全规则。阶段1.5的候选控制台方案因此不能作为发布证据；本轮不重新设计业务系统。

本地替代入口整改已实现，**可以准备短时、少账号、主动接受剩余风险的 STAGING；真实云端前置检查、安全升级与用户交互授权尚未完成，当前不可直接开启，也不能 PUBLIC**。本轮未访问云账号、启动隧道、改私有配置/密码、切换真实发布状态或调用付费模型。用户报告的现有CLOSED/禁Worker环境未被操作。

## 2. 实现与文件

| 文件 | 职责 |
| --- | --- |
| `cloud-functions/proxy.mjs`、两份 `tests/edgeone_proxy*.test.mjs` | 所有Node入口统一早期校验；精确SITE_ORIGIN、方法/路径、Header、JSON256KiB与multipart4MiB＋64KiB、115s、无重试、脱敏及Cookie兼容；未配置Tunnel时仍可验前置拒绝，合法请求503且不联网 |
| `deploy/nginx/Dockerfile`、`public-demo.conf` | 固定官方版本及摘要的单个非root Nginx；共享zone全局/写入速率与并发、大小、路径/方法、上游超时、无缓存与敏感Header白名单 |
| `docker-compose.public-demo.yml` | 新增无宿主端口ingress及专网；Tunnel→ingress:8080；API保持backend+egress，模型出网不变 |
| `scripts/public_resources.py` | 专网归属、实际拓扑/挂载、单运行入口实例预检，拒绝拆分共享预算 |
| `scripts/start_public_demo.sh`、`stop_public_demo.sh`、`public_backup.py` | 准备入口，停止/冷备先停入口；不触碰卷数据或自动启动Tunnel |
| `services/entry_protection.py`、`release_control.py`、`scripts/release_control.py` | 默认域名STAGING专项工件、六个入口文件绑定、显式剩余风险接受、最长2h；PUBLIC授权与运行时双拒绝 |
| 主 `Dockerfile` / `.dockerignore` | 只增加非秘密入口文件供容器内摘要核验；不改变原开发Compose运行行为 |
| 默认域名、资源安全、Docker入口测试与脱敏收据 | 真实PG准入、实际Nginx与网络、受控上游；不冒充云端证据 |
| 四份部署/安全/运行/发布文档 | 已有CLOSED部署安全升级、变量、真实云验及关闭顺序 |

前端HTML/CSS/SVG和既有Hybrid＋Rerank、Agent、审批业务代码未重写。没有新增数据库迁移、模型服务或付费云依赖。

## 3. 新拓扑与各层的能力边界

```text
浏览器同源 /api/*
 → EdgeOne Makers Node Cloud Function
 → Cloudflare Quick Tunnel（本轮未启动）
 → ingress：单Nginx容器 / 两个worker / 共享zone
 → FastAPI：代理密钥 → 发布准入 → Session/CSRF/归属
 → PostgreSQL持久限额 / 真实工单与审批
 → Qdrant + BM25 + BGE Rerank / Agent
```

Docker网络为：数据服务仅backend；API/Worker/migrate在backend+原egress；Nginx在backend+新增固定`pc-public-v2-ingress`；Tunnel仅ingress。原四个外部卷及原两个网络不改名，不迁移，不删除；全套公开配置均不发布宿主端口。需要安全升级现有API镜像，旧运行容器不会自动加载源代码。

| 层 | 实际能拦截 | 不能声称 |
| --- | --- | --- |
| 函数 | 可按URL/Header决定的校验先于项目代码读取body/fetch；无长度/虚报长度只能有界读体。配置缺失503，错误站点/Origin403 | 不能阻止“函数被调用”；平台可能预缓冲正文，跨地域无已证原子共享限流；Origin不是身份 |
| Nginx | 到FastAPI前拒绝未知方法/路径、缺失/格式不合法的代理Header、超大正文、超限速率/连接；实际密钥值仍交FastAPI恒时验证 | 不保护函数额度、家庭网络或Tunnel带宽；不识别不可信XFF为真实用户；共享区仅单个Nginx容器，重启计数重置 |
| FastAPI / PG | 原认证、CSRF、归属、身份白名单、CLOSED、持久业务配额、上传锁及审批幂等 | 不替代入口容量防护，不自动证明模型事实、外部取消或账单 |

Nginx原始访问和请求上下文错误日志关闭，避免查询参数/Cookie被原样写入；排错用脱敏JSON、健康、后端run_id。在明确私有维护窗口才能临时启用原始日志，不能公开分享或作为脱敏证据提交。此取舍降低Nginx低层故障的自动诊断细节，503不可被静默当作成功。

Nginx阈值：全站30r/s、burst40；POST/DELETE 2r/s、burst8；总连接24、写连接4。是共享漏桶/并发预算，不是按用户/IP或固定分钟精确计数。GET轮询不进写桶，但仍有全站容量上限。超限429及Retry-After=2；上游断线/超时/5xx统一脱敏503，调用者核对幂等任务状态，不自动重放。连接/读体/传输超时为Nginx阶段空闲时限，115s函数总时限另外约束代理链；不是对任意直连慢速攻击的绝对总执行上限。

Nginx显式关闭上游故障自动重试；提交结果不明时只查询既有任务/幂等结果，不重新POST。

原后端PG约束继续独立：登录注册全站60/自然分钟、账号5/分钟、密码哈希2并发；AI用户5/UTC日、全站30、并发1；Context滚动1h5/活跃3/24h；提案日10/pending3/15min；上传全站接收1个、30s、有界大小、管理员且PDF503；确认行锁与唯一键、轮询不新增AI额度。详见[入口保护](EDGEONE_SECURITY_RULES.md)。

## 4. 免费能力分析、发布门禁与残留风险

2026-11-05核对 [Cloud Functions](https://pages.edgeone.ai/zh/document/cloud-functions)、[配额](https://pages.edgeone.ai/zh/document/limits-and-quotas)、[KV](https://pages.edgeone.ai/zh/document/kv-storage) 与 [Blob](https://pages.edgeone.ai/zh/document/blob-storage)：Node Cloud Functions6MB/最长120s、支持多地域；KV仅Edge Functions、最终一致且未给出原子递增/CAS保证；Blob强一致读取不等于原子读改写。本轮不接KV计数，不用进程Map冒充全局控制，不迁移到1MB正文的Edge Functions破坏上传。

[指标说明](https://pages.edgeone.ai/zh/document/metric-analysis)按函数触发计数。代码早拒绝、Nginx429、Tunnel关闭都发生在函数调用之后；免费100万次/月、50万GB-s仍可被消耗。无法保证恶意额度耗尽或未来价格；控制台硬预算/自动暂停能力未核实，不编造免费外层防护。此为PUBLIC阻塞，短时STAGING只能由用户明确接受剩余风险。全站预算可被攻击者占满，邀请制不等于抗DDoS；停止本地入口不等于停止平台函数计费/配额消耗。

保留所有原STAGING必填检查、管理员TTY密码/授权短语、不可变审计、配置/代码/资源/origin/账号/到期与依赖失败关闭。对edgeone.cool（含尾点等价FQDN）：`edge_protection`必须为新profile，含真实云端与本地验收工件实际SHA、六个入口源摘要及逐字剩余风险接受。期限最多2h；普通env不能开放；Worker独立授权。PUBLIC无论授权文档或伪改数据库行运行检查均拒绝。摘要证明当前字节绑定，不自动证明真实EdgeOne部署版本、工件语义或测试通过，仍须管理员审核。不能用仓库合成测试收据冒充发布授权。

## 5. 自动验证与未执行项目

执行者为Codex，所有业务测试数据合成；没有用户本人手测。

- 完整Python回归：`PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 PC_TEST_WEB_QDRANT=1 OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 .venv/bin/python -m pytest -q -rs`：**1596 passed、13 skipped、0 failed**。13项为当时已收集的7个Nginx Docker用例、5个真实存储用例、1个CUDA模型。前两组随后另行启用并通过，Docker另增第8项真实三网探针也通过；CUDA本轮未启用。不相加重叠用例或宣称全量无skip。104条旧Starlette测试Cookie弃用警告。
- 真实PG：新增默认域名证据、2h、过期/身份、PUBLIC拒绝和源漂移测试在每例全新随机数据库执行，既有登录/配额/并发/审批回归通过；不访问实际演示库。真实PG通过不等于真实云端开放。
- 真实HTTP Qdrant及编译LangGraph：五项独立启用存储flags的集成回归 **5 passed、0 failed**。模型/Embedding受控替身，非真实模型质量。
- Node代理与静态构建：50项代理（含本机真实回环HTTP）＋10构建通过；完整前端/Chromium/Node组合 **79 passed、0 failed、0 skipped**（包含上述50＋10，不重复相加）。浏览器后端被拦截或受控，不是公网浏览器验收。
- Nginx固定镜像实际pull/build和非root/read-only配置校验成功；最终 **8 passed、0 failed**（38.00s）：7项受控HTTP上游＋1项真实FastAPI无数据库失败关闭/三网隔离；29项结构/预检/启停/备份安全测试也通过（与全量重叠，不相加）。Docker测试与脱敏收据详见[实际记录](evidence/DEFAULT_DOMAIN_INGRESS_LOCAL.json)。速率、两进程客户端、两worker共享、超限恢复、conn4、GET轮询、4MiB、Cookie/两Set-Cookie/CSRF、无key未读body403、断线/超时分别验证。上游主体为受控Python HTTP；额外真实API探针先在容器内核对实际Gateway503正文，再验证外侧正确密钥仍503、缺密钥403、仅Tunnel网客户端不能解析直连API。该探针无数据库，不冒充真实PG业务链路。超时用独立派生配置110s→1s加速，不伪称等了110s。
- 应用镜像 `project-copilot:default-domain-validation` 正常实际构建；六源与后端指纹host/image一致，无`.env`、模型权重或私有发布工件；六个当前入口文件与镜像字节绑定只证明本地构建，不冒充真实平台已部署。验证容器无网络/卷、只读并已退出。没有部署到当前演示容器。
- 真实EdgeOne Node平台、控制台变量/日志/额度、真实Tunnel、PUBLIC、真实模型/CUDA、真实公网Cookie/4MiB上传链路均**未执行**；留作获授权的STAGING验收，不冒称已通过。

复现命令（仅专用合成环境，禁止指向演示业务库）：

```bash
# 真实编译图与存储；新测试集群为5434/16433，测试helper逐例新建随机数据库。
PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 \
PROJECT_COPILOT_RUN_QDRANT_INTEGRATION=1 PROJECT_COPILOT_RUN_FINAL_QDRANT=1 \
PROJECT_COPILOT_QDRANT_URL=http://127.0.0.1:16433 \
OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 .venv/bin/python -m pytest -q \
  tests/test_agent_integration.py tests/test_final_qdrant_integration.py tests/test_qdrant_integration.py

# 构建新标签；Docker测试只启动新随机容器/网络、临时回环端口，不使用数据卷。
docker build --pull=false -t project-copilot:default-domain-validation .
PROJECT_COPILOT_RUN_INGRESS_DOCKER=1 .venv/bin/python -m pytest -q tests/test_public_ingress_docker.py

PLAYWRIGHT_MODULE=/tmp/project-copilot-ui-qa/node_modules/playwright node --test \
  tests/frontend_auth_browser.test.mjs tests/frontend_jobs.test.mjs tests/frontend_auth.test.mjs \
  tests/edgeone_build.test.mjs tests/edgeone_proxy.test.mjs tests/edgeone_proxy_http.test.mjs
```

Chromium与Playwright需先准备在自己的本地QA目录，以上MODULE是本轮实际路径，不是应用新增运行依赖。真实EdgeOne验证仍由用户执行。最终测试结束后仅停止本轮新建的PG/Qdrant测试容器并保留其卷；原演示容器、固定卷、私有env与管理员数据没有操作。

## 6. 新配置与已有环境升级

新增唯一函数服务端必填变量 `PROJECT_COPILOT_SITE_ORIGIN`：实际HTTPS站点origin、无尾斜杠，与后端AUTH_PUBLIC_ORIGIN完全一致；不是浏览器密码。原BACKEND_ORIGIN/PROXY_SECRET继续留函数服务端，模型/数据库密钥继续仅在本地私有env。Nginx没有新增共享密码，健康探针只读取既有代理密钥；配置不为外部请求注入密钥。

用户审核后按[部署配置](EDGEONE_DEPLOYMENT.md)执行。资源initialize只补新专网，不覆盖旧资源；prepare新增ingress并重建应用镜像，现有存储/管理员与卷原样保留，无需重新bootstrap或删库。CLOSED时API/ingress unhealthy是预期，禁止改health或强启Worker绕过。已有调用中的Worker先按原维护流程关闭，不能在活跃任务中随意重建API/执行器。

控制台先确认分支自动预览/生产部署；本人审核PR后才决定何时合并main、填新服务端变量和部署。真实Node前置/离线检查可在Tunnel/Worker关闭时先采集；之后才审核工件并授权STAGING，最后另获授权启动Tunnel与做端到端测试，避免先伪造公网成功才能启动的循环。

## 7. Git及下一步人工操作

本轮仅 `fix/default-domain-entry-protection` 开发分支；审核后提交推送并提供PR供用户审查，**不向main提交/推送、不合并PR、不修改平台资源**。main仍应指向基准；实际SHA/PR以交付消息与远端核对为准。开发分支推送可能触发平台已有自动预览，未审核origin会被函数策略拒绝；不把自动预览当作用户已授权STAGING。

用户下一步仅需审核PR及自动部署影响，随后亲自完成服务端变量、安全升级、真实Node前置检查/额度与暂停方式核实、真实工件审核与风险接受、交互STAGING/Worker、另行授权Tunnel、短窗口完整链路。操作及排错详见[运行手册](PUBLIC_DEMO_RUNBOOK.md)。本轮不自动开始这些步骤。

## 8. 五个最终问题

1. **不买域名能否受限STAGING？** 可按当前分层方案准备短时、少账号演示，但必须用户接受函数额度风险、完成真实Node前置验证/升级/证据及交互授权；目前尚未实测云端或获授权，继续CLOSED。
2. **是否拦在FastAPI前、是否拦在函数前？** 函数和Nginx能在FastAPI前拒绝一部分不合规/超限请求；不能证明能在Cloud Function触发前阻止滥用。函数已被调用的成本风险保留。
3. **是否需要Nginx？** 本轮选择需要：把多地域/多实例函数请求汇聚到一个可验证共享预算，在FastAPI/PG前限制速率、写入并发与缓冲；函数无可靠免费共享原子状态，PG业务额度保护范围更靠后。它不是云WAF，也不能解决函数配额耗尽。
4. **用户还须做什么？** 审核PR/部署影响，控制台设SITE_ORIGIN并验证真实Node和额度/停止方式；WSL安全升级、审查真实工件、主动授权受限STAGING/Worker后另行启动Tunnel，完成公网Cookie/上传/业务/关闭验收。
5. **是否可以免费直接PUBLIC？** 不可以。缺口包括调用前额度/滥用防护、默认域名真实云端与端到端验收、已核实用量止损、Quick Tunnel无SLA与本机带宽/可用性。不能用本地Nginx结果掩盖这些问题；当前代码明确拒绝默认域名PUBLIC。
