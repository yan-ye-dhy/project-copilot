# D29：学习后的三项手动验证

仅在完成ChatGPT学习并主动开始手测后执行。以下使用本项目和独立本机PostgreSQL，可能产生一张真实工单；不要把上下文令牌发到聊天。预览/确认与普通工单API均没有删除接口，需清理时先确认ID，再按下方限定条件操作；不要删除数据库或卷。

## 前置条件

启动Docker Desktop及本项目PostgreSQL；设置本项目`.env`中的数据库和DeepSeek配置，按需启动Qdrant并准备D24.1本地GPU模型。先执行显式迁移，再在独立终端启动API：

```bash
docker compose --env-file .env.example up -d --wait postgres qdrant
.venv/bin/python -m scripts.ticket_db upgrade
.venv/bin/python -m uvicorn project_copilot.main:app --host 127.0.0.1 --port 8002
```

以下命令在另一个项目终端执行。`umask 077`使后续文件默认只对本人可读；凭据放在已忽略的`data/manual-d29`，不要提交。实际Agent调用会产生模型费用，不自动重试。

## 1. Agent生成提案并预览

```bash
umask 077
mkdir -p data/manual-d29
curl --noproxy 127.0.0.1 --fail --silent --show-error \
  -X POST http://127.0.0.1:8002/agent/contexts > data/manual-d29/context.json
context_id=$(.venv/bin/python -c 'import json;print(json.load(open("data/manual-d29/context.json"))["context_id"])')
context_token=$(.venv/bin/python -c 'import json;print(json.load(open("data/manual-d29/context.json"))["context_token"])')
curl --noproxy 127.0.0.1 --fail --silent --show-error \
  -H "X-Copilot-Context-ID: $context_id" -H "X-Copilot-Context-Token: $context_token" \
  -H 'Content-Type: application/json' \
  --data '{"question":"请使用 propose_ticket 生成待确认工单：标题 D29 手测，描述 验证明确确认后才创建工单。"}' \
  http://127.0.0.1:8002/agent/ask > data/manual-d29/agent.json
.venv/bin/python - <<'PY'
import json
from pathlib import Path
r=json.loads(Path('data/manual-d29/agent.json').read_text())
print('运行状态:',r['status'], '工具:',[(e['name'],e['status']) for e in r['events']])
p=next(s['data'] for s in r['sources'] if s['kind']=='proposal')
Path('data/manual-d29/proposal.json').write_text(json.dumps(p,ensure_ascii=False,indent=2))
print(json.dumps(p,ensure_ascii=False,indent=2))
PY
```

预期`pending_confirmation`，提案状态`pending`、`ticket_id`为null；返回标题/描述、action_id、指纹和有效期。若模型没有调用工具，先看`events`及`failure_type`，不把普通回答当提案。若模型未生成提案，本次未创建工单，可在学习后再自行决定是否重试模型请求。

## 2. 明确确认与重复确认

```bash
action_id=$(.venv/bin/python -c 'import json;print(json.load(open("data/manual-d29/proposal.json"))["action_id"])')
fingerprint=$(.venv/bin/python -c 'import json;print(json.load(open("data/manual-d29/proposal.json"))["payload_fingerprint"])')
for n in 1 2; do
  curl --noproxy 127.0.0.1 --fail --silent --show-error \
    -H "X-Copilot-Context-ID: $context_id" -H "X-Copilot-Context-Token: $context_token" \
    -H 'Content-Type: application/json' --data "{\"payload_fingerprint\":\"$fingerprint\"}" \
    "http://127.0.0.1:8002/actions/$action_id/confirm" > "data/manual-d29/confirm-$n.json"
done
.venv/bin/python - <<'PY'
import json
from pathlib import Path
a,b=(json.loads(Path(f'data/manual-d29/confirm-{i}.json').read_text()) for i in (1,2))
print('两次状态:',a['status'],b['status'],'同一真实工单ID:',a['ticket_id'],a['ticket_id']==b['ticket_id'])
PY
```

预期均为`completed`，工单ID一致。过期返回`expired`、拒绝返回`cancelled`，都不表示创建成功；上下文或指纹错误分别返回403/409。错误时检查API的`X-Run-ID`及脱敏日志和PostgreSQL健康状态。

## 3. 重启后查询工单

停止API并按前置条件重新启动，保留同一PostgreSQL卷；然后运行：

```bash
ticket_id=$(.venv/bin/python -c 'import json;print(json.load(open("data/manual-d29/confirm-1.json"))["ticket_id"])')
curl --noproxy 127.0.0.1 --fail --silent --show-error \
  "http://127.0.0.1:8002/tickets/$ticket_id"
```

预期工单ID及内容保持。只有在明确不再需要本次测试数据时，才考虑在本项目数据库中按该ID删除；由于完成提案引用工单，直接删工单会被外键阻止。当前没有公开的删除API，建议保留记录，不要删除整库、卷或其他数据。
