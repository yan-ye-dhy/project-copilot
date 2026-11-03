# 阶段1.5：公网部署阻塞项收尾报告

## 1. 目标及结论

基于`717f66a588f4d5eb09d71d1e1203f9fd695e1dd7`，落实邀请制业务策略、独立资源预检、冷备恢复、发布/Worker门禁和异常调查。**本轮范围内可离线实施的阻塞整改及验证已完成，可在用户明确授权下进入阶段二受控预发布；不等于具备直接PUBLIC条件。** 真实默认CLOSED，PDF503保留，没有启动隧道、登录云账号、调用付费模型或删除现有卷。

## 2. 文件及职责

- `docker-compose.public-demo.yml`、`scripts/public_resources.py`、`scripts/start_public_demo.sh`、`scripts/public_probe.py`：固定专用卷/网络、标签和挂载预检、消除环境覆盖、CLOSED准备和已授权Worker启动；原开发Compose保持。
- `db/release.py`、`services/release_control.py`、`scripts/release_control.py`、`infrastructure/public_gateway.py`、`infrastructure/call_accounting.py`：持久准入、配置/代码/资源/身份绑定、管理员交互授权、不可变事件及外呼前复核。
- `migrations/versions/0010_release_control.py`、`0011_public_operations.py`、`db/operations.py`、`services/approvals.py`、`api/actions.py`：数据库限额、并发锁、TTL、429和维护/恢复审计。
- `services/ai_worker.py`、`services/ai_audit.py`、`services/public_maintenance.py`、`services/worker_stop.py`、`scripts/public_maintenance.py`：执行租约、内容保留、只读调查、机器停机证明、实际审查工件与禁止重跑的终结。
- `api/jobs.py`、`web/assets/api.js`：内容清理后返回result_expired，前端不重新提交；视觉未修改。
- `scripts/public_backup.py`：停写冷备、归档校验、新卷恢复及真实数据核对、已验证备份保留与容量边界。
- `scripts/retrieval_benchmark.py`：真实HTTP Qdrant合成性能测量；新增相关测试及既有测试适配。`tests/conftest.py`中的合成准入仅限测试合同，不是生产绕过。
- 部署/安全/运行/发布清单文档及`public-demo.env.example`同步，新增入口规则方案、备份指南、性能报告和脱敏证据。

## 3. 关键执行流程

资源初始化前验证私有环境文件、真实渲染配置、固定卷/网络及消费者；不采用未知资源。CLOSED下仅准备存储、迁移和API。用户审核工件并交互授权STAGING后，受信代理密钥、Session/CSRF、身份白名单、归属和持久配额依次约束业务。Worker每次领取/外呼都复查发布状态、配置指纹和健康；CLOSED停止新增工作，在途可收尾。

Context和提案限制由PG触发器及用户事务锁执行，不能仅靠前端。清理仅去除允许过期的内容，保留幂等stub、FK、提案/确认和不确定调查证据。恢复必须CLOSED、禁Worker、独占执行边界、真实Docker停止inventory及已核实工件，原子写不可变审计，不重放任务、不推断供应商结果。

备份先停入口和写进程、核对CLOSED与所有业务/索引数据，再冷停存储归档。恢复只进入全新卷，按镜像身份启动并比较业务摘要、原文/向量及Dense/BM25查询；成功才标verified，不回写正式卷。

## 4. 设计取舍

发布不靠普通env开关；准入工件绑定代码、安全配置、资源、站点、时间及受限用户，配置漂移关闭。工件摘要不能证明语义，真实平台证据仍由管理员核实。状态与Worker开关分开，租约不能证明残留线程结束，故恢复还须停整个进程。管理员凭据验证事务先结束，再进行授权控制事务，避免交互期间持锁及上传/关闭死锁。

采用停机冷备，适合低并发演示，换取可验证的一致性；不宣称在线目录复制可靠。备份最多7份已验证非调查保留集，总预算2GiB；未验证/调查文件超额时拒绝而不清证据。提案及确认至少30天，本轮全保留，工单/知识不自动删除。Context有外键继续保留。无维护活动时不承诺精确到时的物理清除，WAL/日志/备份字节另受运维保护。

## 5. 实际测试及证据

执行者均为Codex自动验证，全部业务数据为合成；测试环境来自public-demo Compose派生的新随机资源，正式配置不发布数据库端口。

```bash
PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 PC_TEST_WEB_QDRANT=1 \
  OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 .venv/bin/python -m pytest -q -rs
```

最终代码完整回归：**1583 passed、6 skipped、0 failed**，104条Starlette测试逐请求Cookie弃用警告。6项跳过中，5项随后指向新测试服务另行启用并通过：

```bash
PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 \
PROJECT_COPILOT_RUN_QDRANT_INTEGRATION=1 PROJECT_COPILOT_RUN_FINAL_QDRANT=1 \
PROJECT_COPILOT_QDRANT_URL=http://127.0.0.1:16433 \
OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 .venv/bin/python -m pytest -q \
  tests/test_agent_integration.py tests/test_final_qdrant_integration.py tests/test_qdrant_integration.py
```

这5项证明真实PG/HTTP Qdrant、多进程索引和编译LangGraph存储集成，决策模型/Embedding仍为替身。剩余1项本地CUDA模型测试本轮未启用，不计为通过。真实EdgeOne、Tunnel、公开浏览器链路和真实模型质量/账单未执行。

前端、Chromium、静态构建及受控代理：下列组合 **71 passed、0 failed**，网络均为本地/测试替身：

```bash
PLAYWRIGHT_MODULE=/tmp/project-copilot-ui-qa/node_modules/playwright node --test \
  tests/frontend_auth_browser.test.mjs tests/frontend_jobs.test.mjs tests/frontend_auth.test.mjs \
  tests/edgeone_build.test.mjs tests/edgeone_proxy.test.mjs tests/edgeone_proxy_http.test.mjs
```

- 真实PG覆盖Context/提案并发、所有权、配额、状态关闭、恢复审计及内容清理。签名/时效/篡改测试使用合成Docker inventory；另用新建无网络真实容器验证停机采集器：运行时拒绝，退出且PID0后接受，见[采集器证据](evidence/STAGE15_DOCKER_STOP_PROBE.json)。它不是对真实公开任务执行恢复，也不冒充真实供应商对账。
- 真实HTTP Qdrant覆盖删除中断恢复和性能；备份真实冷停归档及新卷恢复见[脱敏收据](evidence/STAGE_1_5_BACKUP_RESTORE.json)，1合成工单、1文档/片段，全部ORM业务表和原文/向量摘要、Dense/BM25查询一致。小样本不能代表生产容量。
- 三规模性能见[报告](STAGE15_PERFORMANCE.md)：顺序请求中位约24.85/42.22/120.31ms，校验每次1/2/8批；4并发锁等待增加。模型替身，不含真实BGE/供应商时延；内存仅Python分配。
- 容器镜像实际构建成功，并在无网络/无卷临时容器检查默认403/503、0011迁移和不包含/app/.env；容器与工作区后端代码指纹一致。原Dockerfile远程frontend查询曾遇到Docker Hub EOF，改用仅移除syntax首行的临时副本与本机内置前端构建，应用构建步骤/锁定依赖相同，仓库Dockerfile未修改。普通在线构建仍依赖Registry可达。
- Compose不展开秘密的配置检查、git diff --check通过。所有本轮测试容器停止，新测试卷全部保留，原API/Phoenix未操作。

中途隔离端口未正确暴露的问题已修复；曾遇Docker网络地址池耗尽，只移除本轮新建的空测试网络后恢复验证，失败诊断保留。安全复核补强环境覆盖、固定网络、完整业务表摘要、恢复计划绑定与配置漂移门禁后重新执行相关测试及上述全回归。

## 6. GitHub

本批只提交已审核源码、测试、模板和脱敏证据到main，不包含.env、私有回执、归档、运行数据或权重。实际commit、push结果及本地/远端SHA核对以本次交付消息与Git记录为准；不以Git同步代替上线或用户亲自手测。

## 7. 已知边界

真实公网、免费版规则实际字段/效果、地区域名可用性、真实Cookie链路、CPU/GPU模型和更多语料容量需阶段二实测。公开PDF保持503。Docker/root属于受信运维权限，必须按脚本及维护窗口操作；人为绕过不能由应用完全约束。备份不包含模型缓存与Phoenix历史，追踪保留另行制定。供应商最终状态和业务结果须人工核实，程序验证工件身份而不猜测账单。

## 8. 阶段二交接

用户明确授权后，按[部署配置](EDGEONE_DEPLOYMENT.md)、[入口规则](EDGEONE_SECURITY_RULES.md)、[运行手册](PUBLIC_DEMO_RUNBOOK.md)和[发布清单](RELEASE_CHECKLIST.md)创建受限环境并采集真实证据。先STAGING，再独立验收，PUBLIC另需主动授权。当前无须无限追加本地模拟，但不能省略真实公网验证；本轮到此停止。
