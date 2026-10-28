# D32整合批次：最终验收与指标

日期：2026-10-28。执行者为Codex，覆盖原D32–D34。本轮使用真实Docker API、PostgreSQL、Qdrant、Phoenix、智谱/DeepSeek及固定本地BGE模型。用户本人学习和三个手测未启动；D29学习仍待明确确认。

## 七项核心能力

| 项目 | 实际结果和判断依据 |
| --- | --- |
| 1 中文资料上传 | 自编final-chinese.md经实际HTTP上传，返回200/HYBRID_RERANK和稳定片段ID；首次入库真实Embedding，第二轮重复上传复用完成记录 |
| 2 知识问答 | 返回answered及PC-FINAL-4827，citation原文、document_id与上传一致；真实Dense/BM25/RRF/BGE阶段出现在Phoenix |
| 3 真实工单 | 创建预置工单201，详情与列表匹配；通过独立PostgreSQL只读快照核对写入数量及已有工单保持 |
| 4 组合Agent | 实际search_tickets＋search_knowledge，最终answered引用ticket与knowledge两类来源；不是只检查回答文字 |
| 5 提案与确认 | 实际Agent只生成pending/ticket_id=null；独立接口预览，错误指纹409且数据库不变；正确确认/重复确认返回同ID，新增仅1张工单 |
| 6 真实Phoenix Trace | 第二轮17条Trace/84个Span由Phoenix REST回读，父子关系、run_id与响应Trace ID一致；成功和失败链路均可定位，UI入口HTTP200 |
| 7 优化实验 | 原D17选型与保留集verify均退出0、模型未加载；155份历史归档/配置/证据原字节保持，不修改保留集重选方案 |

业务确认由Codex作为外部测试客户端按用户授权调用，不由模型工具调用，也不登记为用户本人手测。Phoenix结果通过真实REST数据读取核对；未声称已自动操作浏览器或保存GUI截图。

## Docker与持久化

CPU和GPU镜像均实际构建；CPU容器实际使用torch2.14.1+cpu、float32固定模型推断，GPU容器实际验证cuda/float16、同模型对象复用及释放。CPU没有另跑整套真实云端业务场景，完整HTTP演示使用GPU部署。

API、PostgreSQL、Qdrant、Phoenix四个容器均实际停止并重建，容器ID已改变、命名卷保持。重建后预置和确认工单与之前完整DTO一致，数据库行摘要一致；Qdrant新集合及原业务集合的点、payload和向量摘要保持；之前的Phoenix Trace仍能查询。数据卷、旧集合、模型/向量缓存均保留。

镜像根目录检查为/app，镜像内部没有真实.env和模型权重，实际运行时只读挂载；已核对49份运行应用源码与宿主最终业务源码相同。镜像标签记录构建起点，最终源码身份以文件SHA记录，不把尚未生成的Git提交写成构建时版本。

## 真实指标和样本边界

第二轮预设两项Agent任务为组合分析，以及提案后完成独立确认：**2/2完成**。三个工具事件均成功，**3/3**；本轮Agent没有工具失败样本，失败分类不能据此推广为整体系统故障率。预期错误指纹409属于独立业务接口失败，不能放进Agent工具分母。

第二轮17条人工编排的HTTP请求含14个200、2个201和1个预期409。单轮请求时延中位35.924ms、nearest-rank p95为5299.947ms；其中混合健康、数据库和模型场景，不能作为某类接口的生产SLA。真实阶段耗时在[指标JSON](evidence/D32_FINAL_METRICS.json)逐项给出，每项标样本量；父子Span嵌套不相加当总延迟。

第一轮在RAG生成时出现ChatRequestError/502，立即停止后续模型场景，保留3条Trace/24个Span。只读模型列表连接检查恢复后，只重新进行一轮有界演示；没有SDK自动重试或删除首次失败。第二轮使用第一轮已预热的GPU模型，不能将第二轮温态时延称为完整冷启动。

两轮合计有**5次对话生成/决策尝试，其中4次返回usage**；已知输入4466、输出744、合计5210 tokens，失败尝试usage未返回、实际费用未知。四次实际Embedding请求Span均返回usage，合计179输入tokens。已知DeepSeek部分按2026-10-28官方Flash价估算约US$0.000778～0.001555，Embedding约¥0.0000895；该数不含无法核实的失败请求费用，也不是实际账单。[DeepSeek官方价格](https://api-docs.deepseek.com/quick_start/pricing/)、[智谱官方价格](https://docs.bigmodel.cn/cn/guide/models/embedding/embedding-3)。

## 成功和失败Trace分析

- 成功示例`4da96f33d9c5ad3e74f0ce39b01a44fa`：agent.model提出只读工具，实际ticket.search及完整Hybrid/Rerank返回来源，后续模型生成answered；工具事件与Trace阶段一致。它不代表所有自然语言任务都能完成。
- 业务失败示例`a29652aa2671b57776f5cb4dc59b5900`：确认指纹不匹配，HTTP409、approval.confirm为ERROR，db.transaction为rolled_back；随后正确指纹可确认，失败没有留下工单。
- 首轮上游失败`156769016bf6f1f9c5d76f681d274f38`：检索完成后rag.generate出错，HTTP502；Trace真实导出成功与业务成功是两种状态，不能把X-Trace-Exported=true当作业务成功。

实际停止Phoenix时，health和工单查询仍200，X-Trace-Exported=false并出现trace_export_failed/ConnectError脱敏诊断；关闭应用追踪且Phoenix停止时，这两类业务仍200且没有Trace ID。恢复后再次导出成功。控制实验均未新增模型调用。

## 测试、故障处理及限制

完整回归命令见[D32八项报告](reports/DAY_32.md)。首次全量只有旧OpenAPI const=false断言失败，更新为实际布尔回执合同；另一轮受资源争用影响GPU预热超时并停止，独立GPU验证通过后限制OMP/MKL线程再全量通过。原失败记录保留，不冒充通过；最终全量包含真实PG、Qdrant和GPU。

CPU镜像首次PyTorch官方包TLS下载失败，直接连通检查后清空构建代理参数的一次重试成功，没有关闭TLS校验。业务首轮上游网络错误按上述失败留证并停止后续付费场景，没有修改模型或提示词掩盖错误。

本地Docker独立GPU探针的PyTorch peak allocated为1,144,796,160字节；清理后allocated为8,519,680字节。该探针不是API worker的全卡显存监控，allocator也不等于整个GPU设备占用。模型按worker各占显存，线程等待超时仍不能强制终止CUDA执行。

当前为localhost单用户工程，原普通工单创建接口和Phoenix无完整登录鉴权，D29 bearer能力不等于账户/租户授权。无生产负载、分布式锁、持续运行可靠性或统计显著性证明。D35仅作为本批排错缓冲，关键验收通过后不另增功能或开发日；用户源码学习及[三项本人手测](FINAL_MANUAL_VERIFICATION.md)等待明确开启。
