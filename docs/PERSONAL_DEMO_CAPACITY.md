# 高额度 PERSONAL_DEMO：实现、边界与升级

## 1. 目标与实际范围

基于main `edaee27`升级已有单用户模式。高额度仅来自新的`personal-demo-v2`管理员授权，仍默认CLOSED、一个已有active账号、禁止开放注册、独立开启Worker、无自动续期。未修改真实任务、真实数据库或私有环境文件，未部署、重启现有Worker或调用付费模型。

## 2. 文件与额度对照

| 项目 | 旧个人模式 | 新个人模式默认值 |
| --- | --- | --- |
| 单次授权AI任务 | 3 | 500（失败仍计数） |
| 最长授权 | 90分钟 | 1440分钟 / 24小时 |
| 用户 / 全站UTC日任务 | 5 / 30 | 1000 / 2000 |
| 等待及执行任务占槽 | 1 | 3 |
| 部署Worker实例 | 1 | 1，按队列顺序执行 |
| Agent模型调用 | 4 | 24 |
| 工具调用尝试 | 6 | 80，分别统计执行与失败 |
| 供应商发起 | 16 | 160（发送前持久化，不自动重试） |
| Context每小时 / 活跃 | 5 / 3 | 100 / 30 |
| 提案UTC每日 / pending | 10 / 3 | 200 / 30 |
| Agent总时限 | 300秒 | 900秒 |
| 单次模型 / 工具时限 | 30 / 240秒 | 60 / 300秒 |
| DeepSeek输出上限 | 1024 tokens | 4096 tokens |
| 普通前端等待 | 8分钟 | 个人模式50分钟分段查询，允许停止和恢复 |
| 单连接SSE | 后端20秒，代理25秒 | 不变；不是15分钟HTTP连接 |
| Worker停止排空宽限 | 360秒 | 个人镜像覆盖1260秒 |

STAGING/PUBLIC及普通本机模式的原配置默认值、校验上限、认证和发布规则不变。Context仍有效24小时，提案仍15分钟，登录Session仍从登录时计24小时；个人授权不延长旧Cookie，必要时重新登录而不是续期授权。Nginx全局限流、提交审计短窗及PDF关闭门禁均保留，因此不能承诺任何429都消失。

主要文件：

- `config.py`、`services/personal_profile.py`：独立的`PERSONAL_*`字段、500/24h上限和派生执行设置。
- `services/release_control.py`、`scripts/release_control.py`、`services/quotas.py`：v2授权快照、额度、三槽及500次累计。
- `0015_personal_capacity.py`：新增任务失败标识，替换限额函数；没有修改0011或清空历史。
- `agent/{graph,state,tools}.py`、`schemas/agent.py`：规划提示、只读去重、无进展终止、执行/失败计数与900秒支持。
- `services/{ai_worker,execution_slots,job_failures,job_events}.py`、`db/jobs.py`：真实排空后决定failed/unknown、持久化安全失败类型和终态事件。
- `api/{jobs,web}.py`、`web/`：授权用户额度显示、明确429原因、长任务查询/恢复、真实进度和失败说明。
- `deploy/personal-demo.image.yml`、`public-demo.env.example`及相关Python/PG/Node/浏览器测试。

## 3. 完整执行流程与设计决策

管理员从CLOSED重新授权 → v2记录唯一用户、期限、配置/代码摘要及完整limits快照 → HTTP认证/CSRF/来源检查 → 原子持久化幂等任务及额度 → 三槽内排队 → 一个Worker领取 → 派生个人执行设置 → 模型自主选择工具 → 真实工具及供应商进度 → 最终Schema/真实来源校验 → 真实后台Future排空 → 同事务确定状态、写终态事件和fencing/释放槽 → SSE或原job_id恢复读取。

配额累计所有结果及跨日旧记录；500次属于授权窗口，1000/2000属于UTC日，不互相替代。低于默认值的个人字段可以显式配置，不能超过上述上限。派生设置只用于已领取的个人任务，外呼仍用原配置核验授权指纹，避免执行副本误触发其他模式。只部署一个Worker；测试证明单Worker顺序执行，不宣称手动启动多个Worker时有全局调度互斥，不使用`--scale worker`。

管理员显式CLOSED时，仅结束**当前v2授权**中尚未领取的reserved、provider_attempts=0且没有执行登记/异常结果的队列，标记`failed/grant_ended`并释放槽；保留任务、额度和幂等记录，移除未领取的临时上传字节。它们不会在新授权下重跑，也不会再阻塞新窗口24小时。已运行/unknown、旧授权及v1历史均不处理。若队列核实遇数据库错误，保存点回滚队列变化，CLOSED仍须成功；需要调查时保留占槽证据，可在修复后再次显式CLOSED核对从未执行的队列。

数据库触发器在发布共享锁、用户锁下读取真实ReleaseControl及实时计数。只有有效v2、一个匹配active账号、合法期限及受限快照才使用高额度；PUBLIC/STAGING、其他用户、CLOSED、过期/无效快照均使用原低限额。客户端GUC、请求头或前端显示不能提高数据库限额。

Agent对本次run内相同规范化参数的成功只读调用不再次执行，返回指向先前ToolMessage的`duplicate_read`提示；不缓存/重放数据，不跨请求复用，不缓存写工具。三轮无新观察/无进展会明确终止为`repeated_tool_loop`或`tool_no_progress`。多tool_calls每项都计尝试；`tool_executions`只计实际调用，`tool_failures`包含拒绝/异常，事件带`executed`。提案仍只是预览，真正创建必须走独立确认接口。

模型最后一个多工具消息可能声明超过剩余预算的调用；拒绝的声明仍准确记入尝试/失败统计并配对ToolMessage，不能把统计裁掉伪装没有请求。超过80预算的工具不会执行。供应商预算在真实Graph模型和工具节点都识别为`provider_budget`，不再被通用异常掩盖，本批其余工具也不会继续执行。

900秒是Graph停止发起后续决策/等待的逻辑时限，不能强杀已启动的同步线程。其物理排空可能继续超过900秒；此期间显示真实“等待工具结束”，保留占槽，不处理下一任务，不谎称取消或退费。正常三任务各15分钟的排队由50分钟前端查询覆盖；异常排空更久仍可停止等待并按原编号恢复。60,000字符上下文、每条模型响应最多12个tool_calls及最终answer 4000字符合同仍保留，避免将工具数据无限塞进上下文；4096是生成tokens上限，不是绕过最终合同。

## 4. failed与unknown的边界

| 执行结果 | 持久化 |
| --- | --- |
| model_budget / tool_budget / provider_budget / 无进展 / 最终校验失败，且供应商、线程及业务绑定已核实结束 | `failed`，安全failure_type，释放槽；不退款/重跑 |
| 工具超时，但物理线程仍活 | 保持running并显示排空进度；不能提前终态或放槽 |
| 同步工作结束但供应商响应失联、回执未写入、业务绑定不一致 | `unknown`并继续占槽，保留PR #11人工边界 |
| 未分类执行异常 | `unknown`；若仅本地结果不确定但执行结束证据齐全，可仅释放槽 |
| 管理员明确关闭当前v2授权中的未领取队列 | `failed/grant_ended`；没有外呼、不重放，窗口/日计数仍保留 |
| 数据库提交结果不明/进程崩溃 | 不推断失败或成功；保留原租约/回收/幂等规则，不能重放 |
| 工具先前超时但最终有效回答、物理工作和供应商均已完成 | 保留通过校验的成功回答，不因一个已恢复工具错误一概失败 |

终态在实际排空后写入，网页失败包含安全类型和原任务编号；unknown不会伪装为failed。未知usage保留None，cost_status仍unknown，模型Token估算不等于账单。

现有Agent usage汇总来自已返回且通过校验的对话模型usage，不是DeepSeek与Embedding的统一账单；未采集的Embedding用量不能当成0。供应商发起次数、逻辑任务数、对话tokens和实付费用继续分开解释。

任务`051fd1eb-979e-4a83-852b-662b65f43342`：**本轮没有直连真实库复核**。按用户提供的真实记录，它为tool_budget/unknown、provider_open=0、provider_uncertain=false、slot_held=false、release_reason=drained，故该记录报告为不占槽。0015不会回填或改变它的状态、失败原因或费用；旧任务若已有安全diagnostic，API可只读显示该原因，没有记录时不补造。

## 5. 自动验证

执行者为Codex，全部模型/供应商及Worker响应受控，PG使用独立5434随机数据库，浏览器请求拦截。测试涵盖500/24h、1000/2000、三槽、160发起、单Worker顺序、24/80、重复工具/无进展、数据库高低限额及竞争、known failed/unknown、原物理租约/fencing、确认幂等、长等待/SSE/引用权限。

| 最终验证 | 结果 |
| --- | --- |
| 开启隔离PG的完整Python回归 | **1916 passed、17 skipped、0 failed**，285.41秒 |
| 新Agent预算/循环测试 | 18项，包含真实编译图运行；供应商均为替身 |
| 新个人配额/执行与授权关闭测试 | 17项，含两条编译Graph＋PG＋Worker供应商预算路径 |
| 新Context/提案数据库策略 | 29项，包含真实PG阈值、并发及0014→0015保留数据 |
| 新执行终态/历史诊断读取 | 9项，含供应商不确定性和只读保留历史 |
| 完整Node／浏览器回归 | **240 passed、0 failed、0 skipped**，27.49秒 |
| 中文桌面/移动视觉复核 | 10个本机字体辅助视觉用例通过；字体不入库、不远程下载 |
| 升级命令语法 / Git diff | 4个Bash块、1段内嵌Python及diff检查通过 |

Python专项包含在全量计数中，不重复相加。17项GPU、Qdrant/外部环境等可选集成未启用；不宣称真实模型、公网部署或新Docker镜像验收通过。首次全量发现旧API精确字段与一槽断言需要按新合同更新，更新后通过；后续补齐实际Graph供应商预算和关窗队列保护，再完成上述最终全量。弃用警告不影响通过结果。

```bash
PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 PYTHONPATH=src:. \
  .venv/bin/python -m pytest -q --disable-warnings
node --test --test-isolation=none tests/*.test.mjs
git diff --check
```

真实模型、公网SSE、Docker升级不属于本轮离线测试，不把替身或历史公网结果当作本版已部署证据。

## 6. Git交付与审核后的WSL升级

独立分支`feat/personal-demo-high-capacity`，PR指向main，实际SHA/链接在交接中提供。本轮不合并、不修改EdgeOne、Tunnel或现有容器。用户合并main会按自己已有的云端自动部署设置发布前端，需自行选择时机。

下面仅在审核合并后执行，每块失败即停止。不要覆盖`.env.public-demo`、重建资源/管理员或使用`down -v`。这是0015升级，不要继续使用旧0014文档中的固定head断言。

```bash
cd /home/yanye/project-copilot
git status --short --branch
git fetch origin main
git switch main
git merge --ff-only origin/main
.venv/bin/python -m scripts.public_resources check --env-file .env.public-demo
pc_demo=(docker compose --env-file .env.public-demo \
  -f docker-compose.public-demo.yml -f deploy/personal-demo.image.yml)
"${pc_demo[@]}" exec api python -m scripts.release_control CLOSED
"${pc_demo[@]}" --profile tunnel stop cloudflared ingress api worker migrate
```

API已停止时，关闭命令改用`"${pc_demo[@]}" run --rm --no-deps --pull never api python -m scripts.release_control CLOSED`。CLOSED会同时禁用Worker。保留全部卷及原退出容器。

```bash
pc_lock_local=$(sha256sum uv.lock | cut -d ' ' -f 1)
pc_lock_image=$(docker run --rm --network none --entrypoint python project-copilot-api:public-demo \
  -c "from pathlib import Path; from hashlib import sha256; print(sha256(Path('/app/uv.lock').read_bytes()).hexdigest())")
test "$pc_lock_local" = "$pc_lock_image"
docker build --pull=false --network=none -f Dockerfile.personal-demo \
  -t project-copilot-api:personal-demo .
pc_expected=$(.venv/bin/python -c 'from project_copilot.services.release_control import code_fingerprint; print(code_fingerprint())')
pc_actual=$(docker run --rm --network none --entrypoint python project-copilot-api:personal-demo \
  -c 'from project_copilot.services.release_control import code_fingerprint; print(code_fingerprint())')
test "$pc_expected" = "$pc_actual"
pc_cli_local=$(sha256sum scripts/release_control.py | cut -d ' ' -f 1)
pc_cli_image=$(docker run --rm --network none --entrypoint python project-copilot-api:personal-demo \
  -c "from pathlib import Path; from hashlib import sha256; print(sha256(Path('/app/scripts/release_control.py').read_bytes()).hexdigest())")
test "$pc_cli_local" = "$pc_cli_image"
"${pc_demo[@]}" run --rm --no-deps --pull never -T migrate
"${pc_demo[@]}" run --rm --no-deps --pull never -T migrate python - <<'PY'
import os, stat
from pathlib import Path
from sqlalchemy import text
from project_copilot.config import load_settings
from project_copilot.db.session import Database
from project_copilot.db.migrations import check_model_drift
from project_copilot.services.release_control import release_status
s = load_settings()
db = Database(s.database_url.get_secret_value())
try:
    with db.transaction() as session:
        session.execute(text('SET TRANSACTION READ ONLY'))
        head = session.scalar(text('SELECT version_num FROM alembic_version'))
        state = release_status(session)
        assert head == '0015_personal_capacity'
        assert state['state'] == 'CLOSED' and state['worker_enabled'] is False
    check_model_drift(db)
finally:
    db.close()
manifest = Path(s.manifest_dir)
assert manifest.is_dir() and not manifest.is_symlink()
assert manifest.stat().st_uid == os.getuid() and os.access(manifest, os.W_OK)
lease = manifest / '.job-executions'
if lease.exists() or lease.is_symlink():
    info = lease.lstat()
    assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077
print({'migration': head, 'state': 'CLOSED', 'worker_enabled': False})
PY
```

依赖锁不一致、离线基础镜像缺失、源码/CLI摘要或Manifest权限不一致时停止；不下载不明依赖、不拿旧镜像冒充新代码，不递归chown或删除旧锁文件。API/Worker/migrate继续共享同一个固定Manifest卷及UID。

## 7. 私有配置及重新授权

默认值已经生效；可只向现有私有文件**追加需要调整的非秘密PERSONAL项**，不要复制示例覆盖已有密钥。完整名称见`public-demo.env.example`中`PROJECT_COPILOT_PERSONAL_*`：日任务1000/2000、占槽3、供应商160、模型24/工具80、模型60秒/工具300秒/总900秒、输出4096、Context100/30、提案200/30。普通`MAX_DAILY_REQUESTS_*`等原字段继续约束其他模式，不必提高。

代码、配置或迁移更新后必须从CLOSED显式重新授权，不能沿用旧profile v1或旧代码摘要。用户名替换为原有active账号，不重新注册：

```bash
"${pc_demo[@]}" run --rm --no-deps --pull never api \
  python -m scripts.release_control PERSONAL_DEMO --user '已有测试账号用户名' --minutes 1440
"${pc_demo[@]}" run --rm --no-deps --pull never api \
  python -m scripts.release_control worker-enable
"${pc_demo[@]}" up -d --no-build --pull never api ingress
"${pc_demo[@]}" up -d --no-build --pull never worker
"${pc_demo[@]}" exec api python -m scripts.release_control inspect
```

交互仍需管理员密码、精确AUTHORIZE短语及原因。授权只绑定一个已有账号，Worker独立开启，不自动延长。以上命令由用户选择执行，本轮未运行；Tunnel及EdgeOne目的地仍由用户按既有流程明确授权，不能因本PR存在就自动开放。

## 8. 后续个人体验与限制

页面应显示剩余时间、已用/500、占槽/3及执行器状态；429区分窗口、用户日、全站日及并发，失败展示安全原因。可连续使用RAG和自主Agent，但不无限增加模型输出或重复检索。提交响应不确定时先查原编号，停止等待、刷新恢复和SSE重连都不再POST。

实际体验时先核对部署版本和授权，再使用已有知识/工单。预算耗尽应显示明确failed原因；上游失联和业务不确定仍显示unknown。工单提案不等于工单，依然必须独立预览、确认，重复确认返回同一ticket_id。本轮没有新的付费验证；之后的真实调用由用户手动发起，实际费用以供应商账单为准。
