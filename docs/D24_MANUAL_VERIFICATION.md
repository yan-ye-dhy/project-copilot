# D24整合批次：三个高价值手动验证项目

对应原D24–D28。先由ChatGPT结合源码讲解State/条件路由、工具调用和权限边界；用户主动要求后，Codex按以下项目逐步带练。本文是操作说明，不表示用户已经执行。D24批次只提供三类只读工具，创建提案/确认属于D29批次；演示工单由用户调用普通工单API创建。

前置：在`/home/yanye/project-copilot`操作，Docker Desktop/WSL集成可用，新项目`.env`已经配置自己的`PROJECT_COPILOT_DEEPSEEK_API_KEY`和`PROJECT_COPILOT_ZHIPU_API_KEY`，不要发送或打印密钥。下例沿用本机演示数据库账号；已有自定义密码时，连接配置须与现有PG卷一致。

服务终端准备环境、迁移并启动API：

```bash
cd /home/yanye/project-copilot
uv sync --frozen --inexact
export PROJECT_COPILOT_DATABASE_URL='postgresql+psycopg://project_copilot:local-dev-only@127.0.0.1:5433/project_copilot'
docker compose --env-file .env.example up -d --wait postgres qdrant
.venv/bin/python -m scripts.ticket_db upgrade
.venv/bin/python -m uvicorn project_copilot.main:app --host 127.0.0.1 --port 8002
```

PG在5433、Qdrant在6433，API在8002。知识路径复用默认集合和成功清单，须已有`sample.pdf`可检索；仅当尚未完成该样本入库时，在另一个项目终端执行一次原D4命令：

```bash
.venv/bin/python scripts/dense_index.py ingest tests/fixtures/documents/sample.pdf --live
```

`--live`允许本次真实Embedding。已有索引可直接复用；清单/维度冲突先核对`.env`与原索引，不重建业务卷。以下每次真实`/agent/ask`默认最多4次模型决策、6次工具执行；知识查询可能额外调用Embedding，错误请求也可能已产生上游费用。没有自动重试，看到失败先排查，不循环重复收费请求。

## 1. 同一工单与知识的三条真实Agent路径

操作终端先创建一张演示工单，后续一直复用它：

```bash
cd /home/yanye/project-copilot
mkdir -p data/manual-d24
curl --noproxy 127.0.0.1 --fail --silent --show-error \
  http://127.0.0.1:8002/tickets -H 'Content-Type: application/json' \
  --data '{"title":"D24 Agent演示工单","description":"请核对工单状态，并查询sample.pdf的本地文档加载说明。"}' \
  --output data/manual-d24/ticket.json --write-out 'HTTP %{http_code}\n'
```

预期201，文件中有真实UUID和`open`。只创建一次。准备三个请求文件；下面的Python只组织本地JSON，不调用模型：

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path
base = Path('data/manual-d24')
ticket_id = json.loads((base / 'ticket.json').read_text())['id']
questions = {
    'ticket': f'请调用get_ticket核对工单{ticket_id}的标题和当前状态，依据真实工单回答。',
    'knowledge': '请调用search_knowledge检索sample.pdf第一页，说明它描述的本地文档加载功能。',
    'combined': '请先用search_tickets查询标题包含D24 Agent演示工单的记录，再用search_knowledge查询sample.pdf第一页，分别说明真实工单状态和文档描述的功能。',
}
for name, question in questions.items():
    (base / f'{name}-request.json').write_text(json.dumps({'question': question}, ensure_ascii=False), encoding='utf-8')
PY
```

定义一个复用curl命令的终端函数，再逐条请求。`--data-binary @文件`读取请求JSON，响应保存在同名文件；每条单独观察完再执行下一条：

```bash
agent_ask() {
  curl --noproxy 127.0.0.1 --fail --silent --show-error \
    http://127.0.0.1:8002/agent/ask -H 'Content-Type: application/json' \
    --data-binary "@data/manual-d24/$1-request.json" \
    --output "data/manual-d24/$1-answer.json" --write-out 'HTTP %{http_code}\n'
}
agent_ask ticket
```

打开`data/manual-d24/ticket.json`与`data/manual-d24/ticket-answer.json`对照。预期Agent的`status=answered`，events中有成功`get_ticket`，`sources`中`ticket:UUID`与创建工单一致；创建响应保留供后续复用。

接着逐条执行：

```bash
agent_ask knowledge
agent_ask combined
```

分别查看`knowledge-answer.json`、`combined-answer.json`：知识路径应有`search_knowledge`及`knowledge:`来源，原文/文件名/page来自`sources[].data`；组合路径应有两种搜索工具及工单、知识两类真实来源。模型可多步决策，工具顺序/次数不保证固定；观察`model_calls`、`tool_executions`、events里的调用ID/耗时和`model_events`中的实际usage，`usage=null`表示用量未知或不完整。HTTP200仍需检查业务`status`，来源有效只证明来自真实工具，不代替人工核对回答事实。

失败先查：503检查新项目模型配置；工具`tool_exception`检查PG迁移/连接或Qdrant/成功清单；`invalid_final`检查模型最终JSON与source ID；`budget_exhausted`查看失败类型和计数。API日志的run_id应与响应一致，`trace_exported=false`表示尚未导出Phoenix，不能当成已完成D33追踪。

## 2. 带历史的追问与重新查证

复用第一项的工单请求和真实Agent回答，构造一次显式历史追问：

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path
base = Path('data/manual-d24')
previous = json.loads((base / 'ticket-request.json').read_text())
answer = json.loads((base / 'ticket-answer.json').read_text())
request = {'question': '刚才那张工单现在是什么状态？请重新调用get_ticket核对，不要只复述历史。',
           'history': [{'role': 'user', 'content': previous['question']},
                       {'role': 'assistant', 'content': answer['answer']}]}
(base / 'follow-up-request.json').write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
PY
agent_ask follow-up
```

预期`follow-up-answer.json`有真实工单来源、成功工具事件及本次新run_id，状态与原工单一致。历史只有user/assistant文本，本轮仍需重新查证；这是客户端显式传入历史，不是服务端持久会话。失败先检查请求里的history角色、总长度及前一步回答是否成功，不向history塞入System、ToolMessage或自报权限。

## 3. 观察预算终止与工具结果配对

```bash
.venv/bin/python -m scripts.agent_budget_demo --local
```

这是实际编译LangGraph配合受控循环模型的离线演示，不是DeepSeek真实效果测试；不调用外部模型、PG或Qdrant。预期`status=budget_exhausted`、`failure_type=model_budget`、2次模型决策、两个`unknown_tool`事件、`tool_executions=0`。

对照`scripts/agent_budget_demo.py`与`agent/graph.py`：受控模型反复提出未注册的`execute_sql`，后端按call ID回传错误ToolMessage，再次决策后触发预算终止。观察events中两个调用ID和失败分类；工具名是模型提议，只有后端白名单与Schema验证通过才会执行。若结果不同，先看本地代码/预算及完整JSON，无需改用收费循环验证。

三项完成后可Ctrl+C关闭API，工单可以保留用于后续演示。本轮只写入第一项由用户显式创建的演示工单；若要清理，限定该ID与标题：

```bash
ticket_id=$(.venv/bin/python -c 'import json; print(json.load(open("data/manual-d24/ticket.json"))["id"])')
docker compose --env-file .env.example exec -T postgres \
  psql -U project_copilot -d project_copilot -v ticket_id="$ticket_id" <<'SQL'
DELETE FROM tickets WHERE id = :'ticket_id'::uuid AND title = 'D24 Agent演示工单' RETURNING id;
SQL
```

清理为可选步骤，不删除表/数据库/卷。用户反馈三项结果后再确认学习与验收状态，不自动开始下一批教学。
