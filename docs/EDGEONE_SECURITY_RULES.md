# 默认域名下的邀请制入口保护

2026-11-05 用户已在腾讯云中国站控制台确认：Makers 精准速率限制与自定义安全规则只适用于自定义域名，当前免费分配的 `edgeone.cool` 项目域名无法使用。此前阶段1.5提出的“1条频控＋4条规则”仅为当时待核实的方案，**不能作为当前域名已通过入口防护的证据**。免费版配额表列出功能不代表本项目默认域名可配置。

本轮在独立开发分支完成本地整改；真实环境保持 CLOSED，未配置腾讯云、启动隧道或开放 STAGING/PUBLIC。前端视觉、Hybrid＋Rerank、Agent、审批及原有数据保留。

## 三层防护及边界

```text
浏览器
  → EdgeOne Makers Node Cloud Function（/api/*）
  → Cloudflare Quick Tunnel
  → 单个 Nginx 安全入口（跨 worker 共享限流区）
  → FastAPI 发布门禁、Session/CSRF、所有权
  → PostgreSQL 持久业务限额 / Qdrant / Hybrid＋Rerank
```

| 层 | 实际保护 | 无法保证 |
| --- | --- | --- |
| Cloud Function | 固定目的地与方法/路径白名单；本站 origin 绑定；Header、编码、Content-Length及正文边界；写入格式与来源前置校验；115秒超时；不重试；不转发浏览器伪造的代理身份 | 请求已触发函数；无跨实例原子共享计数，不能阻止调用次数/GB-s消耗，不能以这些校验替代登录或CSRF |
| Nginx | 所有地域/实例的函数请求集中到一个本机入口；共享内存的全局、写入请求和连接预算；大小/路径/方法限制；固定上游、超时、不缓存认证响应；无新增宿主公网端口 | 限流到达此层时已经经过函数/Tunnel；非平台DDoS防护，不能保护家庭网络/Tunnel容量或函数额度；共享区仅覆盖同一Nginx实例的多个worker，不覆盖多个独立容器/主机 |
| FastAPI / PG | 代理密钥、CLOSED/STAGING/PUBLIC、Session/CSRF、受限身份和归属；注册/登录短窗、AI日额/并发、Context/提案配额、上传并发与确认幂等 | 应用校验前的网络/解析资源仍有开销；配额不能证明模型事实正确，也不能抵消已发生的函数调用 |

Nginx不将Tunnel连接IP当作真实用户IP，也不信任浏览器提交的 `X-Forwarded-For`。它使用有界全局预算，用户级控制仍由PG执行。不同地域的Cloud Function实例共享这个入口；不要为了扩容直接增加独立Nginx副本，否则会拆分限流预算。限流参数及其实际测试以本轮 [专项报告](DEFAULT_DOMAIN_SECURITY_REPORT.md) 和仓库中的Nginx配置为准。

GET任务状态轮询使用读请求预算，**不计入较严格的写请求预算**；仍受总体连接与请求容量约束。受限演示验收须验证正常轮询不会被误伤，而不是完全免除GET的资源上限。

## Cloud Function 进入阶段

所有入口共用 `cloud-functions/proxy.mjs`。配置 `PROJECT_COPILOT_SITE_ORIGIN` 为当前站点的精确HTTPS origin，与后端 `PROJECT_COPILOT_AUTH_PUBLIC_ORIGIN` 一致；缺失、错误或请求origin不匹配时关闭。既有后端origin和代理密钥仍只放函数服务端，不能进入前端构建变量或静态文件。

方法、路径、请求Header、声明大小和高风险格式可以在代理调用 `getReader()`、访问Tunnel之前拒绝。没有Content-Length或长度虚报的正文仍须有界读取才能知道真实大小；平台本身也可能在函数代码执行前缓冲正文，不能宣称项目代码控制了平台缓冲。普通JSON上限256KiB，上传保留4MiB文件所需的4MiB＋64KiB multipart正文上限；完整文件约束仍由后端复核。公开PDF继续503。

Origin、`Sec-Fetch-Site`、Cookie存在性等只能作早期筛选；脚本客户端可以伪造其中部分Header，服务端身份、CSRF、受限账号、资源归属与业务限额不可省略。`eo_token`仅作为平台预览机制按真实环境验证，不能当作API身份认证。不要新增浏览器可见的固定密码或隐藏URL来冒充访问授权。

## 免费共享状态的可行性结论

2026-11-05核对官方资料：

- [KV](https://pages.edgeone.ai/zh/document/kv-storage)仅支持Edge Functions，读取存在最长60秒的边缘缓存一致性窗口；公开API未给出本项目可据以实现严格计数的原子increment/CAS/事务保证。
- [Blob](https://pages.edgeone.ai/zh/document/blob-storage)可以强一致读取，但公开的覆盖写入与读取组合不等于原子读改写，不能据此声称严格跨实例限流。
- [Functions配额](https://pages.edgeone.ai/zh/document/limits-and-quotas)列出Cloud Functions每月100万次、请求正文6MB、最长120秒；Edge Functions正文1MB，因此本轮不把上传迁移到会破坏4MiB能力的路径。
- [Cloud Functions](https://pages.edgeone.ai/zh/document/cloud-functions)支持多地域部署；进程内Map不共享，故本项目没有把它当作全局限流。
- [指标定义](https://pages.edgeone.ai/zh/document/metric-analysis)按函数被触发次数统计请求。代码返回403/429时已经触发函数，仍不能宣称不消耗函数调用额度。

本轮没有找到经过验证、无需新增付费服务且适用于当前Node Cloud Functions的原子云端限流能力。因此不引入未经证明的KV方案。函数调用之前的滥用与免费额度耗尽风险仍然存在，任何本地测试不能抹去该缺口。当前 [价格说明](https://pages.edgeone.ai/zh/document/pricing-and-plans)不提供可用于本项目实付计算的未来商业价；免费额度与费用边界须用户在实际账号核实，不能保证额度耗尽后行为。

## 后端持久配额复核

| 对象 | 实际约束 |
| --- | --- |
| 注册/登录 | 全站60次/自然分钟、账号5次/自然分钟；独立事务记录尝试；PG跨进程最多2个密码哈希并发槽 |
| AI任务（含上传） | 用户5/UTC日、全站30/UTC日、用户并发1；同一幂等键不重复预留；running/unknown继续占槽，不自动重放 |
| Context | 用户滚动1小时5、最多3个活跃、有效期24小时；PG触发器与owner级事务锁 |
| 提案 | 用户UTC日10、未过期pending最多3、最长15分钟且不超过Context；PG触发器与事务锁 |
| 上传 | 管理员、Session/CSRF先验证；全站PG会话锁最多1个正文接收，30秒有界读取；4MiB文件，PDF503 |
| 确认 | action行锁、owner/Context/指纹与状态校验；工单creation_key唯一约束，重复确认返回原结果 |
| 轮询 | Session和任务owner校验；不新增AI预留，不扣AI日额，不进入写入短窗口 |

自然分钟不是滑动60秒，窗口交界可出现突发；外层Nginx使用独立有界预算。请求审计另有全站60/分钟、账号10/分钟的新增审计行容量控制，不能与供应商计费或所有入站请求的严格限流混为一谈。

## 受限STAGING准入与PUBLIC禁入

当前默认域名方案只供用户明确接受剩余风险后的短时受限STAGING；本轮代码拒绝以该方案开放PUBLIC。STAGING保留后台交互授权、不可变审计、真实工件摘要、代码/配置/资源/站点绑定、有限身份白名单、到期关闭和依赖失败关闭；此方案授权窗口最多2小时。普通环境变量不会开启数据库状态，Worker也不因Compose服务启动而自动获准领取。

`edge_protection`工件须真实记录所采用的替代入口、实际配置/测试、默认域名控制台限制、已知函数额度缺口和本次风险接受；`restricted_access`须记录受限账号、时间窗口与停止条件。不能把原有控制台规则记成通过，不能用本地Nginx单测冒充云端验证，也不能编造公网成功记录来满足门禁。具体工件结构、顺序和命令见 [部署配置](EDGEONE_DEPLOYMENT.md) 与 [运行手册](PUBLIC_DEMO_RUNBOOK.md)。

## 用户必须完成的真实平台验收

本轮未执行以下操作，须在审核开发分支并另行授权后由用户操作：

1. 确认生产分支自动部署设置；先审查PR，不擅自合并main。核对本项目真实域名、HTTPS、预览机制与函数运行时；保存默认域名规则不可用的脱敏记录。
2. 设置精确站点origin及既有函数服务端变量；执行真实Node函数的合法/非法方法、路径、Header、大小、Cookie和多Set-Cookie检查，记录部署版本与日志。只有本地Node结果不能写成云端通过。
3. 在CLOSED下升级独立入口并核对原卷挂载、健康及503；先在Tunnel/Worker关闭时做实际云函数前置和离线错误检查；审核工件并获用户明确授权后，交互开启STAGING/Worker，最后另获授权才启动Tunnel。不得通过降低健康标准、绕过代理密钥或修改数据库行强行启动。
4. 有界验证全局/写入限流、恢复、并发、正常GET轮询、上传、登录退出/CSRF、直接Tunnel拒绝、断线和超时。核对函数调用指标确实仍增加，观察额度与本地资源；超出批准范围或异常即CLOSED并停止入口/Worker。
5. 退出STAGING后核对数据库状态、Worker停止与未决任务，保留脱敏证据，不清空已有数据库或卷。

记录中禁止保存Cookie、API Key、代理密钥、管理员密码或真实Tunnel地址。所有PUBLIC缺口须另行解决、真实复核并再次用户授权；免费默认域名目前不具备直接PUBLIC准入条件。
