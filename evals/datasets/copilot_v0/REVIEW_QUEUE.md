# D7～D8逐题复核队列

共40题：D7的pc-d7-001～020已由用户yanye于2026-10-17明确批量确认；D8新增pc-d8-021～040由Codex起草，用户于2026-10-18明确回复“021–040已核对，全部通过”，现40题全部confirmed。此文件用于学习和复核，不属于检索语料；正式字段以JSONL为准。

每题核对问题含义、来源版本、证据完整性与答案支持；同题多个必需证据单元均须支持。无答案题检查sources.jsonl中全部5份语料，追问检查固定历史和独立问题。gold按原文版本和位置保存，不绑定当前chunk_id。

用户实际核对后可按明确题号范围批量确认；仅存疑或需修改的题补充说明，无需逐题重复书面依据。Codex不得把结构检查通过、AI起草或D7的确认延伸为D8已通过。D7批量确认登记时间保持2026-10-17T22:14:55+08:00；D8复核者为yanye，批量确认登记时间为2026-10-18T21:41:27+08:00，不编造各题独立复核时间或书面理由。

以下问题族供D8划分使用：同义题和同一追问链必须同侧。新增题复用已有相近问题族；不能为凑30/10强拆问题族。正式划分与冻结状态以对应manifest为准，复核队列本身不代表已经冻结。

## pc-d7-001 · knowledge

问题族：configuration-lifecycle；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** Project Copilot 启动时，配置在什么阶段读取并保存到哪里？关闭时执行什么？

**已确认参考答案：** 在lifespan启动阶段由load_settings读取，保存到application.state.settings；关闭时执行lifespan的finally并记录停止日志。

证据 pc-d7-001-e1：[启动与配置排查笔记 / 启动命令与执行顺序](corpus/startup_and_config.md#启动命令与执行顺序)；来源类型project_experience，字符区间[345, 485)，page=null。

> 进入lifespan启动阶段后，load_settings读取本项目配置，结果保存到application.state.settings。启动成功才开始处理请求；关闭时执行lifespan的finally并记录停止日志。用户D1真实操作已观察到启动完成及Ctrl+C后的正常关闭。

## pc-d7-002 · exact_match

问题族：environment-validation；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 把PROJECT_COPILOT_ENVIRONMENT设为staging为什么不能启动？合法值有哪些？

**已确认参考答案：** environment只接受development、test、production；staging不满足Settings字段约束，会使应用启动失败。

证据 pc-d7-002-e1：[启动与配置排查笔记 / 配置字段与启动失败](corpus/startup_and_config.md#配置字段与启动失败)；来源类型project_experience，字符区间[501, 643)，page=null。

> Settings使用PROJECT_COPILOT_前缀，默认服务名是Project Copilot，默认environment是development。app_name去除首尾空白后至少有一个字符；environment只接受development、test、production。

证据 pc-d7-002-e2：[启动与配置排查笔记 / 配置字段与启动失败](corpus/startup_and_config.md#配置字段与启动失败)；来源类型project_experience，字符区间[645, 784)，page=null。

> D1用户临时覆盖APP_NAME为Copilot Learning后，健康响应的service随之变化。将APP_NAME设为全空格，或将ENVIRONMENT设为staging，均触发Settings校验错误并使应用启动失败。命令前的临时变量只影响该次启动，不会改写配置文件。

## pc-d7-003 · paraphrase

问题族：model-key-lifecycle；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 两家模型凭据还没配好，能先把后端启动起来吗？空白凭据会在哪一步被拒绝？

**已确认参考答案：** 密钥字段可缺省，所以后端可以先启动；创建Embedding或Chat客户端时，工厂检查去除空白后的密钥，在发模型请求前拒绝空值。

证据 pc-d7-003-e1：[启动与配置排查笔记 / 密钥检查发生在哪里](corpus/startup_and_config.md#密钥检查发生在哪里)；来源类型project_experience，字符区间[943, 1056)，page=null。

> 两家模型密钥字段允许缺省，应用可以在没有模型密钥时启动。真正创建Embedding或Chat客户端时，工厂才取出密钥并去除空白；空值会在发出模型请求前拒绝。这和app_name、environment在启动阶段的字段校验不同。

## pc-d7-004 · exact_match

问题族：model-key-lifecycle；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** embedding_smoke.py没有--live时退出码是什么？加--live能绕过空白智谱密钥检查吗？

**已确认参考答案：** 未带--live时退出码为2；--live只是显式实调开关，不能绕过密钥检查，空白智谱密钥仍返回配置错误、退出码1。

证据 pc-d7-004-e1：[启动与配置排查笔记 / 密钥检查发生在哪里](corpus/startup_and_config.md#密钥检查发生在哪里)；来源类型project_experience，字符区间[1058, 1171)，page=null。

> D2用户运行embedding_smoke.py时，不带--live得到退出码2；显式--live后返回一条2048维向量摘要；临时空白智谱密钥返回安全配置错误、退出码1。--live是命令行的显式实调开关，并不绕过密钥校验。

## pc-d7-005 · cross_passage

问题族：curl-upload-form；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 要正确上传一个本地文档，应请求哪个方法和路径、使用哪个文件字段，并选择哪些后缀？

**已确认参考答案：** 使用POST /documents/upload，只提供一个名为file的文件字段，按-F的file=@路径提交；实现支持.txt、.md、.pdf，并匹配基本MIME，内容仍须解析校验。

证据 pc-d7-005-e1：[单文件上传排查笔记 / curl文件表单](corpus/upload_troubleshooting.md#curl文件表单)；来源类型project_experience，字符区间[183, 361)，page=null。

> 上传入口是POST /documents/upload，只接受一个名为file的文件字段。正确的表单参数是 `-F 'file=@路径;type=application/pdf'`。大写-F提交multipart表单并自动选择POST；小写-f表示HTTP错误检查，不会创建文件表单。@必须紧接等号，表示读取本地文件；file=/@路径会变成普通字段内容。

证据 pc-d7-005-e2：[单文件上传排查笔记 / 类型、空文件和额度](corpus/upload_troubleshooting.md#类型空文件和额度)；来源类型project_experience，字符区间[542, 684)，page=null。

> 实现支持.txt、.md、.pdf，并对后缀及基本MIME做匹配检查；实际内容还要经过UTF-8或PDF解析，不能只凭Content-Type判断文件有效。用户把PDF的上传名称改成sample.exe得到415；0字节TXT得到422“上传文件不能为空”。这些拒绝发生在模型请求前。

## pc-d7-006 · exact_match

问题族：curl-upload-form；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** curl里-F与-f分别做什么？为什么上传时误用-f可能得到405？

**已确认参考答案：** 大写-F提交multipart表单并自动选择POST；小写-f是HTTP错误检查，不创建文件表单，用户误用后实际GET，上传路由只接受POST而返回405。

证据 pc-d7-006-e1：[单文件上传排查笔记 / curl文件表单](corpus/upload_troubleshooting.md#curl文件表单)；来源类型project_experience，字符区间[183, 361)，page=null。

> 上传入口是POST /documents/upload，只接受一个名为file的文件字段。正确的表单参数是 `-F 'file=@路径;type=application/pdf'`。大写-F提交multipart表单并自动选择POST；小写-f表示HTTP错误检查，不会创建文件表单。@必须紧接等号，表示读取本地文件；file=/@路径会变成普通字段内容。

证据 pc-d7-006-e2：[单文件上传排查笔记 / curl文件表单](corpus/upload_troubleshooting.md#curl文件表单)；来源类型project_experience，字符区间[363, 526)，page=null。

> D6用户误写小写-f后实际发送GET，得到405及Allow: POST；在引号内给字段名加入换行后，又因缺少有效file上传字段得到422。修正为file=@路径后进入正常的文件校验。Shell续行反斜杠消除换行，不会自动补空格；--show-error后缺少分隔空格会和下一行URL拼成未知选项，该错误发生在发送请求之前。

## pc-d7-007 · paraphrase

问题族：empty-upload；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 我传了一个完全没内容的TXT，服务会拿它去计算向量吗？会返回什么？

**已确认参考答案：** 0字节TXT返回422“上传文件不能为空”，在模型请求前拒绝，不会对它计算向量。

证据 pc-d7-007-e1：[单文件上传排查笔记 / 类型、空文件和额度](corpus/upload_troubleshooting.md#类型空文件和额度)；来源类型project_experience，字符区间[542, 684)，page=null。

> 实现支持.txt、.md、.pdf，并对后缀及基本MIME做匹配检查；实际内容还要经过UTF-8或PDF解析，不能只凭Content-Type判断文件有效。用户把PDF的上传名称改成sample.exe得到415；0字节TXT得到422“上传文件不能为空”。这些拒绝发生在模型请求前。

## pc-d7-008 · terminology

问题族：upload-budget；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 文件额度和整个HTTP请求体额度有什么区别？D6默认各是多少字节？

**已确认参考答案：** 文件额度是5242880字节（5MiB）；整个body默认5308416字节，比文件多65536字节用于multipart封装；二者检查对象不同。

证据 pc-d7-008-e1：[单文件上传排查笔记 / 类型、空文件和额度](corpus/upload_troubleshooting.md#类型空文件和额度)；来源类型project_experience，字符区间[686, 848)，page=null。

> 默认文件额度是5242880字节，即5MiB。D6用户上传5242881字节的TXT得到413“上传文件超过允许的大小”。框架另外限制整个HTTP请求体，默认额度为文件上限加65536字节，即5308416字节，给multipart封装留空间。这两个检查对象不同；该次用户实测证明了文件额度分支，没有单独手测整个请求体上限。

## pc-d7-009 · chinese_long

问题族：temporary-upload-copy；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 上传因大小限制失败后，我准备的测试文件还在磁盘，但后端/tmp副本消失了。这是清理不完整吗？说明两份文件的用途和清理责任。

**已确认参考答案：** 不是：data/manual-d6保存用户输入样本，服务端另外复制到随机前缀临时文件，原名只作来源元数据，finally删除临时副本；用户输入样本不因此删除。

证据 pc-d7-009-e1：[单文件上传排查笔记 / 临时文件与请求关联](corpus/upload_troubleshooting.md#临时文件与请求关联)；来源类型project_experience，字符区间[928, 1077)，page=null。

> 服务端将内容复制到随机的project-copilot-upload-前缀临时文件，原文件名仅作来源元数据。finally负责删除临时副本；用户保存在data/manual-d6的输入样本不会因此删除。D6用户在失败后检查/tmp为0；完成真实上传、问答并停止后端后再次检查，残留临时副本数也为0。

## pc-d7-010 · terminology

问题族：local-run-id；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** X-Run-ID、上传正文run_id和trace_exported=false分别说明什么？能据此认定Phoenix已导出吗？

**已确认参考答案：** X-Run-ID关联响应与完成日志，上传成功正文也返回同值；trace_exported=false明确表示没有导出Phoenix Trace，不能将本地关联ID当作导出证据。

证据 pc-d7-010-e1：[单文件上传排查笔记 / 临时文件与请求关联](corpus/upload_troubleshooting.md#临时文件与请求关联)；来源类型project_experience，字符区间[1079, 1198)，page=null。

> 响应头X-Run-ID与完成日志关联；上传成功正文也有同一个run_id。trace_exported=false表示没有导出Phoenix Trace。并发忙碌409、整体body超限及上游异常由自动测试覆盖，本轮用户没有手测这些分支。

## pc-d7-011 · knowledge

问题族：pdf-page-identity；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** sample.pdf有3页却只得到2个片段，片段page应该是多少？空白页会改变后面的页号吗？

**已确认参考答案：** 页索引1为空白，切分跳过空文本且不跨页合并，得到page0和page2两个片段；loader保留原页索引，不把页2改编号为页1。

证据 pc-d7-011-e1：[Dense身份与引用排查笔记 / 样本页码与原文](corpus/dense_identity_and_citations.md#样本页码与原文)；来源类型project_experience，字符区间[166, 296)，page=null。

> 自编sample.pdf共3页，原始页索引为0、1、2；页1为空白。loader保留全部页及原索引，splitter跳过空白文本且不跨页合并，所以默认只产生页0、页2的两个片段。空白页不会使原页2重新编号为页1；TXT和Markdown的page则为null。

## pc-d7-012 · paraphrase

问题族：stable-content-identity；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 把同一文件改名再挪到另一个目录，内容没变，还算同一份文档吗？显示名称有什么作用？

**已确认参考答案：** 文件字节不变时SHA-256与document_id不变；source_name仅展示来源，不参与文档身份。

证据 pc-d7-012-e1：[Dense身份与引用排查笔记 / 文档与片段身份](corpus/dense_identity_and_citations.md#文档与片段身份)；来源类型project_experience，字符区间[455, 552)，page=null。

> loader对一次读取的文件字节计算SHA-256，再以内容哈希生成稳定document_id。仅改文件名或物理路径，字节不变时文档身份不变。source_name负责展示来源，不参与文档身份。

## pc-d7-013 · cross_passage

问题族：stable-content-identity；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 相同sample.pdf重传后为什么仍只有2个点？入库先后顺序和成功清单如何解释这个结果，它等于跨存储事务保证吗？

**已确认参考答案：** 先加载切分并检查清单/集合，再Embedding、Qdrant upsert，最后写成功清单；重传复用稳定片段ID所以仍2点而非4点。清单原子替换不等于Qdrant与本地清单有跨存储事务，本例也不证明完整故障恢复。

证据 pc-d7-013-e1：[Dense身份与引用排查笔记 / 成功清单与重复上传](corpus/dense_identity_and_citations.md#成功清单与重复上传)；来源类型project_experience，字符区间[700, 827)，page=null。

> 入库依次完成加载、切分、检查清单与集合契约、Embedding、Qdrant upsert，最后写入本地成功清单。清单保存同一集合的模型、维度、切分等配置；不一致时拒绝混用。清单文件用临时写入加原子替换，不能据此声称Qdrant与本地文件具备跨存储事务。

证据 pc-d7-013-e2：[Dense身份与引用排查笔记 / 成功清单与重复上传](corpus/dense_identity_and_citations.md#成功清单与重复上传)；来源类型project_experience，字符区间[829, 946)，page=null。

> D6重复上传相同sample.pdf后，响应为indexed、2个片段、1167字节，清单仍是一份文档，Qdrant精确点数仍为2。upsert复用稳定片段ID，没有累加成4点。这个结果只说明该样本的身份复用，不等于完整故障恢复验证。

## pc-d7-014 · chinese_long

问题族：evidence-support；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 回答已经是合法JSON、通过Pydantic且引用编号有效，能直接当成事实正确吗？项目记录要求继续核对什么，能否据单个样本报告完整准确率？

**已确认参考答案：** 不能；这些只提供格式和身份约束，还需人工逐条核对问题、答案与原文证据是否支持事实。单个已知事实不能产生完整RAG准确率、Recall或MRR。

证据 pc-d7-014-e1：[Dense身份与引用排查笔记 / 引用契约与事实支持](corpus/dense_identity_and_citations.md#引用契约与事实支持)；来源类型project_experience，字符区间[1117, 1220)，page=null。

> JSON语法正确、Pydantic校验通过和引用编号有效，均不能自动证明答案得到原文事实支持；仍需人工逐条核对问题、答案与证据。本轮只验证了一个已知事实，不能据此报告完整RAG准确率、Recall或MRR。

## pc-d7-015 · knowledge

问题族：compose-variable-layers；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** Compose命令行--env-file需要在YAML中先声明吗？它与服务级env_file、environment的作用有什么区别？

**已确认参考答案：** 命令行--env-file不需YAML同名声明，用来给Compose解析变量占位符；服务environment或env_file才配置传给容器的变量，命令行选项不会自动注入全部变量。

证据 pc-d7-015-e1：[Compose环境与服务边界笔记 / 命令行变量文件](corpus/compose_environment.md#命令行变量文件)；来源类型official_documentation_note，字符区间[223, 368)，page=null。

> docker compose --env-file .env.example up -d qdrant中的--env-file是Compose命令行选项，不要求YAML存在同名字段。它指定Compose读取的变量文件，供解析${变量名}之类占位符使用；不会仅凭这个选项就把所有变量注入容器。

证据 pc-d7-015-e2：[Compose环境与服务边界笔记 / 容器环境变量](corpus/compose_environment.md#容器环境变量)；来源类型official_documentation_note，字符区间[478, 603)，page=null。

> 服务的environment可以直接声明容器环境变量，也可引用Compose先解析出的值；服务的env_file让Compose从指定文件取得要传给该容器的变量。命令行--env-file与服务env_file属于不同配置层次，不能因名称相似而等同。

## pc-d7-016 · knowledge

问题族：named-vector-spaces；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 一个Qdrant collection里所有向量都只能有同一种维度吗？命名向量如何改变这个判断？

**已确认参考答案：** 不是；同一向量空间须遵循其维度和距离配置，但一个point可有多个命名向量空间，各自使用自己的维度和度量。

证据 pc-d7-016-e1：[Qdrant向量契约笔记 / Collection与命名向量](corpus/qdrant_vector_contract.md#collection与命名向量)；来源类型official_documentation_note，字符区间[207, 346)，page=null。

> collection组织可检索的points，一个point可以带向量及payload。同一向量空间的维度和距离度量须遵循集合配置；命名向量允许在一个point上声明多个向量空间，各自有自己的维度和度量。不能把“一个collection里只能有一种维度”套用于所有命名向量场景。

## pc-d7-017 · unanswerable

问题族：missing-benchmark-results；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 本项目启用Rerank后在冻结保留集上的Recall@5提高了多少个百分点？

**已确认无答案理由：** 本批语料没有Rerank实验或冻结保留集逐题/汇总结果，不能从能力描述推导提升数值；须人工检查全体语料后确认缺失。

检查范围：sources.jsonl列出的完整5份语料；gold证据为空。

## pc-d7-018 · unanswerable

问题族：missing-account-bill；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 2026-10-16 D6用户手测固定问题的那次DeepSeek请求，在我的账号上实际扣了多少人民币？

**已确认无答案理由：** 本批语料未提供该次账号账单、实际扣款或人民币结算信息；不能以调用成功、Token数或标价替代实付，须人工检查全体语料。

检查范围：sources.jsonl列出的完整5份语料；gold证据为空。

## pc-d7-019 · follow_up

问题族：upload-budget；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 那整个请求体也只允许这么大吗？

固定历史：

- user：Project Copilot D6默认允许上传多大的文件？
- assistant：默认文件额度是5MiB，即5242880字节。

**已确认独立问题：** Project Copilot D6默认文件额度5MiB时，整个HTTP请求体的默认字节额度是多少，额外空间有什么作用？

**已确认参考答案：** 不是；文件默认5MiB，整个HTTP请求体默认5308416字节，多出的65536字节用于multipart封装。

证据 pc-d7-019-e1：[单文件上传排查笔记 / 类型、空文件和额度](corpus/upload_troubleshooting.md#类型空文件和额度)；来源类型project_experience，字符区间[686, 848)，page=null。

> 默认文件额度是5242880字节，即5MiB。D6用户上传5242881字节的TXT得到413“上传文件超过允许的大小”。框架另外限制整个HTTP请求体，默认额度为文件上限加65536字节，即5308416字节，给multipart封装留空间。这两个检查对象不同；该次用户实测证明了文件额度分支，没有单独手测整个请求体上限。

## pc-d7-020 · follow_up

问题族：evidence-support；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 那能直接当成90%的正确率吗？

固定历史：

- user：Project Copilot的Qdrant检索结果出现Cosine score为0.9，这表示什么？
- assistant：这里的score表示文本的Cosine语义相关度。

**已确认独立问题：** Project Copilot Qdrant的Cosine score为0.9，能解释成答案有90%概率正确吗，可靠性还应如何核对？

**已确认参考答案：** 不能；Cosine score是文本语义相关度，不能把0.9解释为答案有90%概率正确，仍要核对检索证据、回答内容及拒答表现。

证据 pc-d7-020-e1：[Qdrant向量契约笔记 / 检索分数的含义](corpus/qdrant_vector_contract.md#检索分数的含义)；来源类型official_documentation_note，字符区间[1103, 1215)，page=null。

> 本项目的score表示Cosine相关度，不是答案正确概率或人工标注的置信度。分数高也可能只说明文本语义接近，不能据此跳过证据支持判断，更不能把0.9解释为答案有90%概率正确。可靠性要分别核对检索证据、回答内容及拒答表现。

## pc-d8-021 · knowledge

问题族：health-probe-boundary；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** GET /health返回哪些字段，从哪里读取？为什么HTTP200不足以证明模型API与向量库可用？

**已确认参考答案：** 返回status、service、environment，从app.state.settings读取服务名和环境；接口没有探测模型API或向量库，因此HTTP200不代表这些外部依赖可用。

证据 pc-d8-021-e1：[启动与配置排查笔记 / 健康检查的边界](corpus/startup_and_config.md#健康检查的边界)；来源类型project_experience，字符区间[798, 927)，page=null。

> GET /health从app.state.settings读取服务名和环境，返回status、service、environment。D1默认响应为HTTP200、status为ok。这个接口没有探测模型API或向量库，因此健康接口成功不证明外部依赖可用。

## pc-d8-022 · knowledge

问题族：stable-content-identity；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** chunk_id由哪些因素共同生成？相同文字位于不同位置时会合并为同一个片段吗？

**已确认参考答案：** 由文档身份、切分版本、原页索引、片段位置和片段文本哈希共同生成；切分参数或相关实现版本变化会影响指纹和片段身份，同一文字在不同位置不会合并成同一个片段。

证据 pc-d8-022-e1：[Dense身份与引用排查笔记 / 文档与片段身份](corpus/dense_identity_and_citations.md#文档与片段身份)；来源类型project_experience，字符区间[554, 684)，page=null。

> chunk_id由文档身份、切分版本、原页索引、片段位置和片段文本哈希共同生成。切分参数或相关实现版本变化，会改变切分指纹并影响片段身份；相同原文出现在不同位置也不会合并为一个片段。D3用户验证了改名后身份稳定，以及将切分参数改为15/3后版本与片段ID变化。

## pc-d8-023 · knowledge

问题族：embedding-local-dimension-check；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** PROJECT_COPILOT_EMBEDDING_EXPECTED_DIMENSIONS是模型降维参数吗？客户端与存储层分别如何约束响应向量？

**已确认参考答案：** 它仅用于本地响应与索引校验，不作为模型API的dimensions参数；客户端检查每条向量长度和有限数值，存储层也校验向量，改成本地预期3不会让2048维模型输出变成3维。

证据 pc-d8-023-e1：[Qdrant向量契约笔记 / 本地维度预期不是模型参数](corpus/qdrant_vector_contract.md#本地维度预期不是模型参数)；来源类型official_documentation_note，字符区间[742, 892)，page=null。

> PROJECT_COPILOT_EMBEDDING_EXPECTED_DIMENSIONS只用于本地响应与索引校验，不是发给模型API的dimensions参数。Embedding客户端检查每条向量的长度及有限数值；存储层也验证向量。不能通过把本地预期改成3就把2048维Embedding变成3维。

## pc-d8-024 · knowledge

问题族：stable-content-identity；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 模型、维度或切分方案变化后，本项目为何要求新集合和新索引配置？这是否表示Qdrant本身禁止追加命名向量？

**已确认参考答案：** 项目要求新集合和新索引配置，以免新旧结果混入原基线，这是为实验可复现制定的约束；不表示Qdrant所有版本都禁止增加其他命名向量。

证据 pc-d8-024-e1：[Qdrant向量契约笔记 / 本地维度预期不是模型参数](corpus/qdrant_vector_contract.md#本地维度预期不是模型参数)；来源类型official_documentation_note，字符区间[894, 987)，page=null。

> 模型、维度或切分方案改变后，需要按项目规则使用新集合和新索引配置，不能将新旧结果混在原基线里。这个约束是本项目为可复现实验作出的选择，不是断言Qdrant所有版本都禁止增加其他命名向量。

## pc-d8-025 · terminology

问题族：upload-response-stages；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 上传日志中先出现HTTP100 Continue再出现413，两种状态分别表示什么，最终应按哪个判断结果？

**已确认参考答案：** 100 Continue是允许继续发送请求体的临时响应；413才是本次超限上传的最终拒绝状态，不能把100当成上传成功。

证据 pc-d8-025-e1：[单文件上传排查笔记 / 类型、空文件和额度](corpus/upload_troubleshooting.md#类型空文件和额度)；来源类型project_experience，字符区间[850, 912)，page=null。

> 用户在超限上传时先看见100 Continue，再看见413。100是允许继续发送请求体的临时响应，最终拒绝状态仍是413。

## pc-d8-026 · terminology

问题族：named-vector-spaces；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** Qdrant的collection与point是什么关系？一个point除了向量还可以携带什么，命名向量又扩展了什么？

**已确认参考答案：** collection组织可检索points；point可以带向量及payload，命名向量允许同一point有多个向量空间，各空间有自己的维度和距离度量。

证据 pc-d8-026-e1：[Qdrant向量契约笔记 / Collection与命名向量](corpus/qdrant_vector_contract.md#collection与命名向量)；来源类型official_documentation_note，字符区间[207, 346)，page=null。

> collection组织可检索的points，一个point可以带向量及payload。同一向量空间的维度和距离度量须遵循集合配置；命名向量允许在一个point上声明多个向量空间，各自有自己的维度和度量。不能把“一个collection里只能有一种维度”套用于所有命名向量场景。

## pc-d8-027 · terminology

问题族：citation-assembly；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** citation_numbers和返回给用户的原文、来源、页码是什么关系？这些引用元数据由模型直接填写吗？

**已确认参考答案：** 模型只产生status、answer、citation_numbers；后端先做JSON和ModelAnswer校验，再按本次上下文检查编号越界，最后根据编号从真实证据构造原文、来源、页码元数据。

证据 pc-d8-027-e1：[Dense身份与引用排查笔记 / 引用契约与事实支持](corpus/dense_identity_and_citations.md#引用契约与事实支持)；来源类型project_experience，字符区间[962, 1115)，page=null。

> 模型只生成status、answer和citation_numbers。后端先解析JSON并用ModelAnswer校验状态、引用是否为空及编号是否重复；回答编排再结合本次上下文检查编号是否越界。随后按编号从本次真实证据构造原文、来源、页码等引用元数据。answered必须有引用，证据不足时引用必须为空。

## pc-d8-028 · exact_match

问题族：stable-content-identity；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** D4～D6默认Qdrant集合名、向量空间名、维度、距离度量及Embedding模型分别是什么？

**已确认参考答案：** 集合pc_knowledge_dense_v0，单一命名向量dense，2048维，Cosine距离，Embedding模型embedding-3。

证据 pc-d8-028-e1：[Qdrant向量契约笔记 / 本项目采用的契约](corpus/qdrant_vector_contract.md#本项目采用的契约)；来源类型official_documentation_note，字符区间[443, 605)，page=null。

> D4～D6的集合名称默认是pc_knowledge_dense_v0，仅配置一个名为dense的向量空间，维度2048，距离为Cosine，Embedding模型是embedding-3。DenseIndexSpec把模型、维度、向量名、距离、切分版本等纳入索引配置指纹。本阶段没有启用稀疏向量、Hybrid或Rerank。

## pc-d8-029 · exact_match

问题族：service-port-storage；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 当前Compose配置把Qdrant的HTTP端口绑定到宿主机哪个地址和端口，容器端口与持久卷挂载目录分别是什么？

**已确认参考答案：** 宿主机绑定127.0.0.1:6433，转发到容器6333；命名卷qdrant_data挂载到/qdrant/storage。

证据 pc-d8-029-e1：[Compose环境与服务边界笔记 / 端口和持久卷](corpus/compose_environment.md#端口和持久卷)；来源类型official_documentation_note，字符区间[1028, 1157)，page=null。

> 端口127.0.0.1:6433:6333表示宿主机回环地址的6433端口转发到容器6333端口，访问范围限制在本机绑定地址。qdrant_data:/qdrant/storage把命名卷挂到容器数据目录，容器与持久数据有各自生命周期，排障时不能随意删除卷。

## pc-d8-030 · paraphrase

问题族：curl-upload-form；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 把curl命令拆成几行后，终端说--show-errorhttp://127.0.0.1这个选项不存在，是上传接口报错了吗？

**已确认参考答案：** 不是接口响应；Shell续行反斜杠只消除换行，不会补空格，--show-error与下一行URL粘连成未知选项，错误发生在请求发送之前。

证据 pc-d8-030-e1：[单文件上传排查笔记 / curl文件表单](corpus/upload_troubleshooting.md#curl文件表单)；来源类型project_experience，字符区间[456, 526)，page=null。

> Shell续行反斜杠消除换行，不会自动补空格；--show-error后缺少分隔空格会和下一行URL拼成未知选项，该错误发生在发送请求之前。

## pc-d8-031 · paraphrase

问题族：compose-variable-layers；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 目前启动qdrant少写了--env-file .env.example，会导致镜像、映射端口或卷变成另一套值吗？

**已确认参考答案：** 不会：截至D6的YAML只有固定镜像、端口和卷，没有变量占位符或服务级environment/env_file；所以该命令行选项不改变这些值，可以直接docker compose up -d qdrant。

证据 pc-d8-031-e1：[Compose环境与服务边界笔记 / 当前项目为什么可以省略](corpus/compose_environment.md#当前项目为什么可以省略)；来源类型official_documentation_note，字符区间[721, 923)，page=null。

> 截至D6，docker-compose.yml只有qdrant服务，镜像是qdrant/qdrant:v1.19.2；ports、volumes都是固定值，没有环境变量占位符，也没有服务级environment或env_file。因此--env-file .env.example不会改变该YAML中镜像、端口或卷的这些值。当前也可用docker compose up -d qdrant启动同一个服务。

## pc-d8-032 · chinese_long

问题族：local-run-id；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 同学看到测试代码覆盖409忙碌、整体body超限和上游异常，想把它们写成D6用户亲手验证过的分支。依据语料能这样登记吗？X-Run-ID和trace_exported=false又能证明哪些事？

**已确认参考答案：** 不能，这些分支由自动测试覆盖，本轮用户没有手测。X-Run-ID关联响应和完成日志，上传成功正文也返回同一run_id；trace_exported=false表示未导出Phoenix Trace，不能把本地关联记录算成已导出的Trace。

证据 pc-d8-032-e1：[单文件上传排查笔记 / 临时文件与请求关联](corpus/upload_troubleshooting.md#临时文件与请求关联)；来源类型project_experience，字符区间[1079, 1198)，page=null。

> 响应头X-Run-ID与完成日志关联；上传成功正文也有同一个run_id。trace_exported=false表示没有导出Phoenix Trace。并发忙碌409、整体body超限及上游异常由自动测试覆盖，本轮用户没有手测这些分支。

## pc-d8-033 · chinese_long

问题族：health-probe-boundary；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 我先看到应用/health返回200，再看到Qdrant的/readyz返回all shards are ready，就准备宣称模型密钥有效、RAG回答正确。这两项检查分别覆盖什么，为什么仍不足以得出这个结论？

**已确认参考答案：** 应用/health只读取配置并返回状态，没有探测模型或向量库；Qdrant /readyz成功仅表示存储服务就绪。两者都不能证明模型密钥有效或回答内容正确，还需对应的模型和回答验证。

证据 pc-d8-033-e1：[启动与配置排查笔记 / 健康检查的边界](corpus/startup_and_config.md#健康检查的边界)；来源类型project_experience，字符区间[798, 927)，page=null。

> GET /health从app.state.settings读取服务名和环境，返回status、service、environment。D1默认响应为HTTP200、status为ok。这个接口没有探测模型API或向量库，因此健康接口成功不证明外部依赖可用。

证据 pc-d8-033-e2：[Compose环境与服务边界笔记 / Qdrant与FastAPI的边界](corpus/compose_environment.md#qdrant与fastapi的边界)；来源类型official_documentation_note，字符区间[1314, 1551)，page=null。

> D6用户看到容器Up后，访问127.0.0.1:6433/readyz得到HTTP200及all shards are ready。该端口的readyz、collections和points/count均由Qdrant服务提供，不需要在本项目FastAPI里重写。项目API另由Uvicorn监听127.0.0.1:8002，提供health、documents/upload和rag/ask。Qdrant就绪只表示该存储服务的就绪检查成功，不能证明模型密钥或回答内容正确。

## pc-d8-034 · cross_passage

问题族：configuration-lifecycle；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 只执行docker compose up -d qdrant为什么不会同时启动项目/health？应另外怎样启动项目API，配置又在启动的哪个阶段保存？

**已确认参考答案：** Compose只定义qdrant，FastAPI未容器化；要在项目根目录另执行uv run --frozen uvicorn project_copilot.main:app --host 127.0.0.1 --port 8002。Uvicorn导入并构建应用后，lifespan启动时load_settings读取配置并保存到application.state.settings。

证据 pc-d8-034-e1：[Compose环境与服务边界笔记 / 当前项目为什么可以省略](corpus/compose_environment.md#当前项目为什么可以省略)；来源类型official_documentation_note，字符区间[925, 1015)，page=null。

> 这不影响宿主机运行的Python应用：应用仍由自己的Settings读取本项目配置。FastAPI没有被该Compose文件定义成容器，不能把容器启动误解为同时启动了本项目API。

证据 pc-d8-034-e2：[启动与配置排查笔记 / 启动命令与执行顺序](corpus/startup_and_config.md#启动命令与执行顺序)；来源类型project_experience，字符区间[163, 343)，page=null。

> 在项目根目录执行 `uv run --frozen uvicorn project_copilot.main:app --host 127.0.0.1 --port 8002`。uv使用项目环境和锁定依赖，Uvicorn导入main模块并取得app；模块末尾的create_app构建应用、注册路由和中间件。构建应用时不连接模型、Qdrant或Phoenix。

证据 pc-d8-034-e3：[启动与配置排查笔记 / 启动命令与执行顺序](corpus/startup_and_config.md#启动命令与执行顺序)；来源类型project_experience，字符区间[345, 485)，page=null。

> 进入lifespan启动阶段后，load_settings读取本项目配置，结果保存到application.state.settings。启动成功才开始处理请求；关闭时执行lifespan的finally并记录停止日志。用户D1真实操作已观察到启动完成及Ctrl+C后的正常关闭。

## pc-d8-035 · cross_passage

问题族：stable-content-identity；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 入库在Embedding前对成功清单和现有集合分别检查什么？不兼容时会删除集合重建吗，本地清单原子替换能当成跨存储事务保证吗？

**已确认参考答案：** 成功清单检查同集合的模型、维度、切分等配置；集合检查恰有预期dense向量，维度和距离匹配。已知不兼容在Embedding前拒绝，不会自动删除集合或改写旧索引；本地文件原子替换不代表Qdrant与清单之间有跨存储事务。

证据 pc-d8-035-e1：[Dense身份与引用排查笔记 / 成功清单与重复上传](corpus/dense_identity_and_citations.md#成功清单与重复上传)；来源类型project_experience，字符区间[700, 827)，page=null。

> 入库依次完成加载、切分、检查清单与集合契约、Embedding、Qdrant upsert，最后写入本地成功清单。清单保存同一集合的模型、维度、切分等配置；不一致时拒绝混用。清单文件用临时写入加原子替换，不能据此声称Qdrant与本地文件具备跨存储事务。

证据 pc-d8-035-e2：[Qdrant向量契约笔记 / 本项目采用的契约](corpus/qdrant_vector_contract.md#本项目采用的契约)；来源类型official_documentation_note，字符区间[607, 723)，page=null。

> DenseQdrantStore会检查现有集合恰有预期的dense向量，维度及距离均符合契约。发现配置不兼容时明确拒绝，不自动删除集合或改写旧索引。入库在Embedding前先检查成功清单和集合，避免已知存储不兼容时仍产生模型请求。

## pc-d8-036 · unanswerable

问题族：missing-compose-client-version；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 本项目D6手测时，宿主机Docker Compose客户端的精确版本号是多少？

**已确认无答案理由：** 五份语料没有记录Docker Compose客户端精确版本；Compose笔记还明确说明该值未记录。不能把Qdrant镜像或服务版本当成Compose客户端版本，须人工检查全体语料确认。

检查范围：sources.jsonl列出的完整5份语料；gold证据为空。

## pc-d8-037 · unanswerable

问题族：missing-mcp-deployment；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 依据这五份语料，Project Copilot已经部署的MCP Server使用哪个传输方式和监听地址？

**已确认无答案理由：** 五份语料只描述现有启动、上传、Dense和Qdrant/Compose契约，没有已部署MCP Server的传输方式或监听地址。不能从FastAPI或Qdrant端口推导MCP服务配置，须人工检查全体语料确认缺失。

检查范围：sources.jsonl列出的完整5份语料；gold证据为空。

## pc-d8-038 · unanswerable

问题族：missing-hnsw-configuration；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** pc_knowledge_dense_v0集合实际使用的HNSW m和ef_construct数值分别是多少？

**已确认无答案理由：** 五份语料给出集合名、向量名、维度与距离，但没有该集合的HNSW实际配置或查询记录；不能以服务版本或常见默认值冒充实际值，须人工检查全体语料确认缺失。

检查范围：sources.jsonl列出的完整5份语料；gold证据为空。

## pc-d8-039 · follow_up

问题族：citation-assembly；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 那证据不足时也得填一个编号吗？

固定历史：

- user：Project Copilot模型返回的answered回答可以没有引用吗？
- assistant：answered必须带有引用，后端还会检查引用编号。

**已确认独立问题：** Project Copilot在证据不足时应如何填写citation_numbers，是否必须为了满足answered契约填入一个编号？

**已确认参考答案：** 不需要，证据不足时引用必须为空；不能为了满足answered必须有引用的契约而随意填编号。

证据 pc-d8-039-e1：[Dense身份与引用排查笔记 / 引用契约与事实支持](corpus/dense_identity_and_citations.md#引用契约与事实支持)；来源类型project_experience，字符区间[962, 1115)，page=null。

> 模型只生成status、answer和citation_numbers。后端先解析JSON并用ModelAnswer校验状态、引用是否为空及编号是否重复；回答编排再结合本次上下文检查编号是否越界。随后按编号从本次真实证据构造原文、来源、页码等引用元数据。answered必须有引用，证据不足时引用必须为空。

## pc-d8-040 · follow_up

问题族：service-port-storage；人工状态：confirmed（复核者：yanye，批量确认）。

**问题：** 那它和容器是同一个生命周期吗，排障可以随意删除吗？

固定历史：

- user：Compose里的qdrant_data是用来做什么的？
- assistant：它是挂载到Qdrant容器/qdrant/storage的命名卷，用于持久数据。

**已确认独立问题：** 当前Compose的qdrant_data持久卷与Qdrant容器是否共享同一个生命周期，排障时能否随意删除该卷？

**已确认参考答案：** 不是，容器与持久数据有各自生命周期；qdrant_data是挂载到/qdrant/storage的命名卷，排障时不能随意删除。

证据 pc-d8-040-e1：[Compose环境与服务边界笔记 / 端口和持久卷](corpus/compose_environment.md#端口和持久卷)；来源类型official_documentation_note，字符区间[1028, 1157)，page=null。

> 端口127.0.0.1:6433:6333表示宿主机回环地址的6433端口转发到容器6333端口，访问范围限制在本机绑定地址。qdrant_data:/qdrant/storage把命名卷挂到容器数据目录，容器与持久数据有各自生命周期，排障时不能随意删除卷。
