# 【D32 最终整合开发报告】

日期：2026-10-28；覆盖原D32、D33、D34。核心自动验收全部通过，按用户授权审核后提交推送。D29学习尚待用户明确确认；最终批次的集中教学和三项本人手测尚未开始。

## 1. 今日开发目标

完成可独立运行的Docker完整服务、真实Phoenix追踪、七项核心能力的真实演示、现有功能全量回归及最终文档。使用已有Hybrid＋Rerank、工单和确认架构，保留历史评测及原数据；D35只作为本批排错缓冲，不额外安排功能。

## 2. 新增和修改的文件

| 文件 | 职责 |
| --- | --- |
| [Dockerfile](../../Dockerfile)、[.dockerignore](../../.dockerignore)、[部署overlay](../../docker-compose.deploy.yml)、[GPU overlay](../../docker-compose.gpu.yml) | 锁依赖镜像、排除密钥/权重/运行数据、迁移/健康/网络/挂载、显式CPU与GPU |
| [tracing.py](../../src/project_copilot/infrastructure/tracing.py)、[config.py](../../src/project_copilot/config.py)、[main.py](../../src/project_copilot/main.py)、依赖和模板 | OTel SDK、请求隔离Span、单次OTLP实际ack、导出开关和生命周期 |
| [Agent图](../../src/project_copilot/agent/graph.py)、[模型适配](../../src/project_copilot/infrastructure/models.py)、RAG检索/融合/重排/回答 | 安全模型/工具/阶段Span、真实usage和错误类型；保留原预算、来源合同与算法 |
| [数据库Session](../../src/project_copilot/db/session.py)、[工单Service](../../src/project_copilot/services/tickets.py)、[提案Service](../../src/project_copilot/services/approvals.py)、响应Schema | flush与真正commit/rollback区分、同步依赖跨线程安全、实际导出布尔回执 |
| [final_acceptance.py](../../scripts/final_acceptance.py)、[final_metrics.py](../../scripts/final_metrics.py) | 有界真实演示、数据库/向量摘要、重建验证、真实Phoenix回读和分母明确的指标 |
| [核心追踪测试](../../tests/test_tracing.py)、[业务追踪测试](../../tests/test_business_tracing.py)、[指标边界测试](../../tests/test_final_metrics.py) | OTel/MockTransport、实际图和真实PG分别验证，失败分母与未知usage不造零 |
| README、[架构](../ARCHITECTURE.md)、[部署](../DEPLOYMENT.md)、[最终验收](../FINAL_ACCEPTANCE.md)、[三项手测](../FINAL_MANUAL_VERIFICATION.md)、计划/日志/学习/面试材料 | 最终工程交接与学习定位 |
| [最终审计](../evidence/D32_FINAL_ACCEPTANCE.json)及关联5份JSON | 真实业务、首轮失败、Phoenix原始Span、指标、Docker/资源证据 |

## 3. 核心实现内容

D32：独立API镜像、最小白名单构建上下文、非root运行、只读密钥/模型挂载、可写清单挂载、PG/Qdrant原卷及新Phoenix卷；一次性Alembic服务成功后才启动API。GPU覆盖层显式CUDA/float16，CPU镜像显式CPU/float32，原Hybrid＋Rerank没有回退。实际CPU/GPU镜像均构建，实际迁移、健康和完整业务均运行。

D33：HTTP根Span通过run_id关联Agent模型/工具/终态、Dense/BM25/RRF/BGE、生成、Embedding实际请求、工单与提案事务。真正db.transaction退出Session.begin后才committed；Service的flush仅为awaiting_commit。按trace_id一次导出OTLP，在实际接受且未部分拒绝后才标true；Phoenix失败保留业务响应并明确诊断，关闭追踪继续业务。Span只记录安全元数据，真实密钥/上下文令牌/请求头/原文/异常原文不导出。

D34：整合README、最终架构/部署、七项真实能力、成本/时延/失败分析、学习和面试材料。沿用已有实验和Trace，不建设重复监控平台，不用保留集再次选参。

## 4. 关键设计决策

应用源码在镜像中，真实.env与固定模型只在运行时只读挂载；UID/GID与宿主一致以写清单。镜像构建校验PROJECT_ROOT=/app，避免非editable安装找错.env或迁移路径。依赖与基础/Phoenix镜像固定版本或digest，容器内部使用服务名，宿主端口只绑定localhost。

同步FastAPI yield依赖可能跨线程进入/退出，事务Span使用不附着上下文的持续时间Span保留HTTP父级，防止跨线程token解绑错误。请求最多256个Span、16MiB有限JSON缓冲；导出单次有界HTTP，不把别的请求的ack当当前请求成功。超时后的迟到线程不混入其他请求，嵌套阶段耗时不求和冒充请求延迟。

保留原未鉴权POST /tickets和局部bearer上下文的本地演示边界，未声称完整用户/租户安全或生产SLA。实际账号账单、失败请求未返回的usage均为未知；CPU只做实际镜像/固定模型推断，完整云端HTTP业务使用GPU。

## 5. 自动测试结果

最终全量：**1386 passed、0 failed、0 skipped，105.84秒**，真实PG、Qdrant、GPU均启用，远端测试集合保留。命令：

```bash
OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 \
PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION=1 PROJECT_COPILOT_RUN_QDRANT_INTEGRATION=1 \
PROJECT_COPILOT_RUN_FINAL_QDRANT=1 PROJECT_COPILOT_RUN_FINAL_GPU=1 HF_HUB_OFFLINE=1 \
timeout --signal=INT --kill-after=10s 300s \
  .venv/bin/python -m pytest -q -p scripts.retain_test_qdrant
```

追踪核心/业务定向25项通过（SDK与MockTransport、受控图以及其中真实PG用例分别注明），单独真实GPU1项通过，最终指标5项通过；它们与全量有重叠，不相加充测试数。

| 实际验收 | 结果与证据边界 |
| --- | --- |
| Docker构建与启动 | CPU/GPU实际构建，迁移退出0、API/PG/Qdrant/Phoenix健康；镜像无.env和权重，运行应用49份源码SHA匹配 |
| CPU/GPU模型 | CPU float32真实推断；GPU固定revision、float16、同对象复用，probe peak allocated约1.145GB、释放后约8.5MB；不是全卡显存监控 |
| 存储与重建 | 四容器实际停止/重建且ID改变，同命名卷；旧工单摘要、演示工单DTO及新旧Qdrant点/payload/向量摘要保持；Phoenix旧Trace仍可查询 |
| 七项能力 | 全部按[最终验收表](../FINAL_ACCEPTANCE.md)实际执行；确认由独立测试客户端发送，模型没有确认工具 |
| Phoenix真实收存 | 第二轮17条Trace/84个Span回读，成功/409失败均有父子和run_id；首轮502另保留3条Trace/24个Span |
| 关闭/失败行为 | Phoenix停止时业务200、导出false且明确ConnectError诊断；应用追踪关闭且Phoenix停止时，health与真实工单查询仍200 |
| 冻结优化原档案 | selection/holdout verify退出0、不加载模型；155份原历史配置/归档/证据SHA保持 |

首次全量旧OpenAPI const=false断言失败后按新布尔合同修正；另一次在构建与残留工具进程并行期间GPU预热超时并停止。清理本轮挂起测试、独立GPU验证、限制OMP/MKL线程后全量通过，没有跳过GPU。CPU构建首次官方包TLS下载失败，直接连通检查后清空构建代理参数的一次重试成功，未关闭TLS校验。

真实模型采集共两轮：第一轮RAG生成连接错误502，停止后续付费场景并保留真实Trace；只读/models连通检查恢复后，仅进行一次有界第二轮。两轮合计5次对话尝试、4次有usage（已知输入4466/输出744），失败一次usage未知；4次实际Embedding请求合计179输入tokens。已知费用估算DeepSeek约US$0.000778～0.001555、Embedding约¥0.0000895，不含未知失败费用，不等于账单。模型SDK零自动重试。

排查期间另有6次只读模型列表请求尝试，用于比较连通性/HTTP环境；没有生成token，不计为上述对话调用或Agent任务。内部健康检查及独立无模型OTLP探针也不进入业务成功率分母。

第二轮两项Agent任务2/2、工具事件3/3，HTTP17个样本含1个预期409；中位35.924ms、nearest-rank p95 5299.947ms是混合单轮样本，不是生产SLA。首轮中止和温态GPU复用单列，未用第二轮掩盖冷启动失败。完整分母/阶段/费用来源见[指标](../evidence/D32_FINAL_METRICS.json)。用户本人学习与手测没有被自动验收替代。

2026-10-29用户明确委托Codex直接执行三项功能验证并提交，见[委托验证记录](../evidence/D32_DELEGATED_VERIFICATION_20261029.json)。本轮实际上传中文资料、组合知识/真实工单、提案预览与独立确认、错误指纹回滚及重复确认；API容器重建，PostgreSQL和Qdrant停止后重新启动，数据库与向量快照一致。Phoenix REST回读17条Trace/84个Span，UI入口200，未自动操作浏览器。本轮4次DeepSeek请求输入5452/输出978 tokens，2次Embedding共35输入tokens，实际账单未知；自动重试关闭。该记录不声称用户本人操作，也不与开发期自动验收样本合并。

## 6. GitHub 提交信息

起点main及远端main均[00d08dba737128a162057812dcd4c771ebbfa129](https://github.com/yan-ye-dhy/project-copilot/commit/00d08dba737128a162057812dcd4c771ebbfa129)，工作区原为干净。用户明确授权本批开发/验收/审核后由Codex提交推送；最终实际commit、远端SHA核对结果与链接在交接消息填写，不预填尚未生成的ID。

开发与自动验收提交[5d8e3eec6b1241d85ee6427f162f6daffe468821](https://github.com/yan-ye-dhy/project-copilot/commit/5d8e3eec6b1241d85ee6427f162f6daffe468821)已同步main；2026-10-29委托验证收尾提交的实际SHA与推送结果在本轮交接消息核对。

提交范围仅本批源码、部署、测试和脱敏证据/文档，不含.env、模型权重、缓存、数据库文件或私有上下文令牌。不强推、不重写历史；现有数据库卷和Qdrant数据保留。

## 7. 后续学习提示

交ChatGPT按最终源码分轮教学：

1. Dockerfile与Compose覆盖合并、CPU/GPU依赖、只读挂载、容器网络、健康和一次性迁移。
2. HTTP run_id→contextvars→真实OTel Span→单次OTLP→Phoenix回读，理解业务成功与导出成功不同。
3. Agent模型/工具节点及Unified FinalRetriever各阶段来源、计数和usage，理解嵌套耗时与失败分母。
4. Session yield跨线程、flush/awaiting_commit/commit/rollback，以及提案确认与模型权限边界。
5. 七项能力证据、真实优化中的改善/退化、未知费用和本地工程限制，面试如实说明Codex/用户分工。

## 8. 下一步计划

关键自动验收通过后结束正式开发，不新增D35功能。用户委托Codex完成本轮三项功能验证；[手测指南](../FINAL_MANUAL_VERIFICATION.md)仍可用于用户本人实践。D29与最终批次学习状态继续待用户明确确认，本轮不登记为用户本人操作。
