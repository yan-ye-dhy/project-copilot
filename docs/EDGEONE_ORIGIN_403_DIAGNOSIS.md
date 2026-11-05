# EdgeOne Node Cloud Function 403：定点诊断交接（历史）

> 后续状态：用户已取得真实日志，确认Node内部HTTP Origin导致误拒。临时诊断与专用测试已在后续修复移除；本文保留PR #2时的调查过程，不再执行其中收集日志步骤。精确内部匹配方案后续也已替换；现行策略及验证见[最终来源设计](EDGEONE_ORIGIN_FINAL_FIX.md)。

## 1. 目标、基准及当前结论

2026-11-05，基于用户已合并的 `60875ee393e28e3f04db12d752b7397244b10615`，只排查代理来源403。用户报告固定项目域名 `https://project-copilot-2v2ym9bf.edgeone.cool`，SITE_ORIGIN相同，未配置BACKEND_ORIGIN，CLOSED/禁Worker，未开Tunnel。

**尚不能离线证明 EdgeOne 重写了 request.url，不能声称真实403已经修复。** 当前交付是保持严格准入的临时诊断版：先取得函数内真实Origin及分支，再依据证据做兼容修复，不猜测放行部署域名。没有修改本地Docker、数据库、环境变量、模型配置或视觉，没有访问真实站点/云账号，没有启动Worker/Tunnel、切换发布状态或调用模型。

## 2. 修改文件

- `cloud-functions/proxy.mjs`：三个403来源分支附加固定内部reason；新增仅服务端、定点、有界、脱敏的观察，不改变判定或响应。
- `tests/edgeone_origin_diagnostics.test.mjs`：9项合成运行时测试，覆盖项目/部署域名差异、Host伪造、正常503、403分支、隐私及日志故障。
- 本报告：根因证据、官方契约边界、实际测试及用户最少云端验证步骤。

`cloud-functions/api/[[default]].js`继续复用同一个handler，没有新增诊断HTTP路由、依赖或配置变量。请求仍经过原方法/路径、Header、Origin、正文大小、超时、Cookie/CSRF、代理密钥及后端门禁。

## 3. 403分支与已经排除的因素

| 内部reason | 现有条件 | 用户浏览器观测能证明什么 |
| --- | --- | --- |
| `url_origin_mismatch` | `new URL(request.url).origin !== config.siteOrigin` | 浏览器location.origin一致不能证明函数内部URL或实际env一致；这是首要嫌疑 |
| `origin_header_mismatch` | 存在但不匹配的Origin，或写请求缺少合法Origin | **GET缺少Origin本来允许**；仍需核对平台传入函数后是否为空值/重写 |
| `fetch_site_disallowed` | 存在且非same-origin/none的Sec-Fetch-Site | 浏览器显示same-origin不等于已验证函数内值；日志只记录是否允许 |

按用户给出的“无有效后端目的地”前提，响应发生在读取正文、fetch、Tunnel、Nginx或FastAPI之前；这些本地服务不会导致本次来源403。SITE/secret配置无效本应503，非法方法/路径应404。匹配站点的GET通过前置检查后，无目的地应503且不读体/联网，本地测试已证明。

## 4. 官方资料能证明和不能证明什么

2026-11-05查询：

- [Node Functions](https://pages.edgeone.ai/zh/document/node-functions)示例读取request.url，并描述context.env和server等属性；**没有保证其origin一定等于浏览器固定项目域名，也未定义Host/X-Forwarded-Host是经认证的原始站点身份**。
- [域名管理](https://pages.edgeone.ai/zh/document/domain-overview)区分固定项目域名与每次部署的独立域名；这不证明Node Handler必然以部署域名重写URL。
- [构建/环境说明](https://pages.edgeone.ai/zh/document/build-guide)说明生产/预览变量独立、变量更改仅适用于新部署。因此控制台当前值正确，不等于正在执行的部署已经读到该值。

不能从这些资料直接决定新的可信域名映射。Host/XFH可被客户端影响，诊断中的Host比较仅是线索，不参与准入；不允许全部`*.edgeone.cool`、不改SITE指向部署域名来掩盖问题、不把eo_token当业务身份。

## 5. 临时诊断及隐私边界

只有有效站点/代理secret配置、无有效后端目的地、GET health/me时，才记录三个来源403或前置通过后的503。事件名 `pc_proxy_origin_v1`；每个warm handler最多两条，无新增环境开关、无全球限流承诺。配置无效、非法路径/方法、其他路由、写请求和已有有效后端时不产生日志。

记录字段仅为：固定route/outcome，净化的request_url_origin/expected_site_origin，URL是否匹配、Origin是否存在/匹配、Fetch-Site是否存在/允许、Host是否存在/等于预期host，以及有效后端是否配置（此诊断范围为false）。

**不记录完整URL、路径查询、eo_token、fragment、Cookie、Header对象、Host/XFH原值、API Key、代理密钥、账号凭据或正文。** URL仅取origin；userinfo、异常协议、超长origin、含代理密钥的origin变为`[redacted]`。默认只向Cloud Function服务端结构化日志写入，不回传浏览器；同步日志异常也不改变403/503、不发上游。平台自动日志不受本模块控制，不能转发整份平台日志或HAR。

这是临时观察代码，不是来源豁免、身份认证或发布开关。确认根因并验证最终修复后，应删除该观察及专用测试/交接说明；来源检查继续保留。

## 6. 自动测试与未验证项

执行者Codex；全部输入为合成，真实传输仅本机回环。

```bash
node --test tests/edgeone_origin_diagnostics.test.mjs \
  tests/edgeone_proxy.test.mjs tests/edgeone_proxy_http.test.mjs
```

**59 passed、0 failed、0 skipped**：原50项代理＋9项诊断，覆盖正常无Origin GET503、部署URL不匹配403、伪造Host/XFH无效、非法方法404、非法写Origin403、scheme差异、运行时Header异常、读体/上游零调用、敏感标记不泄漏、日志失败/预算不改判定，以及原4MiB/多Cookie/CSRF/超时/无重试。

```bash
PLAYWRIGHT_MODULE=/tmp/project-copilot-ui-qa/node_modules/playwright node --test \
  tests/frontend_auth_browser.test.mjs tests/frontend_jobs.test.mjs tests/frontend_auth.test.mjs \
  tests/edgeone_build.test.mjs tests/edgeone_proxy.test.mjs \
  tests/edgeone_proxy_http.test.mjs tests/edgeone_origin_diagnostics.test.mjs
```

完整相关组合 **88 passed、0 failed、0 skipped**（5.95s）；上述59项包含在组合内，不重复相加。MODULE是本机已有QA依赖路径，不是新增应用依赖。`node --check`及`git diff --check`通过。没有Python/数据库/Docker改动，因此不启动它们或重复其集成测试。真实EdgeOne运行时、URL重写及合法GET恢复503均**待用户审核部署后验证**，不将合成部署URL案例当云端证明。

## 7. Git与审核

独立分支 `fix/edgeone-origin-403-diagnostics`，审核代码与测试后推送并创建草稿PR；不写main、不合并PR、不通过工具部署。main已有自动生产部署，用户合并会触发部署，必须先审核。开发分支也可能被平台既有自动预览配置构建，不代表真实生产验证或开放授权。

实际提交、PR与远端核对见本次交付消息/Git记录。用户本机私有环境、CLOSED、禁Worker和无Tunnel安排保持；本轮没有读取或改写.env.public-demo。

## 8. 用户下一步：最少一次请求及一条日志

1. **先审核PR。** 审核同意后由用户决定合并/部署诊断版本，核对实际部署的commit和生产环境；本轮不代执行。保持原变量，尤其不为试验修改SITE；BACKEND继续未配置，Worker/Tunnel继续关闭。平台变量变更不作用旧部署，不要只看控制台编辑值。
2. 在固定项目域名页面打开开发者工具Console，只执行一次：

```javascript
const response = await fetch('/api/health', { cache: 'no-store', credentials: 'same-origin' });
console.log(response.status, await response.json());
```

若需对比会话检查，最多再将路径改为`/api/auth/me`执行一次。不要附加eo_token/query，不发送POST、不读/复制Cookie或请求正文。本诊断版**可能仍返回403**，这是保留来源校验的预期，不代表已修复；若返回503则前置检查已通过且没有上游。

3. 在该部署的Cloud Function服务端日志筛选 `pc_proxy_origin_v1`，只提供这一条净化JSON及HTTP状态，不发送完整URL、HAR、headers或原始平台日志。两个日志额度仅限单warm handler：没有新记录时先查同部署已有前两条，不无限重试/重部署；也核对实际代码、环境、route以及BACKEND是否意外在旧部署生效。

| 观察结果 | 下一步依据 |
| --- | --- |
| request_url_origin与expected_site_origin不同 | 才有运行时差异的直接证据；结合scheme/项目与部署域名、Host布尔线索核实平台语义，不能据此盲信Host/XFH |
| expected_site_origin不是实际固定项目origin | 核对实际部署/生产与预览环境及部署时变量，而非放宽校验 |
| URL匹配但reason为origin_header_mismatch或fetch_site_disallowed | 平台传入Header与浏览器观测不同，依据该分支继续查；不直接删除Origin/CSRF |
| preflight_pass_no_backend且HTTP503 | 当前请求通过前置校验，未连接任何上游；仍不代表STAGING、真实业务或Tunnel已验收 |

取得真实证据后，再做只允许已证可信映射的最小兼容修复与回归。最终目标仍是合法health503、非法方法404、非法来源403，CLOSED拒绝业务。当前先交付诊断，不虚构已经达到云端修复目标。
