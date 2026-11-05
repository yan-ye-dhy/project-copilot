# 聊天视觉与公网故障体验改进

## 1. 本次目标与边界

基于 main `d8407d7`，保持水墨竹青视觉、安全 Markdown、SSE、真实阶段事件、任务恢复和提案独立确认。改进消息层级、头像/命名、内嵌状态与故障提示；不增加 Nginx 阈值、不改变 PERSONAL_DEMO 三任务或并发额度，不修改在线容器、卷、密钥、发布授权或云端配置。

本轮公开故障排查使用源码、只读容器配置/脱敏日志和 PostgreSQL `BEGIN READ ONLY` 汇总，没有向公网或模型发送测试请求，没有恢复/重放 unknown 任务。

## 2. 修改文件

| 文件 | 职责 |
| --- | --- |
| `src/project_copilot/web/assets/app.js` | 统一助手身份、SVG头像和内嵌状态；保留任务编号及提案卡片 |
| `src/project_copilot/web/assets/styles.css` | 透明助手正文、用户浅竹青气泡、印章、响应式排版及提示对比度 |
| `src/project_copilot/web/assets/api.js` | 明确429拒绝/502不确定语义；仅对已有任务GET有限退避，禁止重放POST |
| `tests/frontend_outage.test.mjs` | 12项故障语义、Retry-After、GET退避、重连节奏与POST一次性验证 |
| `tests/frontend_visual_browser.test.mjs` | 7项桌面/移动浏览器外观、无障碍、Markdown及不重复提交验证 |
| `docs/CHAT_VISUAL_RESILIENCE.md`、`docs/evidence/CHAT_VISUAL_*.png` | 本次实现、脱敏证据与合成浏览器实拍 |

本报告不将Codex自动验证登记为用户本人手测。

## 3. 视觉与交互实现

助手使用独立透明`.message-body`，DOM不再嵌套白色`.message-bubble`；清理原重复消息布局和旧标签头像样式。用户保持右对齐浅竹青气泡，使用本地固定SVG user-round轮廓，无重复“你”标签。助手为竹青“知”印章、“知行助手”及小字Project Copilot。Markdown内文透明，引用、表格和代码继续使用各自必要的轻量排版。

所有进度、SSE临时草稿、错误、unknown、停止及恢复采用内嵌图标/细色条，保留ARIA status/alert。错误不再套在白色消息气泡中。副标题和恢复文字在纸色上的对比度至少4.5。

已有job_id的状态GET遇429/502/503/504或网络/超时，最多5次只读退避后停止等待（最多6次连续失败读取），基础2/4/8/16/30秒并加不超过500ms小抖动。尊重合法Retry-After；超过剩余8分钟等待预算就保留编号退出，不提前绕过服务器等待。SSE错误也退避；少于1.5秒即结束的非终态流累计两次后转轮询，每次至少间隔2秒，避免旧300ms短连接循环。

所有POST/DELETE仍单次尝试；429说明本次请求被拒绝，502/503/504、网络或499中断说明受理/执行结果未核实。查询失败保留job_id、原结果未知状态与恢复入口，不改成failed、不退配额、不自动再次调用模型或确认提案。身份/归属错误不进入重试。

## 4. 429／502 排查结果与设计依据

### 当前入口策略（未修改）

实际运行 ingress 的 `/etc/nginx/nginx.conf` 与本次起点源码逐字一致，SHA256：
`9305fa0399bfe5a9ebf1591d10e9d20bec3c0ec5d38c1d5f96692cb038de71ec`。

| 保护 | 当前策略 |
| --- | --- |
| 所有请求 | 全站共享固定 key，30r/s、burst40、nodelay |
| POST／DELETE | 全站共享固定 key，2r/s、burst8、nodelay |
| 正在处理的请求连接 | 全站24；其中写请求4 |
| 拒绝响应 | 429、固定 JSON“请求过于频繁，请稍后重试。”、`Retry-After: 2` |
| Nginx上游异常 | 500/502/503/504统一输出503，不暴露后端诊断 |
| 代理期限 | Node普通请求115秒、SSE25秒；后端SSE20秒主动结束，心跳5秒 |

这些区共享两个 Nginx Worker 的状态；固定 key 也意味着经过该入口的多个用户、标签页和健康探测共享预算，不能解释成“每个用户独立30r/s”。GET轮询/SSE不计入较低的写请求区，但仍计入全站请求区；SSE在处理期间占全站连接，不占写连接。闲置 keep-alive TCP 不等于正在处理的请求连接。[Nginx连接限制说明](https://nginx.org/en/docs/http/ngx_http_limit_conn_module.html)、[请求限制说明](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html)。

本次起点源码的正常单页路径：登录态检查后初始化只并行发健康和UI配置两个GET，没有周期性HTTP健康定时器（页面15秒定时器只更新本地提案显示）。每任务先读状态，再顺序建立一个SSE，正常20秒后间隔300ms重连；降级后原轮询间隔2秒。后端SSE每秒读取数据库不等于浏览器每秒发HTTP。单页健康路径本身不能证明超过30r/s；多标签、快速重复点击、慢写请求占满4连接、上游异常短连接反复重连均可能增加压力。此处是触发机制分析，不是本次线上事故的直接归因。

### 本次真实只读证据

2026-11-05读取公共演示 API 最近两小时的保留日志；实际可见窗口约 `11:50:02Z–12:17:20Z`。只统计 Uvicorn access 行，未将同一次请求的 Trace 日志再次计数，不输出原始URL、query、Cookie、用户ID、账号或正文。

| 可核实项 | 结果 |
| --- | --- |
| 后端实际收到的任务提交 | `POST /jobs` 两次429，时间 `12:07:08Z`、`12:07:51Z`（北京时间20:07） |
| 同窗口任务状态／SSE请求 | 0条；不能将这两次429归因于已经发生的SSE重连/轮询 |
| 其他业务 access | auth/me 200一次、ui/config 200一次、Context创建201一次 |
| 健康 access | 200共296条、503共38条；含部署健康探测，不能当作用户发起的296次操作；早期503也不能等同于当前业务故障 |
| Nginx日志 | access_log关闭、error_log丢弃是现有隐私配置；没有可回溯的限流计数，不临时放开原始日志 |
| Cloudflare保留日志 | 42行、1条注册连接；未发现固定“connection refused／deadline／Unable to reach origin”诊断，不能据此证明EdgeOne无故障 |

对原独立演示 PostgreSQL 的只读事务进一步得到：读取快照时发布状态 PERSONAL_DEMO、尚未过期；**当前授权窗口任务0个，唯一账号有1个unknown占槽，UTC当日3个任务；全站当日3个；最近两小时quota_rejected审计2次**。

`QuotaService.reserve()`先检查当前授权窗口三任务额度，再检查跨授权窗口的`reserved/running/unknown`。PERSONAL_DEMO用户并发上限固定1。因此在该有效授权快照下新增任务会被`user_concurrency`拒绝，**换授权窗口不应释放旧unknown占槽**。这能确定当前阻塞条件，且说明至少上述两次429到达FastAPI并受到数据库业务配额检查，而不是被Nginx在到达API前拒绝。旧审计没有保存逐请求细分quota reason，不能把当前快照冒充对每次历史429的完整还原。没有修改unknown、释放额度、重新授权或新增任务。

### 502的已知范围与未核实项

“上游服务暂时不可用。”来自Node代理固定错误响应；可由连接/读取错误、上游HTTP502、重定向、Cookie/编码/响应大小/SSE类型保护等产生。明确的内部期限触发504；Nginx自身上游错误通常被其503映射隐藏。HTTP502本身不足以证明模型失败或任务未创建，尤其POST可能已经提交到数据库。

本轮本机保留API日志没有502，且没有云函数运行日志或与事故对应的阶段诊断；不能确认是哪一个分支触发用户报告的502。下一次用户侧复现只需记录：UTC时间、方法和固定路由类别、HTTP状态、存在时的run_id、`Retry-After`、Node脱敏`stage/error_type`，以及该任务是否已有job_id。不要记录地址中的敏感参数、Cookie、账号密码、原始正文或Tunnel地址。没有这些证据前不提高限流，不重发POST，不宣称真实公网故障已消失。

### 本次客户端改进原则

- 提交被429明确拒绝时说明“请求被拒绝”，但仍保留此前任务和unknown，不宣称已有后台工作已取消或费用退还。
- POST502/503、网络中断和超时说明“执行结果尚不确定”；不能把代理错误当作数据库回滚证明，更不能自动重发POST。
- 已有job_id的只读状态/SSE遇429或临时502，保留ID并有限退避；优先尊重受限`Retry-After`，避免SSE失败后立刻密集重连。认证错误不靠重试绕过。
- 不改变后端并发1、PERSONAL_DEMO三任务、幂等键和事务设计。unknown的管理员只读调查/受控恢复仍按原流程，不属于本次视觉改造自动动作。

## 5. 自动测试证据

本轮 Codex 实际执行现有限流专项回归（新建合成容器/网络，无原数据卷、无公网/模型调用）：

```bash
PROJECT_COPILOT_RUN_INGRESS_DOCKER=1 PYTHONPATH="$PWD/src:$PWD" \
  /home/yanye/project-copilot/.venv/bin/python -m pytest -q \
  tests/test_public_ingress_docker.py \
  -k 'shared_limits_across_workers or polling_excluded or write_connection_budget'
```

结果 **3 passed、6 deselected，0失败**，23.46秒。证明共享请求区能在多进程合成压力下返回429并恢复、GET轮询不占低速写区、慢写请求共享4连接预算且GET仍可查询。测试没有提高阈值，也没有证明实际线上某次502根因。

其他最终验证：

| 实际命令／范围 | 结果 |
| --- | --- |
| `node --test tests/*.test.mjs`（配置现有Playwright路径，业务网络全部拦截） | **219通过、0失败、0跳过**，13.01秒；含原6项及新7项浏览器用例 |
| `PYTHONPATH="$PWD/src:$PWD" /home/yanye/project-copilot/.venv/bin/python -m pytest -q tests/test_web.py` | **13通过、0失败**，2.15秒；静态资源、CSP、配置隐私与API保持 |
| `node scripts/build_edgeone.mjs --output-dir /tmp/pc-polish-static-20261105` | 8个静态文件，未覆盖现有产物 |
| `node --check`（app.js、api.js）、`git diff --check` | 通过 |

新浏览器用例验证1440px桌面和390px手机：实际computedStyle中助手及消息父容器透明、无多层边框，user-round SVG/无重复“你”，知行助手＋Project Copilot＋知印章，Markdown/XSS、表格与长代码内部滚动，整页不溢出，输入框在视口内，副标题对比度≥4.5，429/502/503/unknown/SSE/停止/刷新恢复不重发POST。测试只读使用本机已有Windows雅黑/宋体以核对中文字形；没有将字体、账号数据或凭据复制进项目、镜像或产物。

实拍仅含合成账号、受控内容及替身结果，不是公网成功证据：[桌面](evidence/CHAT_VISUAL_DESKTOP.png)、[移动端](evidence/CHAT_VISUAL_MOBILE.png)、[手机503内嵌提示](evidence/CHAT_VISUAL_MOBILE_ERROR.png)。未调用付费模型，没有启动或变更真实公网部署。

初轮整套测试与新增浏览器断言编写并发，捕获到测试把设计所需2px提示条错误要求为0；修正断言后，在源码/测试冻结状态完整重跑219项通过，没有删除失败场景或修改限流来通过测试。

## 6. GitHub信息

独立分支 `feat/chat-visual-resilience`，起点main `d8407d7`；真实提交/PR地址见最终交接，不在报告中自引用。只提交已审核的本轮9个文件，保持不自动合并、不部署。

## 7. 学习与排查重点

消息外壳与Markdown内文的层级、DOM安全渲染、状态语义与ARIA；SSE只读恢复和POST幂等的区别；入口请求速率与后端逻辑任务额度的区别；为什么unknown跨授权保留占槽。

## 8. 后续步骤

等待用户审核PR后自行决定合并/部署；本轮没有部署。无新增变量、镜像、迁移或配额配置。已有EdgeOne构建仍为`node scripts/build_edgeone.mjs`、输出`dist`。后端及入口阈值无需为本次视觉改造调整。

需要线上验证的是新样式在用户浏览器的实际效果、真实网络429退避与云函数502阶段。当前unknown占满并发槽，部署前端不会解除这一业务限制；应沿用既有管理员核实/恢复流程，不能通过删记录、换窗口或改额度绕过。不要为了验证出字而重复提交付费任务。
