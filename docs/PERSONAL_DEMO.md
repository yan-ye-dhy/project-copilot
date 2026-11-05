# 个人公网体验（PERSONAL_DEMO）

一次短期个人体验，与STAGING/PUBLIC独立，不需要八份证据。仅限隔离public-demo环境、一个已有active账号；最长90分钟，无自动续期。每次授权最多**3个已提交AI任务**（RAG、Agent、上传合计），失败仍计数、相同幂等键不重跑；并发1，原日配额和供应商尝试预算继续执行。一个Agent任务可能有多次模型/Embedding调用，3任务不等于3次供应商请求。

并发槽与任务状态独立：新版本领取的个人任务只有在真实后台工作结束、供应商完成记录明确后才自动释放，`unknown`、usage、费用及幂等记录不变。正常成功也走此检查；线程取消/超时不等于线程结束。崩溃回收使用共享本机文件锁和数据库 fencing，不按任务年龄放槽。旧任务没有证明、供应商超时/流中断或锁证据缺失时保留占槽与既有人工核实入口，见[执行槽说明](PERSONAL_EXECUTION_SLOTS.md)。

仅指定历史任务的交互式剩余风险处理及新版Docker升级顺序见[历史任务升级说明](LEGACY_PERSONAL_DEMO_UPGRADE.md)。不允许该例外处理任意unknown；原任务及额度保留，真实操作等待用户审核授权。

本轮本机自动验证：相关回归 **212 passed，0 failed，0 skipped**；真实PostgreSQL使用独立临时测试集群/随机数据库，模型及检索依赖受控。包括实际FastAPI/编译LangGraph/Worker/工单确认闭环、并发和到期回滚。独立新镜像已构建并核对源码、锁文件、0012迁移；没有切换真实发布状态或发起付费模型请求。测试中的弃用提示不影响通过结果。

本轮自动测试不代表已在公网开放。以下迁移/服务更新、授权、Tunnel及云变量操作均由用户亲自执行；本轮没有更改真实环境。先审核本分支，别直接合并main触发前端自动生产部署；代理无需本次改动。

## 1. 加载新后端（仍CLOSED）

本轮没有新增依赖；使用已缓存CPU依赖镜像复制**新源码和迁移**，不向Docker Hub拉取。当前源代码必须在本机；不能复用旧运行容器冒充新代码。若缺基础镜像或锁文件/依赖已变化，停下并使用正式Dockerfile构建，不能跳过更新。

```bash
cd ~/project-copilot
docker build --pull=false --network=none -f Dockerfile.personal-demo -t project-copilot-api:personal-demo .
# 只覆盖api/worker/migrate镜像，端口、卷、健康门禁等仍继承原独立Compose。
pc_demo=(docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml -f deploy/personal-demo.image.yml)
"${pc_demo[@]}" run --rm migrate
"${pc_demo[@]}" up -d --no-build --pull never api ingress
"${pc_demo[@]}" exec api python -m scripts.release_control inspect
```

只升级现有隔离演示库，0012保留已有数据及审计；不初始化/重建管理员或数据卷。应仍见CLOSED/worker=false，health暂时unhealthy符合预期。可用下面两条**只读**命令对比当前代码指纹，必须完全相同；不输出私有配置：

```bash
.venv/bin/python -c 'from project_copilot.services.release_control import code_fingerprint; print(code_fingerprint())'
"${pc_demo[@]}" exec -T api python -c 'from project_copilot.services.release_control import code_fingerprint; print(code_fingerprint())'
```

## 2. 本机管理员开启与启动Worker

`--user`填已有测试账号用户名（可以是现有管理员，原角色权限保持）。命令会要求管理员密码、精确的 `AUTHORIZE PERSONAL_DEMO` 和无秘密原因；不要把密码写进参数。只允许从CLOSED开启，期限不能超过90分钟。

```bash
"${pc_demo[@]}" exec api python -m scripts.release_control PERSONAL_DEMO --user '替换为已有测试账号用户名' --minutes 90
"${pc_demo[@]}" exec api python -m scripts.release_control worker-enable
"${pc_demo[@]}" up -d --no-build --pull never worker
```

应见真实state=PERSONAL_DEMO和expires_at，Worker开启需要单独 `AUTHORIZE worker-enable`。不是把CLOSED伪装成开放；到期即拒绝新业务、领取和新的供应商调用，即使容器还在运行。inspect显示实际状态与expired，不自动续期。原任务收尾/已发出的远端请求不能保证被取消。旧窗口任务不能在新窗口续跑，unknown不自动退款/重试。

## 3. 启Tunnel并配置EdgeOne

```bash
"${pc_demo[@]}" --profile tunnel up -d --pull never cloudflared
```

待API/ingress健康后才能启动。**只在自己终端私下**查看cloudflared日志获得本人的HTTPS trycloudflare origin，填入EdgeOne生产函数服务端 `PROJECT_COPILOT_BACKEND_ORIGIN`（只有origin，无路径/查询）。保留SITE_ORIGIN和代理密钥；不要发聊天、截图或Git。变量生效所需的新部署由用户在控制台执行，先确认目标是当前生产版本，不合并未审main。不要把模型Key放EdgeOne或浏览器。

## 4. 体验与关闭

打开现有EdgeOne页面，登录唯一测试账号。可先发一次RAG知识问答，查看任务状态与原文引用；再用Agent查询自己的工单/知识并请求提出工单，预览后点击独立确认，查看真实ticket_id与重复确认幂等。提交1次Agent任务可有多步工具调用；失败先看任务状态，不重新点击制造新任务。邀请码/其他账号不能扩展本次体验。资料上传仍要求原管理员权限且计入3任务，公开PDF继续关闭；使用既有资料可节省额度。

体验结束立即：

```bash
"${pc_demo[@]}" exec api python -m scripts.release_control CLOSED
./scripts/stop_public_demo.sh
```

第一条输入 `AUTHORIZE CLOSED`；第二条只停演示容器，不删除卷。另在EdgeOne清空BACKEND_ORIGIN并按平台要求部署生效，停止转发；本机停止后健康/me应503或上游离线错误。CLOSED/过期不会取消已经发送的模型请求；running/unknown保留调查记录，缺少执行结束证明时继续占槽，具备新协议证明的个人任务可在正常收尾或后续开放后的领取/准入检查中释放槽位。停止Tunnel不能阻止云函数自身继续计次，本模式不承诺云端免费额度防滥用或生产安全验收。若报503先查期限、新镜像/迁移、账号active、依赖与配置，不改health或放宽PUBLIC。
