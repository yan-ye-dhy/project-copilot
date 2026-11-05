# EdgeOne 内部运行时 Origin 403 修复（历史精确匹配方案）

> 用户确认部署7faa95d后仍403；固定内部域名不是稳定契约，该方案已由[最终路由/来源设计](EDGEONE_ORIGIN_FINAL_FIX.md)替代。以下保留PR #3当时源码/本地测试记录，不再作为当前配置或验收指令。没有新日志时，不宣称已知内部地址再次如何变化。

## 1. 目标、基准及真实根因

2026-11-05，先同步main至PR #2合并提交 `41581c6`，再在独立分支修复。目标是在CLOSED、无BACKEND/Tunnel/Worker条件下，使合法GET health/me通过前置检查并返回503，其他拒绝行为保持。

本次**已经有用户提供的真实腾讯云Node日志**，不是新的猜测或本机模拟：

```text
event: pc_proxy_origin_v1
outcome: url_origin_mismatch
request_url_origin: http://pages-pro-41-af5f.pages-scf-gz-pro.qcloudteo.com
expected_site_origin: https://project-copilot-2v2ym9bf.edgeone.cool
origin_header_present: false
fetch_site_allowed: true
host_header_matches_site: false
backend_destination_configured: false
```

平台另一条Makers请求路径日志指向实际公网HTTPS项目域名。旧检查混淆了“内部Node请求URL”和“浏览器网站Origin”，因二者不同而误拒绝。原GET缺Origin允许，Fetch-Site也通过；错误发生在任何上游之前。

云端日志执行/提供者为用户；Codex本轮没有访问真实站点或云账号。**根因已确认，修复代码本地已验证；新版真实EdgeOne503仍待用户审核部署后验证。**

## 2. 修改文件

- `cloud-functions/proxy.mjs`：新增精确SITE/runtime兼容组合及用户名/密码URL拒绝；保留所有请求与目的地检查；删除临时origin诊断、日志配额和reason。
- `tests/edgeone_runtime_origin.test.mjs`：替代已删除的临时诊断测试，增加合成边界、真实本机NodeHTTP回环及固定目的地测试。
- `tests/edgeone_proxy.test.mjs`：调整旧测试名称，原未审核运行时域名拒绝测试继续保留。
- `docs/EDGEONE_DEPLOYMENT.md`、`EDGEONE_SECURITY_RULES.md`、`PUBLIC_DEMO_RUNBOOK.md`、`RELEASE_CHECKLIST.md`：修正公网Origin与内部URL语义、预览GET保证和验证步骤。
- `docs/EDGEONE_ORIGIN_403_DIAGNOSIS.md`、`DEFAULT_DOMAIN_SECURITY_REPORT.md`：标记历史边界，不覆盖旧测试结果；本报告交付实际根因、设计、测试与后续操作。

未改API、Agent、RAG、数据库、Nginx、Docker配置/卷、管理员、环境变量、模型或前端视觉。没有新增依赖、配置项、诊断HTTP入口或付费模型调用。

## 3. 来源校验策略

`allowedRuntimeURL`只接受：

1. 原普通模式：运行时URL Origin等于服务端固定HTTPS SITE_ORIGIN。
2. 本项目的额外精确组合：SITE_ORIGIN必须为上面已验证的完整HTTPS项目Origin，运行时Origin必须为上面已观察的完整HTTP Origin。

无通配后缀、无任意qcloudteo来源、无Host/X-Forwarded-Host回退、无第一次请求自动学习。不同SITE不能复用该额外组合；不同内部主机/协议/非默认端口或含用户名密码的URL仍403。HTTP默认80按标准URL规范化为同一Origin，不代表接受任意端口。平台未来改变运行时域名后将失败关闭，需重新取证并审核精确组合。

URL仅为路径定位/传输兼容输入，**不成为上游地址**；上游仍从服务端受审BACKEND_ORIGIN取得，必须为合法HTTPS trycloudflare origin、固定路径规则、无重定向/自动重试。未配置时检查后立即503，不读正文、不fetch。

## 4. 信任依据与剩余限制

[官方Node文档](https://pages.edgeone.ai/zh/document/node-functions)说明文件系统路由和context.env等接口，[域名文档](https://pages.edgeone.ai/zh/document/domain-overview)说明项目域名/部署域名差异。本轮再次查询，**未找到可据以认证原始公网Host的Node元数据保证**；Host/XFH不作认证依据。

此方案依赖Makers正确地把本项目路径分发到本项目handler、相应服务端配置，以及用户提供的该项目实际运行时观察。精确内部域名只是传输兼容特征，可能是平台共享设施；它不是平台签名、项目归属认证、用户身份或原始浏览器Host证明。

同项目部署别名若最终映射到同一内部URL，**无Origin的GET无法由Node独立区分原外部项目域名/部署域名**，不再宣称所有预览域名GET都必定403。GET有显式错误Origin仍403；写入Origin必须精确等于固定公网HTTPS SITE；Sec-Fetch-Site同原规则，cross-site/same-site仍拒绝。这些Header也不是对任意脚本客户端的身份认证。

保留方法/路径白名单、Header/正文大小、Cookie/CSRF/Session、host-only Secure Cookie、资源归属、业务配额、代理密钥、固定目的地、Nginx限流及CLOSED/STAGING/PUBLIC门禁。认证/权限不会因兼容组合提升；知识检索不授予写权限。函数调用前额度风险、预览域名完整链路和PUBLIC限制不由本次修复解决。

`pc_proxy_origin_v1`诊断及其专用测试已经移除；函数不再永久记录运行时/站点信息。平台自身日志不受此模块控制，不要公开原始平台日志、HAR或Cookie。

## 5. 自动测试与执行边界

所有数据为合成。NodeHTTP回环使用真实本机server/client，将请求映射为用户已观察的内部URL，**不模拟成官方可信元数据、不接触实际腾讯云或Tunnel**。无BACKEND矩阵的正文reader/fetch计数均为0；另有一个显式配置假目的地的纯内存fetch替身，证明内部URL不会改变固定上游地址。

```bash
PLAYWRIGHT_MODULE=/tmp/project-copilot-ui-qa/node_modules/playwright node --test \
  tests/frontend_auth_browser.test.mjs tests/frontend_jobs.test.mjs tests/frontend_auth.test.mjs \
  tests/edgeone_build.test.mjs tests/edgeone_proxy.test.mjs \
  tests/edgeone_proxy_http.test.mjs tests/edgeone_runtime_origin.test.mjs
```

最终结果：**90 passed、0 failed、0 skipped**（5.31s），包括原50项代理回归、10项静态构建、19项前端/受控Chromium和11项新增运行时测试，不重复相加。MODULE为本机已有QA依赖路径，不是新应用依赖。

覆盖health/me503、PATCH404、错误/缺失写Origin403、错误GET Origin/Fetch-Site403、非法路径/Header/大小、相似主机/任意后缀/协议/端口/userinfo、伪造Host/XFH、固定上游、无敏感错误回显、4MiB/多Set-Cookie/CSRF/超时/无重试，以及不再输出临时日志。`node --check`、`git diff --check`及文档链接核对另行完成。未改Python或本地服务，不重复启动其集成测试。

真实根因证据来自用户此前云端日志；**修复版的云端503、生产Cookie链路和模型调用没有由Codex执行**。CLOSED保持，Tunnel/Worker未启动，付费模型调用0。

## 6. Git与部署边界

独立分支 `fix/edgeone-internal-runtime-origin`，只推此分支并创建草稿PR；不向main推送、不合并PR、不操作平台。main同步仅fast-forward，保留PR #2历史，不改写提交。实际commit/PR及远端核对见交付消息。

main已绑定生产自动部署，用户审核合并后才部署新版。无需为本次修复调整SITE、BACKEND、模型或本地Docker。今后真正进入STAGING前，入口实际版本/摘要及发布工件仍须一致并重新审核，本修复不是发布授权。

## 7. 最少真实验证

先审核PR；由用户决定合并/部署，并核对部署commit。继续保持原SITE、BACKEND未配置、CLOSED/禁Worker/无Tunnel。在固定项目页面Console执行**一次**：

```javascript
fetch('/api/health', { cache: 'no-store', credentials: 'same-origin' })
  .then(async r => console.log(r.status, await r.json()));
```

预期 `503` 与 `{"detail":"服务暂时不可用。"}`。这代表前置检查通过且没有目的地，不代表业务已开放。无需发送密钥、Cookie、完整URL或平台原始日志；只反馈状态/该JSON即可。不再搜索新`pc_proxy_origin_v1`事件。

如需最小补充，可在同页测auth/me503、PATCH jobs404：

```javascript
for (const [path, method] of [['/api/auth/me', 'GET'], ['/api/jobs', 'PATCH']]) {
  const r = await fetch(path, { method, cache: 'no-store', credentials: 'same-origin' });
  console.log(path, r.status, await r.json());
}
```

错误Origin写入403已在合成与真实回环验证；浏览器不能任意覆盖Origin，勿用被浏览器忽略的伪造Origin控制台代码冒充云验。若需真实平台负例，另用符合平台访问条件的受控HTTP客户端；本轮不要求额外公网请求。

## 8. 失败排查与后续边界

仍403先核对实际部署版本、SITE是否仍精确匹配及平台是否改换内部Origin；不改成后缀放行、不信Host/XFH、不自动学习。若为平台401/HTML，区分预览访问/部署状态与本模块JSON403；不要公开eo_token。合法GET503是当前目标，502或业务200则应核对BACKEND是否在实际部署中意外生效，不能为看到成功而启动Tunnel。

实际云端验证后再登记该项通过；不自动继续STAGING、PUBLIC或扩大业务/模型测试。
