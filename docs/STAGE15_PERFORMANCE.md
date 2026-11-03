# 完成片段一致性校验的性能测量

2026-11-03，Codex 在本机 WSL 和本轮专用 Docker HTTP Qdrant 上测量。每个规模先预热快照，再执行 4 次顺序请求及一组 4 并发请求；每次均保留完整清单回读、原文身份校验、Dense/BM25/RRF 路径。Embedding 和 Rerank 使用确定性替身，不调用供应商，也不代表真实 BGE 推理时延。

| 实际片段 | 每请求精确回读批次 | 顺序请求中位耗时 ms | 其中完成片段校验 ms | 顺序锁持有 ms | 4 并发请求中位耗时 ms | 4 并发锁等待中位 ms | Python 分配峰值（4并发，MiB） |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 1 | 24.85 | 6.99 | 24.28 | 103.93 | 76.38 | 0.66 |
| 100 | 2 | 42.22 | 20.49 | 41.55 | 117.66 | 76.48 | 1.44 |
| 500 | 8 | 120.31 | 82.61 | 117.77 | 355.65 | 234.33 | 3.98 |

完整分位数、批次数与分配峰值见 [机器记录](evidence/STAGE15_RETRIEVAL_PERFORMANCE.json)。样本很少，p95 是这 4 个样本的最大值，不能当作生产 SLO。tracemalloc 仅计 Python 分配，不覆盖 Qdrant 服务进程、全部原生内存、RSS 或 GPU 内存；没有声称测得整机内存。

结果确认校验成本随片段数增长，且现有共享索引排他锁会串行化检索，4 并发主要增加锁等待。这次没有通过省略校验或更换 Dense-only 路径优化数据；低并发邀请制预发布可据此设定初始容量，真实 BGE 冷启动、长文档、慢磁盘与更多用户仍须在 STAGING 测量。超过本轮 500 片段规模前应先复测，不能外推无限容量。

复现必须显式指向新建合成测试服务；脚本拒绝原项目 6433 端口，且仅创建随机前缀集合、结束后删除其自行创建的集合，不删除数据卷：

```bash
OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 .venv/bin/python -m scripts.retrieval_benchmark \
  --qdrant-url http://127.0.0.1:16433 --confirm-isolated \
  --output data/stage15/performance-new.json
```

输出路径必须不存在；不要对原有 Qdrant 使用该命令。测试实现及内存替身计数验证见 `scripts/retrieval_benchmark.py`、`tests/test_retrieval_benchmark.py`。
