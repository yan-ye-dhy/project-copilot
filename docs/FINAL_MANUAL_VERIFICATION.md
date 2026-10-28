# 最终批次：学习后的三个本机手动验证

以下仅为准备好的学习交接，不代表用户已经操作。完成ChatGPT源码学习后，用户主动开启，Codex每次指导一个环节。真实模型请求会产生费用；失败先分析，不自动重试。使用自编材料和本地示例配置，保护上下文令牌。

## 1. Docker启动、健康与持久化

按[部署指南](DEPLOYMENT.md)准备已有`.env`、固定模型缓存和UID/GID，然后在项目根目录执行已验证的GPU部署：

```bash
export PROJECT_COPILOT_CONTAINER_UID="$(id -u)"
export PROJECT_COPILOT_CONTAINER_GID="$(id -g)"
mkdir -p data/manifests

docker compose --env-file .env.example \
  -f docker-compose.yml -f docker-compose.deploy.yml -f docker-compose.gpu.yml build api
docker compose --env-file .env.example \
  -f docker-compose.yml -f docker-compose.deploy.yml -f docker-compose.gpu.yml up -d --wait api
curl --noproxy 127.0.0.1 --include http://127.0.0.1:8002/health
```

预期迁移容器成功退出、API/PG/Qdrant/Phoenix健康，health为200，并有X-Run-ID、X-Trace-ID、X-Trace-Exported。若使用CPU，去掉gpu overlay，保持显式cpu/float32；下载站TLS故障排查见部署指南，不能自动回退算法。

完成第2项并保存真实ticket_id后，使用同一Compose配置执行`stop api postgres qdrant`，再执行`up -d --wait --force-recreate api`，重复查询该工单和Qdrant集合信息。预期ID/内容保持、向量集合及点数仍在。保存重启前后的结果；检查失败时先看`logs api migrate`、挂载与健康状态，不删除数据卷。

## 2. Agent综合检索、待确认提案与真实落库

保持服务运行，另开项目终端：

```bash
umask 077
mkdir -p data/manual-final
printf '# 蓝鹤研发排障\n环境切换后连接失败，应先检查进程的数据库URL和PostgreSQL就绪状态。参考标识 PC-MANUAL-4827。\n' > data/manual-final/knowledge.md
curl --noproxy 127.0.0.1 --fail --silent --show-error \
  http://127.0.0.1:8002/documents/upload \
  -F 'file=@data/manual-final/knowledge.md;type=text/markdown' \
  --output data/manual-final/upload.json --write-out 'HTTP %{http_code}\n'
curl --noproxy 127.0.0.1 --fail --silent --show-error \
  http://127.0.0.1:8002/tickets -H 'Content-Type: application/json' \
  --data '{"title":"蓝鹤手测已有工单","description":"环境切换后连接失败，待排查数据库配置。"}' \
  --output data/manual-final/existing-ticket.json --write-out 'HTTP %{http_code}\n'
curl --noproxy 127.0.0.1 --fail --silent --show-error \
  http://127.0.0.1:8002/agent/ask -H 'Content-Type: application/json' \
  --data '{"question":"请用search_tickets按蓝鹤查询真实工单，再用search_knowledge检索蓝鹤排障，结合两类来源说明现状与建议。"}' \
  --dump-header data/manual-final/combined-headers.txt \
  --output data/manual-final/combined.json --write-out 'HTTP %{http_code}\n'
```

预期上传200/HYBRID_RERANK、已有工单201、Agent200/answered，sources中有ticket和knowledge，events含成功的两类工具。这里会留下自编文档和一张预置工单，便于观察“类似工单”真实查询。

随后沿[D29指南](D29_MANUAL_VERIFICATION.md)第1、2项创建上下文、生成待确认提案、查看payload/指纹，再通过独立确认接口确认和重复确认。观察pending/ticket_id=null→completed/实际ticket_id，两次确认同ID，并用`GET /tickets/{ticket_id}`回读。使用该ID完成第1项重建后查询。令牌只放在忽略的本机文件；普通聊天的“确认”不会替代独立确认接口。

本项最多新增一张确认工单及一张预置工单；当前没有公开删除API，默认保留演示记录。失败首先看run_id、Agent failure_type、工具events、PG健康和提案有效期；过期后需创建新提案，不能延长或篡改旧内容。

## 3. Phoenix中核对成功或失败Trace

打开`http://127.0.0.1:6007`，选择Compose配置的项目名（默认`project-copilot`）。从第2项`combined-headers.txt`取得X-Trace-ID及X-Run-ID，进入对应Trace，观察HTTP根、agent.model、agent.tool.execute、rag.dense/bm25/rrf/rerank及ticket.search。原文/请求头/令牌不应出现在Span中。

确认接口应有approval.confirm、ticket.create以及真正committed的db.transaction；Service的awaiting_commit不代表提交已完成。要观察不产生新工单的失败，可在D29请求终端中沿用action_id/context变量，发送错误指纹：

```bash
curl --noproxy 127.0.0.1 --silent --show-error \
  -H "X-Copilot-Context-ID: $context_id" -H "X-Copilot-Context-Token: $context_token" \
  -H 'Content-Type: application/json' \
  --data '{"payload_fingerprint":"0000000000000000000000000000000000000000000000000000000000000000"}' \
  "http://127.0.0.1:8002/actions/$action_id/confirm" \
  --dump-header data/manual-final/failure-headers.txt \
  --output data/manual-final/failure.json --write-out 'HTTP %{http_code}\n'
```

预期409、脱敏错误；以该响应Trace ID在Phoenix查看ERROR与rolled_back，之前已完成的工单不会被撤销或新建。若追踪关闭或导出失败，X-Trace-Exported不能显示true；先核对开关、Phoenix健康和trace_export_failed日志。日志存在不证明Phoenix已收存，需要实际界面或API回读。
