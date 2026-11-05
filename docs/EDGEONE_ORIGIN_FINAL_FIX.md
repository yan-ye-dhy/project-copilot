# EdgeOne Cloud Function 来源校验最终整改

## 1. 目标、证据与结论

2026-11-05，从main基准 `7faa95dd0ee2e3a87e0128ac5801201b6b26b3af` 创建独立分支 `fix/edgeone-routing-origin-boundary`。用户此前真实Node日志已证明：运行时URL是平台内部HTTP地址，浏览器使用的却是公网HTTPS项目Origin。PR #3精确固化了一个内部地址，但用户确认部署该提交仍403；没有新日志证明新的具体内部地址，不能再次猜测或追加名单。

原问题是把**内部请求路由信息**当作**浏览器来源或身份**。本次彻底移除运行时Origin准入及所有硬编码/后缀/自动学习；URL仅严格解析路由。网站Origin仍固定，身份及权限仍由Session、CSRF、资源归属和发布门禁承担。

本地代码及测试完成，**真实EdgeOne新版503尚待用户审核部署后验收**。没有登录平台、启动Tunnel/Worker、切换状态、操作Docker/数据库/卷/管理员或改写.env.public-demo；模型调用0。新前端视觉、Hybrid＋Rerank及全部业务模块未改。

## 2. 文件与核心实现

| 文件 | 变化 |
| --- | --- |
| `cloud-functions/proxy.mjs` | 删除固定内部组合；严格raw URL/path/query；分离来源判断；剔除预览查询/Cookie字段；公开Session响应Cookie保护失败关闭 |
| `tests/edgeone_origin_policy.test.mjs` | 随机runtime HTTP(S)/主机/端口、来源矩阵、恶意URL/query、固定上游、预览字段及真实本机HTTP入口回环 |
| `tests/edgeone_proxy_privacy.test.mjs` | 真实回环受控认证服务器，验证匿名/归属/CSRF、Cookie、无共享缓存/CORS、预览字段过滤 |
| `tests/edgeone_proxy.test.mjs` | 更新旧来源断言，仍保留原代理安全回归 |
| 删除 `tests/edgeone_runtime_origin.test.mjs` | 旧固定内部域名方案测试由独立路由/来源测试替代，不删除历史Git记录 |
| 部署/安全/运行/发布清单四文档 | 更新现行语义、客户端兼容、敏感GET及验收步骤 |
| `EDGEONE_ORIGIN_403_DIAGNOSIS.md`、`EDGEONE_RUNTIME_ORIGIN_FIX.md`、`DEFAULT_DOMAIN_SECURITY_REPORT.md` | 标记旧方案/旧数量为历史，链接当前报告，不伪改历史云端验收 |

`cloud-functions/api/[[default]].js`及edgeone.json保持。没有新依赖、新配置项或新诊断路由。

## 3. 路由与来源分离

**路由层**接受语法合法的绝对HTTP(S) URL，不论平台内部主机/端口如何变化。原始URL最长8192字符；拒绝空白/控制符/反斜杠、userinfo、fragment、非法协议/端口。原始路径必须与WHATWG规范化路径一致，且精确匹配已有方法/路径白名单；点段、百分号编码路径、重复斜线和调试接口不获准。

查询在过滤之前完整验证：原始与规范化search均≤4096、最多64项、合法percent/UTF-8、无解码控制符或孤立UTF-16代理项，拒空key/空字段。允许合法重复业务key、空value、无等号flag；业务字段保留原编码字节、顺序和重复值，不用URLSearchParams重写。仅删除解码后名称精确为`eo_token`/`eo_time`的参数，包含重复和编码名称；不误删业务值中出现这些文字的内容。

**来源层**只以服务端固定HTTPS SITE_ORIGIN检查显式Origin；POST/DELETE必须有匹配Origin。Fetch-Site缺失允许兼容客户端；存在则仅允许same-origin/none，空值、same-site、cross-site和未知值拒403。Referer不作身份或回退。Origin和Fetch Metadata可被非浏览器客户端伪造，不能授予登录/业务权限。

**转发层**目标始终来自受审服务端BACKEND_ORIGIN，严格HTTPS trycloudflare origin；用户URL、查询值、Host/XFH均不能选择目的地。代理密钥仅服务端注入，客户端同名字段被替换；保留请求大小/Header限制、手动重定向处理、115s、零自动重试。无BACKEND时校验后503，合法正文不读、fetch为0。

这与删除所有来源校验不同：删除的是错误的“运行时hostname等于浏览器Origin”假设；保留并测试了路由、浏览器来源、身份、固定目的地、资源和发布等独立边界。

## 4. 浏览器、别名及敏感GET边界

| 请求 | proxy处理 | 身份/业务边界 |
| --- | --- | --- |
| 同源浏览器GET，无Origin、same-origin | 可过前置 | 敏感接口仍要Session/权限，缺后端503 |
| 同源POST/DELETE，正确Origin | 可过前置，体积/类型仍校验 | 必须会话绑定CSRF、权限/幂等/配额 |
| GET有错误Origin，或浏览器Fetch-Site为cross-site/same-site | 403 | 不读取正文/不fetch |
| 写请求没有Origin、Origin错误或null | 403 | 同源提示不能替代合法Origin |
| 缺Origin和Fetch Metadata的GET，例如curl/旧客户端 | 允许前置，不视为攻击，也不视为登录 | Session/归属继续执行；匿名不得获得业务数据 |
| curl伪造same-origin/Origin | 只是通过浏览器来源筛选 | Cookie及后端认证仍需真实有效，不能伪造账户身份 |
| 项目域名/部署别名被平台映射到同一handler | 不以runtimeHost区分 | **Node无法独立证明原公网Host**，不承诺别名GET一定403 |

敏感GET包括auth/me的用户及CSRF值、工单、任务和提案；不是全部公开接口。源码复核发现：`ApprovalService._get`在经过Session/owner/context校验后，可能把已到期pending物化为expired并flush。它不会创建/确认工单或扣AI额度，但**不能宣称所有GET完全无业务写入**。本轮不擅自改审批模块，继续保留能力凭据与跨站限制；严格纯读语义如需调整应另立业务任务。

所有代理响应强制no-store、nosniff，不透传Access-Control-Allow-*；上游public/max-age不能覆盖。__Host-pc_session必须Secure、HttpOnly、Path=/、SameSite=Lax且无Domain，重复/弱化关键属性返回脱敏502；正常多Set-Cookie、辅助Cookie及注销过期Cookie保留。请求Session/CSRF继续原样传递，不能因过滤预览字段而去重Session Cookie，重复Session仍交后端拒绝。

仅防御剥除明确的`eo_token`/`eo_time`查询key及同名Cookie字段，保留其他业务查询/Cookie；不记录URL/query/Cookie/密钥。**没有证据表明实际API已携带或泄漏这些预览令牌**，不把测试情景当成已发生的事件。平台预览权限不是业务认证。

## 5. 三层保证与未证明事项

1. Makers项目路由＋proxy语法/方法/路径/Header/体积边界：阻止路径绕过和任意代理，依赖平台将项目分发到正确handler；不证明公网Host。
2. 固定浏览器Origin＋Fetch Metadata＋Cookie/CSRF＋server secret/固定目的地：减少浏览器跨站、会话泄露和目标篡改；不认证任意脚本客户端，也不保护调用前函数额度。
3. FastAPI Session/资源归属/数据库配额＋Nginx＋CLOSED/STAGING/PUBLIC：决定真实业务能否执行。默认edgeone.cool仍禁止PUBLIC、PDF继续关闭，本轮不修改这些模块。

官方[Node文档](https://pages.edgeone.ai/zh/document/node-functions)没有已核实的原公网Host认证契约；不使用Host/XFH填补。浏览器安全语义参考[Fetch Metadata](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Fetch_metadata)和[Set-Cookie](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie)。实际平台/CDN是否按no-store、CORS与Cookie策略工作、部署别名的完整行为及真实业务仍须STAGING验收；不能由本地结果冒充。

## 6. 实际自动测试

执行者为Codex；数据均合成，没有真实模型或公网请求。

```bash
PLAYWRIGHT_MODULE=/tmp/project-copilot-ui-qa/node_modules/playwright node --test \
  tests/frontend_auth_browser.test.mjs tests/frontend_jobs.test.mjs tests/frontend_auth.test.mjs \
  tests/edgeone_build.test.mjs tests/edgeone_proxy.test.mjs tests/edgeone_proxy_http.test.mjs \
  tests/edgeone_origin_policy.test.mjs tests/edgeone_proxy_privacy.test.mjs

timeout 60s .venv/bin/python -m pytest -q tests/test_auth_unit.py tests/test_public_gateway.py
```

Node完整相关组合 **102 passed、0 failed、0 skipped**（5.25s：原50项代理、13项新来源策略、10项新隐私、10项构建、19项前端/Chromium；不重复相加）；Python为 **40 passed、0 failed、0 skipped**（1.77s）。首次受限测试进程停滞，终止该助手测试进程后在可运行本机环境重跑；不把终止的运行记通过，也没有终止任何业务进程。MODULE是本机已有QA路径，不是新应用依赖。

Node覆盖随机内部域名/HTTP(S)/端口、严格raw路由/query、来源/Fetch Metadata、匿名/归属/CSRF受控响应、CORS/no-store、多Cookie/注销/弱属性拒绝、预览字段及业务query保留、4MiB/JSON限制、断线/超时/无重放/错误脱敏、CLOSED零正文和零上游，以及Chromium回归。真实传输只限本机HTTP回环；模拟runtime映射和受控认证服务器**不是腾讯云、实际FastAPI Session或PostgreSQL权限集成**。

Python用真实Cookie/Origin/CSRF函数与ASGI门禁，但认证服务/状态数据受控，证明接口合同及CLOSED拒绝，不冒充真实PG。未执行项目：新版腾讯云验证、完整公网链路、实际数据库/Docker重部署、真实付费模型；不将它们登记通过。本轮无须启动或改造这些未变模块。

## 7. Git、文件范围与停止边界

从用户指定main同步fast-forward后创建独立分支，仅提交proxy/Node测试/必要文档，不含.env、秘密、运行数据或权重。测试及diff审核后推送分支、创建草稿PR；不直接合并main，不操作腾讯云。实际SHA/PR和同步核对见交付消息。用户审核前不触发手工生产部署；既有平台自动预览行为由平台配置决定，不等于放行业务。

本轮没有结构迁移、容器/卷变更、账户/密码变更、Agent/RAG/审批改写或视觉改动。不开放STAGING/PUBLIC，完成PR后停止。

## 8. 用户最少真实验收

先审核PR，再由用户决定合并/生产部署，确认实际commit。保持SITE为原固定HTTPS项目Origin、BACKEND未配置、CLOSED/禁Worker/无Tunnel，无新增环境变量。在固定项目页面Console执行一组3次请求：

```javascript
for (const [path, method] of [
  ['/api/health', 'GET'], ['/api/auth/me', 'GET'], ['/api/jobs', 'PATCH']
]) {
  const r = await fetch(path, { method, cache: 'no-store', credentials: 'same-origin' });
  console.log(path, r.status, await r.json());
}
```

预期503、503、404，合法503 detail为「服务暂时不可用。」；没有上游目标，未读合法正文或发起任何实际fetch。不再查找内部hostname或pc_proxy_origin_v1日志，不为了看到200启动Tunnel。

错误来源POST可在**平台允许该受控HTTP客户端访问时**由用户另执行一次：

```bash
curl --silent --show-error --max-time 15 \
  https://project-copilot-2v2ym9bf.edgeone.cool/api/jobs \
  -H 'Origin: https://wrong.example' -H 'Content-Type: application/json' \
  --data '{}' --write-out '\nHTTP %{http_code}\n'
```

预期应用JSON403「请求来源不允许。」。浏览器会禁止脚本任意改Origin，不能用被忽略的Header赋值假装负例通过。若curl先遇平台预览401/HTML，说明尚未到函数，记录为平台访问边界，不能登记应用负例通过，也不能为此放宽认证或公开eo_token/Cookie；负例本地自动化已覆盖，公网项按受控STAGING流程补验。

若新版仍403，运行时hostname已不参与准入，先核对部署、实际Origin与Fetch-Site；不再追加域名。合法503仅证明该请求通过前置且上游仍关闭，**不等于网站完成公网安全验收**。反馈状态和脱敏JSON即可，不发送密钥、Cookie、HAR或完整预览URL。
