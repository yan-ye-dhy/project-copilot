# D20整合批次：三个高价值手动验证项目

覆盖原D20–D23。以下是学习后的自助操作说明，不表示用户已执行；等用户主动要求后再逐步带练。只做这三项，不重跑完整pytest或低价值边界清单。

前置：在`/home/yanye/project-copilot`操作，Docker Desktop及当前WSL集成已开启。以下使用模板中的**本机演示账号**；如果已有自定义PostgreSQL口令，连接串需与现有卷一致，不通过删除卷解决认证失败。若将连接串存入新项目.env，只补对应字段，不覆盖已有供应商密钥。

## 1. 创建与查询闭环

服务终端执行（`--wait`等待PG健康，迁移可重复执行，不会清空表）：

```bash
cd /home/yanye/project-copilot
uv sync --frozen --inexact
export PROJECT_COPILOT_DATABASE_URL='postgresql+psycopg://project_copilot:local-dev-only@127.0.0.1:5433/project_copilot'
docker compose --env-file .env.example up -d --wait postgres
.venv/bin/python -m scripts.ticket_db upgrade
.venv/bin/python -m uvicorn project_copilot.main:app --host 127.0.0.1 --port 8002
```

新开操作终端，设置同一连接配置。只执行一次POST，后两项复用这个工单ID：

```bash
cd /home/yanye/project-copilot
export PROJECT_COPILOT_DATABASE_URL='postgresql+psycopg://project_copilot:local-dev-only@127.0.0.1:5433/project_copilot'
mkdir -p data/manual-d20
curl --noproxy 127.0.0.1 --fail --silent --show-error \
  http://127.0.0.1:8002/tickets -H 'Content-Type: application/json' \
  --data '{"title":"D20 手动验证","description":"观察请求、事务、真实数据库与返回结果。"}' \
  --output data/manual-d20/ticket.json --write-out 'HTTP %{http_code}\n'
ticket_id=$(.venv/bin/python -c 'import json; print(json.load(open("data/manual-d20/ticket.json"))["id"])')
curl --noproxy 127.0.0.1 --fail --silent --show-error "http://127.0.0.1:8002/tickets/$ticket_id"
docker compose --env-file .env.example exec -T postgres \
  psql -U project_copilot -d project_copilot -v ticket_id="$ticket_id" <<'SQL'
SELECT id, title, status, created_at, updated_at FROM tickets WHERE id = :'ticket_id'::uuid;
SQL
```

预期：POST为201；详情为200；psql返回一行，相同UUID、`open`及时间戳。UUID和时间由PG生成；API成功响应前事务已提交。`curl --output`保存响应，`psql -v`传入变量，SQL里引用变量而非拼接输入。

失败先查：503检查连接配置、PG健康、是否执行迁移；422检查标题/描述；连接拒绝检查8002的Uvicorn与5433的PG。健康接口只说明API进程可用，不替代数据库业务检查。

## 2. 受控事务失败与回滚

操作终端执行：

```bash
.venv/bin/python -m scripts.ticket_db rollback-check
```

预期JSON含`status=ok`、真实`probe_ticket_id`、`flushed_before_failure=true`、`visible_after_rollback=false`、`pool_checked_out_after=0`。命令调用真实TicketService，先INSERT/flush取得ID，再主动抛出内部受控异常，离开事务后用新Session按该ID查证不存在；不会提交这张探针工单，也不删除第一项的成功工单。可重复执行，无需清理残留测试记录。

这不是生产API的故障开关。失败先看数据库/迁移状态，再看`services/ticket_checks.py`的受控抛错和`db/session.py`事务上下文，不用“收到一个错误码”推断回滚成功。

## 3. 重启API后的持久化

在服务终端按`Ctrl+C`正常停止API，保留PG容器和卷，再执行同一启动命令：

```bash
.venv/bin/python -m uvicorn project_copilot.main:app --host 127.0.0.1 --port 8002
```

操作终端从保存文件恢复同一ID并查询，不重复创建：

```bash
ticket_id=$(.venv/bin/python -c 'import json; print(json.load(open("data/manual-d20/ticket.json"))["id"])')
curl --noproxy 127.0.0.1 --fail --silent --show-error "http://127.0.0.1:8002/tickets/$ticket_id"
```

预期仍为200，ID、标题、`created_at`/`updated_at`与第一项相同。验证的是PG记录跨API进程生命周期存在，不是内存列表。404先查两次是否连接同一个库、第一项是否201，勿通过重建数据库“修复”。

可以保留该工单用于后续演示。若决定清理，只删除此ID且标题也匹配本次演示的数据，预期`DELETE 1`或已清理时`DELETE 0`：

```bash
docker compose --env-file .env.example exec -T postgres \
  psql -U project_copilot -d project_copilot -v ticket_id="$ticket_id" <<'SQL'
DELETE FROM tickets WHERE id = :'ticket_id'::uuid AND title = 'D20 手动验证' RETURNING id;
SQL
```

不执行删库、降级迁移或卷清理；以上清理语句也不由Codex自动代执行。
