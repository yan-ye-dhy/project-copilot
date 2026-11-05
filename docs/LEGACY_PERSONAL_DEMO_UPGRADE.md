# 历史 unknown 槽位退役与个人演示升级

本文为PR #12的0014阶段操作记录。后续高额度升级使用[0015操作说明](PERSONAL_DEMO_CAPACITY.md)，不要继续执行下文固定0014的head断言；指定历史任务例外和风险边界没有扩大。

## 1. 本轮目标与边界

基于合并PR #10、#11后的main `bb750c2`，只为任务`3f72446c-6aed-470f-85da-774004b97555`增加一个本机交互式例外。没有执行真实退役、Docker升级、发布授权或付费请求。下面的升级步骤**仅供本PR审核合并后由用户执行**，每块成功后才继续；任一核对失败立即停止。

供应商结果和费用可以明确保持`unverified`，这不是远端完成或取消证明。本例外只在已证明本地执行停止、排除未核实业务副作用后，允许管理员接受剩余的远端/费用不确定性。普通`resolve-job`仍要求供应商已核实，不接受`unverified`；新任务仍走PR #11执行租约和fencing。

## 2. 修改文件

| 文件 | 职责 |
| --- | --- |
| `services/public_maintenance.py` | 固定历史身份、归属/业务/停止核查，单槽更新及幂等审计；扩充脱敏inspection身份摘要 |
| `scripts/public_maintenance.py` | 专用交互命令、固定个人镜像选项、私有inspection输出 |
| `tests/test_legacy_job_retirement_postgres.py` | 隔离PG、模拟Worker、事务与并发验证 |
| `tests/test_legacy_maintenance_cli.py` | 交互、镜像选择、私有输出和错误路径测试 |
| 本文及既有运行手册链接 | 实际升级顺序、风险边界和一次公网验收 |

## 3. 核心实现与安全决策

- 固定UUID、Agent/unknown、创建时间`2026-11-05T09:38:17.911814Z`、3次供应商发起和原始五项usage（1389/102/1491/896/493），要求result及finished_at为空、无新执行登记、原个人授权标识仍存在。
- 收据必须绑定原owner/context、创建时间及最新inspection摘要。服务端重新核对Session/Context归属，拒绝任何Context提案和该owner的孤立创建键工单。原公开诊断未披露owner/context UUID，**不能从本文猜测**，必须由管理员对照私有历史记录核实；无法核实则不操作。
- 复用管理员认证、CLOSED且Worker禁用、数据库准入/执行排他锁、全部Manifest卷消费者停止检查及60秒有效签名停止证明。保留已退出的旧Worker，不能先删除/重建它。只支持当前单主机Docker执行边界；若曾有宿主Worker或其他执行器且无法证明已终止，不得确认。
- 只更新`slot_released_at`。原state/usage/费用/调用数/任务正文/幂等键/授权及日配额记录都不修改，不重放、不退款。唯一`JobRecoveryAudit`与既有`MaintenanceAudit.counts`中的结构化元数据同事务提交；元数据记录`provider_outcome=unverified`、管理员风险确认、原字段摘要和停止/审查摘要，无需0015迁移。
- 重复命令仍须管理员、CLOSED、新停止证明及同一原收据，核对唯一审计和完整摘要后仅返回原时间；不新增审计。inspection只规范化本次释放造成的三个字段，其他变化继续拒绝。

## 4. 用户审核后执行的WSL升级顺序

不要修改`.env.public-demo`中的密钥，不初始化资源或管理员，不运行`down`、`down -v`、`rm worker`或清卷命令。PostgreSQL/Qdrant/Phoenix和四个固定卷保持原样。命令均在同一WSL终端执行，管理员密码只在提示时输入。

### 4.1 更新main，检查独立资源与离线依赖

```bash
cd /home/yanye/project-copilot
git status --short --branch
git fetch origin main
git switch main
git merge --ff-only origin/main
.venv/bin/python -m scripts.public_resources check --env-file .env.public-demo

pc_demo=(docker compose --env-file .env.public-demo \
  -f docker-compose.public-demo.yml -f deploy/personal-demo.image.yml)

pc_lock_local=$(sha256sum uv.lock | cut -d ' ' -f 1)
pc_lock_image=$(docker run --rm --network none --entrypoint python \
  project-copilot-api:public-demo -c \
  "from hashlib import sha256; from pathlib import Path; print(sha256(Path('/app/uv.lock').read_bytes()).hexdigest())")
test "$pc_lock_local" = "$pc_lock_image"
```

Git有未提交改动、main无法快进、资源归属不符、基础镜像缺失或锁文件不一致时停止，不重置历史、不采用旧镜像冒充更新。当前基础镜像必须已包含同版离线依赖；依赖不一致需要另行准备镜像。本轮没有新增依赖。

### 4.2 在旧运行环境关闭，再停执行与公网入口

```bash
"${pc_demo[@]}" exec api python -m scripts.release_control CLOSED
```

交互输入管理员凭据、`AUTHORIZE CLOSED`及不含秘密的原因。CLOSED同时禁用Worker。若API本来已停止，用下面命令**替代**上一条，不启动API服务：

```bash
"${pc_demo[@]}" run --rm --no-deps --pull never api \
  python -m scripts.release_control CLOSED
```

授权成功后：

```bash
"${pc_demo[@]}" --profile tunnel stop cloudflared ingress api worker migrate
```

等待停止完成；不要删除容器。停止超时后也不能仅凭等了几分钟就确认，后续维护命令会核查真实PID/退出状态及数据库执行锁。云端无需修改，本机入口停止后不能转发业务。

### 4.3 从缓存构建，并验证新源码确实进入镜像

```bash
docker build --pull=false --network=none -f Dockerfile.personal-demo \
  -t project-copilot-api:personal-demo .

pc_code_local=$(.venv/bin/python -c \
  'from project_copilot.services.release_control import code_fingerprint; print(code_fingerprint())')
pc_code_image=$(docker run --rm --network none --entrypoint python \
  project-copilot-api:personal-demo -c \
  'from project_copilot.services.release_control import code_fingerprint; print(code_fingerprint())')
test "$pc_code_local" = "$pc_code_image"

pc_cli_local=$(sha256sum scripts/public_maintenance.py | cut -d ' ' -f 1)
pc_cli_image=$(docker run --rm --network none --entrypoint python \
  project-copilot-api:personal-demo -c \
  "from hashlib import sha256; from pathlib import Path; print(sha256(Path('/app/scripts/public_maintenance.py').read_bytes()).hexdigest())")
test "$pc_cli_local" = "$pc_cli_image"
```

代码指纹覆盖后端/迁移/锁文件，额外检查维护CLI。不要用继承的旧VCS镜像标签证明源码已更新。构建要求拉网或摘要不一致时停止；不要覆盖`public-demo`基础依赖镜像标签。

### 4.4 迁移0014并只读验证CLOSED、UID及Manifest

```bash
"${pc_demo[@]}" run --rm --no-deps --pull never -T migrate

"${pc_demo[@]}" run --rm --no-deps --pull never -T migrate python - <<'PY'
import json, os, stat
from pathlib import Path
from sqlalchemy import text
from alembic.script import ScriptDirectory
from project_copilot.config import load_settings
from project_copilot.db.migrations import migration_config, check_model_drift
from project_copilot.db.session import Database
from project_copilot.services.release_control import release_status
s = load_settings()
db = Database(s.database_url.get_secret_value())
try:
    with db.transaction() as session:
        session.execute(text('SET TRANSACTION READ ONLY'))
        version = session.scalar(text('SELECT version_num FROM alembic_version'))
        state = release_status(session)
        assert version == ScriptDirectory.from_config(migration_config()).get_current_head() == '0014_execution_slots'
        assert state['state'] == 'CLOSED' and state['worker_enabled'] is False
    check_model_drift(db)
finally:
    db.close()
p = Path(s.manifest_dir)
assert p.is_dir() and not p.is_symlink() and p.stat().st_uid == os.getuid() and os.access(p, os.W_OK)
lease = p / '.job-executions'
if lease.exists() or lease.is_symlink():
    info = lease.lstat()
    assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077
print(json.dumps({'migration': version, 'state': state['state'], 'worker_enabled': False,
                  'uid': os.getuid(), 'manifest_uid': p.stat().st_uid, 'manifest_writable': True}))
PY
```

应看到head为0014、CLOSED、false、UID一致且可写。API/Worker/migrate继承同一个Compose UID/GID及`pc-public-v2-manifests:/app/data/manifests`，个人镜像覆盖只改变image。租约目录尚不存在对旧任务正常，不要手工创建旧任务证明或重建锁文件；权限不符停止调查，不对已有证据做递归chown。

### 4.5 检查、核实原归属、生成私有收据

```bash
.venv/bin/python -m scripts.public_maintenance inspect-job \
  --job-id 3f72446c-6aed-470f-85da-774004b97555 --personal-image \
  --output data/legacy-retirement/inspection-before.json
```

`--personal-image`不可省略：基础Compose的默认image可能仍是旧依赖镜像。输出应保持unknown/3/1491、`result_present=false`、`execution=null`、`actions=[]`及`slot_held=true`。管理员必须核实原归属、所有原执行已停止及业务副作用，不因“没有最终回答”认定没有执行。无法确定这些条件时停在这里，不生成收据。

完成核实后，输入从**原私有记录**核实的两个UUID；脚本还会与最新检查逐项匹配。不要把账号密码或Cookie填入这些字段：

```bash
read -r -p '已核实的原 owner_user_id：' pc_legacy_owner
read -r -p '已核实的原 context_id：' pc_legacy_context
PC_LEGACY_OWNER="$pc_legacy_owner" PC_LEGACY_CONTEXT="$pc_legacy_context" \
  .venv/bin/python - <<'PY'
import hashlib, json, os
from pathlib import Path
from uuid import UUID
from project_copilot.services.public_maintenance import LEGACY_JOB_ID, LEGACY_CREATED_AT, LEGACY_USAGE
p = Path('data/legacy-retirement')
info = json.loads((p/'inspection-before.json').read_text())['result']
owner, context = str(UUID(os.environ['PC_LEGACY_OWNER'])), str(UUID(os.environ['PC_LEGACY_CONTEXT']))
assert info['owner_user_id'] == owner and info['context_id'] == context
assert info['job_id'] == str(LEGACY_JOB_ID) and info['kind'] == 'agent' and info['state'] == 'unknown'
assert info['provider_attempts'] == 3 and info['usage'] == LEGACY_USAGE
assert info['actions'] == [] and info['execution'] is None and not info['result_present'] and info['slot_held']
review = {'job_id': info['job_id'], 'owner_user_id': owner, 'context_id': context,
          'inspection_sha256': info['inspection_sha256'], 'provider_outcome': 'unverified',
          'review': 'Administrator reviewed original ownership and local/business termination evidence; '
                    'provider per-request terminal outcome and final cost remain unverified. '
                    'Fresh host stop inventory and database business checks are required at retirement.'}
raw = (json.dumps(review, ensure_ascii=False, indent=2)+'\n').encode()
receipt = {'version': 1, 'job_id': info['job_id'], 'expected_owner_user_id': owner,
           'expected_context_id': context, 'created_at': LEGACY_CREATED_AT.isoformat(), 'provider_attempts': 3,
           'inspection_sha256': info['inspection_sha256'], 'original_owner_verified': True,
           'original_execution_terminated': True, 'business_verified': True, 'provider_outcome': 'unverified',
           'evidence_reference_sha256': hashlib.sha256(raw).hexdigest(), 'decision': 'retire_legacy_slot_no_retry_v1'}
for name, data in [('review.json', raw), ('receipt.json', (json.dumps(receipt, indent=2)+'\n').encode())]:
    with os.fdopen(os.open(p/name, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600), 'wb') as f:
        f.write(data)
print('私有收据已生成；尚未退役。')
PY
```

这些本地文件在忽略的data目录，禁止提交或公开。已存在文件时拒绝覆盖；需要重新调查时用新的私有目录/文件名，不删除旧调查记录。

### 4.6 管理员交互退役，再次只读检查

```bash
.venv/bin/python -m scripts.public_maintenance retire-legacy-job \
  --job-id 3f72446c-6aed-470f-85da-774004b97555 --personal-image \
  --evidence data/legacy-retirement/receipt.json \
  --review-artifact data/legacy-retirement/review.json

.venv/bin/python -m scripts.public_maintenance inspect-job \
  --job-id 3f72446c-6aed-470f-85da-774004b97555 --personal-image \
  --output data/legacy-retirement/inspection-after.json
```

第一条要求完整输入`ACCEPT UNVERIFIED PROVIDER RISK 3f72446c-6aed-470f-85da-774004b97555`，随后认证管理员；所有交互结束后才采集新鲜停止证明。任何门禁拒绝都不得改成直接SQL更新。

成功应为`provider_outcome=unverified`、`slot_released=true`，`quota_before == quota_after`。检查前后state仍unknown、3次发起、1491 tokens、cost_status仍unknown、幂等摘要/授权/UTC日不变；只有slot标记和`manually_resolved`改变。重复同一收据会返回`already_retired=true`及相同audit_id/时间，不新增记录。**升级收尾到此为止：保持CLOSED，API/Worker/Ingress/Tunnel不启动。**

## 5. 离线自动验证

执行者为Codex；使用独立`postgres-test`（5434、随机测试库）及模型/Worker替身，未访问真实演示库或供应商。实际结果：

| 验证 | 结果 |
| --- | --- |
| 启用隔离PG的完整Python回归 | **1843 passed、17 skipped、0 failed**，225.53秒 |
| 新增历史退役PG专项 | **41 passed**，含模拟Worker真实线程锁、并发重复管理员与实际SQL回滚 |
| CLI及原停止证明专项 | **29 passed** |
| Node代理/界面回归（本机Playwright缓存） | **219 passed、0 failed、0 skipped** |
| 本文命令语法检查 | 10个bash块、2段内嵌Python通过；未执行其中的升级命令 |
| Git diff检查 | 通过 |

17项可选外部环境测试未启用，不代表真实模型、Qdrant或Docker发布验收通过。命令如下（Node测试使用已安装的本机Playwright缓存）：

```bash
PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 PYTHONPATH=src:. \
  .venv/bin/python -m pytest -q --disable-warnings
node --test --test-isolation=none tests/*.test.mjs
git diff --check
```

真实本地Docker升级、真实历史退役和公网SSE均未执行，不能用这些测试宣称上线验收完成。

## 6. Git交付

独立分支`fix/legacy-personal-job-retirement`，指向main的PR，实际commit与链接在交接中提供。不自动合并、部署或触碰现有工作树的运行配置。

## 7. 之后由用户执行的一次公网验收

仅在用户另行授权开启PERSONAL_DEMO、Worker与Tunnel并确认EdgeOne目标后进行，复用原账号及已上传资料，不重新上传或额外生成演示数据。开启/关闭命令沿用[个人演示说明](PERSONAL_DEMO.md)，启用新版API时仍选择个人镜像覆盖；不要在CLOSED下使用`up --wait api`或先启动Worker。

1. 刷新EdgeOne页面，核对PR #10的“知行助手 / Project Copilot”、知字印章、用户SVG和透明助手正文。浏览器Network打开Preserve log。
2. **仅手动发送一次RAG问题**。观察唯一一次`POST /api/jobs`返回202并保留新job_id；失败/结果不确定时不自动或手动重复POST。历史占槽原因应已消失，但日/全站/三任务额度仍可能合法返回429，不放宽或退款。
3. 检查`GET /api/jobs/<新ID>/events`为`text/event-stream`，真实progress/delta在terminal及最终succeeded前抵达；最终回答校验通过后核对知识引用原文。若事件直到完成才整体到达，公网流式验收未通过，保留轮询降级与诊断，不为测试再发第二条付费请求。
4. 再用管理员inspect旧任务（新的输出名），核对仍unknown、provider3、1491、原费用/幂等和已释放标记，未出现新调用或重放。结束后授权CLOSED并停入口/Worker/Tunnel，保留存储卷。

## 8. 剩余限制与失败处理

本工具不能证明供应商最后一笔费用或远端执行终态，不能恢复丢失的回答，不能把旧任务补造为新协议执行。缺少原归属记录、停止证明、旧Worker容器、业务核实或私有工件时，继续占槽并保留证据；不启动新任务掩盖问题。CPU模型准备慢、EdgeOne/隧道SSE缓冲、当前额度和云端实际版本均需之后真实环境核验。本轮不新增公网管理API，不自动开始真实验收。
