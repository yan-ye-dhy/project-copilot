# D24.1：学习后的三个手动验证项目

以下命令供用户学习后自主开启；执行者和结果另行记录。需要Docker Desktop、Qdrant、项目自己的智谱/DeepSeek密钥、本地冻结BGE模型缓存与CUDA环境。所有真实问题请求最多触发对应Embedding与DeepSeek调用，查看响应中的usage；不要发送真实密钥或上传私人文档。

前置：在项目根目录启动Qdrant，并在单独终端启动API：

```bash
cd /home/yanye/project-copilot
uv sync --frozen --inexact
docker compose --env-file .env.example up -d --wait qdrant
.venv/bin/python -m uvicorn project_copilot.main:app --host 127.0.0.1 --port 8002
```

## 1. 上传后验证完整业务流水线

另开项目终端创建一份自编文档并上传一次：

```bash
mkdir -p data/manual-d24-1
printf '蓝鹤协议的唯一识别码是 TICKET-ALPHA-739。\n' > data/manual-d24-1/knowledge.txt
curl --noproxy 127.0.0.1 --fail --silent --show-error \
  http://127.0.0.1:8002/documents/upload \
  -F 'file=@data/manual-d24-1/knowledge.txt;type=text/plain' \
  --output data/manual-d24-1/upload.json --write-out 'HTTP %{http_code}\n'
cat data/manual-d24-1/upload.json
```

预期200、`mode=HYBRID_RERANK`；上传成功后对应清单与BM25快照已同步。首次相同内容使用真实Embedding，重复上传采用稳定ID并复用完成记录。如果上传失败，先查Qdrant就绪、模型密钥和服务日志中的run_id。

## 2. 比较RAG与Agent的真实来源

```bash
curl --noproxy 127.0.0.1 --fail --silent --show-error \
  http://127.0.0.1:8002/rag/ask -H 'Content-Type: application/json' \
  --data '{"question":"蓝鹤协议的唯一识别码是什么？"}' \
  --output data/manual-d24-1/rag.json --write-out 'HTTP %{http_code}\n'
curl --noproxy 127.0.0.1 --fail --silent --show-error \
  http://127.0.0.1:8002/agent/ask -H 'Content-Type: application/json' \
  --data '{"question":"请用search_knowledge查找蓝鹤协议的唯一识别码，并引用来源。"}' \
  --output data/manual-d24-1/agent.json --write-out 'HTTP %{http_code}\n'
```

预期两次HTTP200、运行状态`answered`，分别在`rag.json`的citations和`agent.json`的sources/events中找到刚上传文档原文及`search_knowledge`。首次查询会加载并预热固定GPU模型，之后同一进程复用；模型答案有随机性，来源和状态须实际核对。失败时检查Qdrant、完成清单、固定revision缓存和CUDA；检索失败应显式返回错误或Agent failed，不等同于证据不足。

## 3. 验证快照恢复和不回退

记录快照的inode，再正常重启API并重新执行第2项中的一次RAG请求：

```bash
stat -c 'snapshot inode=%i size=%s' data/manifests/pc_knowledge_dense_v0.bm25.json
```

重启前在服务终端按Ctrl+C，随后重新运行前置的uvicorn命令；重查后相同清单应加载已发布快照、仍引用原文，通常inode不变。若快照或清单被其他进程更新，应以实际指纹/文档内容为准。要观察明确失败，可在额外终端以一个新建空缓存目录启动另一个端口，再访问该端口的`/rag/ask`：

```bash
empty_cache=$(mktemp -d)
PROJECT_COPILOT_FINAL_MODEL_CACHE_DIR="$empty_cache" \
  .venv/bin/python -m uvicorn project_copilot.main:app --host 127.0.0.1 --port 8003
```

用第2项RAG命令将端口改为8003，预期503及脱敏错误，不会返回Dense替代答案；服务日志定位`FinalRetrievalError`。空缓存进程退出后，原8002服务和原模型缓存不受影响。该测试不删除上传的文档、Qdrant数据或卷。
