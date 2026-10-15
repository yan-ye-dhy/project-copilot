# 面试笔记

每个任务保留少量有代码和验证依据的问题。下面是回答思路，需结合自己的操作与理解重新组织，避免把助手实施等同于个人掌握。

## D1：工程入口与配置

### 1. 为什么把初始化放在lifespan，而不是模块顶层？

导入模块会被服务启动、测试和工具检查触发。若导入即读取必填配置、连接存储或下载模型，测试入口会依赖外部环境，启动和清理也难以统一管理。当前 `create_app()`只建立应用；lifespan在提供请求前加载并校验配置，将其放到 `app.state`，以后新增资源也在相应生命周期内管理。

证据：[`main.py`](../src/project_copilot/main.py)；D1用会抛错的Settings构造替身验证“导入不实例化”，并验证非法environment在lifespan启动阶段失败。当前尚无数据库客户端，不能声称验证过连接释放。

### 2. 两个项目怎样避免读取错误配置或共享依赖？

新项目拥有独立Git顶层、Python包、`.venv`和dotenv路径，不导入原项目模块。Settings从新项目文件位置确定dotenv绝对路径，并使用 `PROJECT_COPILOT_`前缀。已验证改变终端目录不会改读另一份dotenv，进程环境变量按既定优先级覆盖文件值。两套虚拟环境可以共享解释器，但各自的site-packages不同。

证据：[`config.py`](../src/project_copilot/config.py)、`.venv/pyvenv.cfg`与 [D1验证记录](DEVELOPMENT_LOG.md#d1独立环境与最小api2026-10-10)。目前仅隔离本地入口；容器、存储与模型配置在后续任务逐步验证。

### 3. 健康接口返回200能说明什么？

本次实际HTTP200证明Uvicorn完成当前启动、FastAPI路由可达、配置可读并按契约返回JSON；环境变量覆盖也通过实际请求验证。它不能证明尚未接入的Embedding、Qdrant或PostgreSQL可用。后续需要按组件设计有意义的就绪和业务验证，区分启动、请求契约、真实存储及实际模型API。

证据：[`main.py`](../src/project_copilot/main.py) 的健康路由；默认与修改配置后的两次HTTP结果、OpenAPI业务路径检查均见开发日志。

## D2：Embedding请求契约

1. **为什么使用原文本输入，并区分batch与chunk？** 原项目智谱适配关闭框架文本长度处理；新SDK直接发送中文原文。batch64是每次请求的文本条数，D3的切分长度是单片段字符数，两者控制不同工作量。
2. **HTTP200为什么还不能认定向量有效？** 必须核对数量、index配对、模型、维度和有限数值。当前原始JSON验证还会拒绝bool和数字字符串，避免SDK类型转换掩盖协议错误。
3. **离线测试能证明什么？** 真实SDK配MockTransport证明序列化、批次、错误和资源契约；账号与实际维度另由一次真实smoke确认1条2048维向量。两类证据分别记录，3维假向量不是实测；本日尚未评估检索效果。

## D3：读取、切分与证据身份

1. **为什么递归切分需要最终字符回退？** 只有段落/换行/空格时，连续中文可能找不到分隔符；最终空字符串按字符切开，保证当前750字符上限。这只是结构验收，切分质量仍待真实评测；750字符也不等于750 tokens。
2. **为什么文件名不能作为唯一身份？** 同名文件可能内容不同，同内容改名也应可识别。当前用原始字节SHA-256派生document UUID，chunk UUID再包含切分指纹、页号、位置和文本；重复文字的位置不同，仍有不同片段ID。
3. **PDF页码如何保证引用可追溯？** 读取时保留0起始真实页索引及空白页，逐页切分不跨页合并。三页自编PDF仅页0/2产生片段，但后者仍是页2；TXT/MD用None，不虚构页0。没有OCR，图像式PDF可能明确返回无文本。

依据：[读取与切分测试](../tests/test_documents.py)、[离线查看脚本测试](../tests/test_document_cli.py)和 [D3报告](reports/DAY_3.md)。当前尚未证明检索或回答效果。

## D4：Dense索引的工程边界

1. **向量库保存了什么，如何找回来源？** 集合定义named dense/2048维/Cosine；每个Point用D3 chunk UUID作ID，向量用于相似检索，payload保存原文与metadata。问题也由同一模型嵌入，过滤当前版本和完成文档后返回top-k证据；实际样本保留PDF页0/2，不能把Cosine score当正确率。
2. **为什么写入完成后还要保存清单？** Qdrant completed只证明点写入，清单表示该文档已由应用登记完成。原子替换避免半份JSON；保存失败可能留下点，但新文档不进入成功范围。顺序单进程、稳定ID覆盖与跨存储事务/并发恢复是不同保证，后两者不能宣称D4已完成。
3. **如何证明Dense链路可用，又避免夸大效果？** 165项离线测试检查SDK请求/身份/失败契约；另1项Docker HTTP测试用假二维向量验证真实存储；2次授权模型请求才验证真实2048维索引与一个问题。这些层级分开记录，没有冻结评测集时不报告Recall/MRR；更换模型/维度/切分要新集合及新实验版本。

依据：[Qdrant适配](../src/project_copilot/infrastructure/qdrant.py)、[入库服务](../src/project_copilot/rag/ingestion.py)、[真实HTTP测试](../tests/test_qdrant_integration.py)和 [D4报告](reports/DAY_4.md)。回答思路尚待用户通过ChatGPT学习并结合自己的手测复述。

## D5：消息模板、回答契约与引用

1. **为什么区分System和User，当前属于Zero-shot还是Few-shot？** 固定System声明角色、证据和输出规则，问题/检索数据在User里一次绑定；没有具体任务输入及标准输出示例，字段骨架仍是Zero-shot。角色分离降低数据被直接提升为高权限指令的风险，但完整注入防护效果尚待D19测试。
2. **JSON mode和Pydantic有什么不同？** 前者让模型输出JSON格式；后者继续拒绝缺字段、额外字段、非法类型/状态和不合规则的引用列表。再检查编号指向本次实际发送的片段，最后还要判断事实支持；这四层不能互相替代。
3. **怎样防止模型编造来源，又保证实验可追溯？** 模型只给引用编号，后端映射原文、稳定chunk ID、文件名和真实页索引；不让模型重新生成元数据。响应另记录Prompt/Schema指纹、Dense版本、请求/实际模型和可用usage；D10再冻结完整基线配置，不把单条样例当效果评测。

依据：[消息与回答编排](../src/project_copilot/rag/answering.py)、[Schema](../src/project_copilot/schemas/rag.py)、[离线契约测试](../tests/test_answering.py)和 [D5报告](reports/DAY_5.md)。2026-10-15用户完成D5手测，并明确确认已通过ChatGPT学习、能解释主调用链和空白密钥为何在模型请求前被拒绝；以上问答继续用于复盘，不据单条样例宣称整体效果或完整注入防护已验证。

## D6：上传入口与Dense工程链路

1. **上传接口怎样复用已有RAG？** multipart和文件校验只负责请求边界；随机临时路径交给原IngestionService，原来源名作为显示元数据，仍按解析→切分→Embedding→Qdrant→成功清单顺序完成。模型/库/清单失败不会返回indexed，临时文件在finally清理。
2. **为什么既限制整个body，又限制文件字节？** multipart有额外边界与头；框架中间件统计实际ASGI字节，业务复制再限制单文件，不能只信Content-Length或把spool内存阈值当文件上限。扩展名和MIME只是初筛，内容仍须通过UTF-8/PDF解析。
3. **线程池、入库保护和run_id分别解决什么？** 线程池承接同步IO/SDK调用；非阻塞进程锁避免两个HTTP上传覆盖清单，忙时409；run_id把本地请求/响应/日志关联起来。它们不构成跨进程事务或已导出的Phoenix Trace，后续按计划逐步验证。

依据：[上传入口](../src/project_copilot/api/documents.py)、[入库编排](../src/project_copilot/rag/ingestion.py)、[请求关联](../src/project_copilot/infrastructure/tracing.py)。D6用户学习与手测尚待完成。
