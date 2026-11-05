# 阶段二 STAGING 准备与紧急回退报告

## 1. 目标及实际起点

2026-11-05，从干净且同步的main `80302f9b103a6b8f628f8d6d41a758ce73ad664d` 创建 `chore/staging-handoff-and-maintenance`。准备真实证据审核、只读预检、维护候选和用户顺序操作手册；不改变业务门禁、私有env、数据库、Compose/Nginx、模型或前端正式视觉，不初始化/重建现有资源。

用户确认预览自动部署已关闭；main仍关联生产自动部署，本轮不合并、不发布。用户提供真实EdgeOne503/503/404及历史额度/控制台描述，按真实用户反馈记录；Codex没有访问用户站点或云账号。截图原件/摘要和其他云端负例尚需本人整理。

## 2. 文件与职责

- `scripts/staging_preflight.py`、`tests/test_staging_preflight.py`：默认只读本地、显式Docker只读metadata和固定库READ ONLY状态查询；权限/入口SHA/迁移真实退出/停止状态/资源快照，不授权、不加载env、不输出秘密。
- `scripts/build_maintenance.mjs`、`tests/maintenance_build.test.mjs`：显式全新目录的独立静态维护候选、原竹/山SVG与无JS页面、文件hash；无Cloud Function/登录/API，不影响正式builder或dist。
- `docs/STAGING_LAUNCH_HANDOFF.md`：8阶段真实操作顺序，所有状态变更/云端测试均标显式本人授权；区分无模型与付费验收。
- `docs/STAGING_EVIDENCE_WORKSHEET.md`：8 checks、来源分类/缺口、精确字段/时效/指纹/身份绑定及不能授权的人工记录模板。
- `docs/STAGING_MAINTENANCE.md`：官方能力核实、候选构建、同项目替换/旧路由验证、失败/恢复和额度残余风险。
- `RELEASE_CHECKLIST.md`、`EDGEONE_DEPLOYMENT.md`、`EDGEONE_SECURITY_RULES.md`、`PUBLIC_DEMO_RUNBOOK.md`：链接现行交接，纠正未核实的暂停/止损假设，避免重复初始化与把旧结论当许可。

## 3. 源码审查与运行顺序

实际STAGING需要identity_isolation、quotas_limits、storage_restore、retention_recovery、proxy_limits、resource_isolation、edge_protection、restricted_access八项；精确path/hash、资源、固定origin、code/config/六入口摘要、verified前7天、expires将来、1–20个active身份。默认edgeone.cool最长2h、禁止PUBLIC；管理员TTY密码和AUTHORIZE不能由脚本代填。

先CLOSED检查→真实函数无上游负例/用量→私有文件脱敏/摘要/人工审查→依赖及准入评估→本人STAGING→另许可Tunnel/BACKEND新部署→无模型浏览器验证→另许可模型/Worker/业务→CLOSED/停服务/未决调查。授权不依赖Tunnel先运行，因此不用伪造Cookie成功工件解决循环。

**发现实际阻塞**：`dependencies_ready` 要求两家真实模型Key非空，并在STAGING授权、worker-enable、每次HTTP准入执行。当前用户确认Key未配置，本轮不得配置；登录/CSRF的真实端到端也暂不能进行。文档提出后续单独授权配置或另立就绪分层审查，本轮不改门禁、不填假Key。

历史真实冷备恢复收据的三项源码摘要与当前均不同：可保留历史价值，不能自动晋级为当前资源storage_restore。白名单针对业务而非禁止全部其他账号登录/me；GET审批可能物化过期状态，均保留实际边界。

## 4. 设计、安全与回退决策

只读预检的未知/外部事项输出NEEDS_MANUAL_CHECK，退出0不等于发布通过。迁移必须exited且0，created/paused/restarting/dead不能误报安全。读取固定演示scope metadata，不打印Docker环境/health日志/绑定源路径/工件内容；无公网请求。网络及其他卷消费者仍用既有完整归属检查。

维护包共7文件，仅public子目录是候选上传根；包根manifest不上传。模板CSP禁止script/connect，无登录/API/环境读取。没有覆盖已有路径、递归复制整个仓库或修改正式构建。官方资料支持重新部署与静态包，但没有证明新包不带函数就原子卸载全部旧函数/旧部署别名；必须真实检查函数清单、受控请求/日志/用量延迟，不只看首页或404。

停本地/Tunnel、换secret、断Git、停自动部署均不能阻止现有云函数计次。用户未发现一键暂停；不删除项目、不猜控制台功能。CLOSED不撤销已提交模型请求，running/unknown保留额度和证据，不重放；完整stop后PG也停，调查须安排相应存储就绪，恢复仍走既有管理员审计工具。

## 5. 实际自动验证及未执行项

执行者Codex，新增测试均合成；既有认证/ASGI为受控服务，HTTP/Chromium仅本机回环。没有真实云端或付费模型请求。

```bash
PLAYWRIGHT_MODULE=/tmp/project-copilot-ui-qa/node_modules/playwright node --test \
  tests/maintenance_build.test.mjs tests/frontend_auth_browser.test.mjs \
  tests/frontend_jobs.test.mjs tests/frontend_auth.test.mjs tests/edgeone_build.test.mjs \
  tests/edgeone_proxy.test.mjs tests/edgeone_proxy_http.test.mjs \
  tests/edgeone_origin_policy.test.mjs tests/edgeone_proxy_privacy.test.mjs

timeout 60s .venv/bin/python -m pytest -q \
  tests/test_staging_preflight.py tests/test_public_demo_scripts.py \
  tests/test_public_resource_safety.py tests/test_public_ingress.py \
  tests/test_public_backup_safety.py tests/test_auth_unit.py tests/test_public_gateway.py \
  tests/test_release_control.py::test_configuration_fingerprint_excludes_keys_and_passwords
```

- Node **108 passed、0 failed、0 skipped**（5.67s，新增维护6＋既有102，不重复相加）；Python **99 passed、0 failed、0 skipped**（2.09s，新增预检29及相关合同）。没有启用真实PG写入/新卷恢复集成测试。
- `node --check`、`py_compile`、diff、维护产物白名单/无函数/环境sentinel敏感扫描、文档链接检查。独立安全复核发现的created迁移假PASS已修并增加反例后再跑最终组合。
- 当前脚本真实构建 `/tmp/pc-maintenance-handoff-20261105-01`，7文件，未上传。agent早期受限子进程未正常运行不计通过，最终本机组合才计上述结果。
- 实际只读 `.venv/bin/python -m scripts.staging_preflight --docker` 核实：PG/Qdrant/Phoenix healthy，API/ingress运行且unhealthy；migrate exited/0；四固定卷标签和主要挂载符合，无演示容器宿主端口；scope内无运行Worker/Tunnel；READ ONLY读得CLOSED/worker=false；7个入口/构建摘要及私有目录检查通过。模型仅确认存在配置文件，不等于冻结完整推断验收。开发时dirty导致退出1属真实状态，提交后复核；主机/Windows外部进程与监听、容量、云端及工件语义仍需人工。
- 未执行：完整业务/模型回归、当前环境冷备恢复、云端负例/维护替换/旧函数移除/真实Cookie与额度监控、STAGING授权/Worker/Tunnel。历史测试不冒充本轮执行。

## 6. GitHub 与部署边界

用户明确授权独立分支提交和草稿PR；用户另确认预览自动部署已关闭，故只推本分支，不改main，不触发本轮部署。提交仅上述源码/测试/文档，不含env、私有工件、token、日志、运行数据或预备构建包。实际commit/PR与远端核对见交付消息；不为填写本文件自引用提交而追加历史。

## 7. 审核与学习重点

集中审阅只读工具的命令白名单/READ ONLY事务/未知状态、八工件语义与机器hash的区别、业务白名单例外、模型就绪与非AI入口耦合、静态发布与历史函数生命周期、CLOSED/断线/供应商最终状态的区别。官方链接和能力限制集中在维护文档，不扩展业务架构。

## 8. 最终结论与下一步（最多5项）

| 结论 | 状态 |
| --- | --- |
| READY_FOR_CLOSED_CHECKS | 是：工具/说明就绪；仍须看每项FAIL/NEEDS_MANUAL_CHECK |
| READY_FOR_USER_REVIEW | 是：本地实现/相关回归完成，供草稿PR审核 |
| READY_FOR_STAGING_AUTHORIZATION | **否**：真实依赖配置、受审证据及云端止损/风险接受尚缺 |

1. 审核草稿PR及三个交接文档；不自动合并或部署。
2. 本人在CLOSED复核只读结果，整理已有截图/部署/额度原件，补最少云端负例及受审文件。
3. 在单独授权下确认/演练同项目静态维护能否移除旧函数与别名；未证实继续保留云端额度风险。
4. 决定后续真实Key安全配置或另审就绪分层；逐项人工审核八工件和短期风险，不生成伪许可。
5. 只有再次明确满足准入并由本人授权，才执行STAGING、Tunnel及后续模型/Worker；窗口结束或异常按手册关闭并调查未决任务。

本轮到此停止，等待用户审核，不自行开始真实预发布。
