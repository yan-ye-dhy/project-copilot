# D7逐题复核队列

以下20题及参考答案由Codex起草，全部pending，人工确认数为0。此文件用于学习和复核，不属于检索语料；正式字段以JSONL为准。

每题检查问题含义、来源版本、证据完整性与答案支持；同题多个证据单元均须支持。无答案题须检查全体5份语料，追问须检查历史和独立问题。实际复核后才更新labels.draft.jsonl，不能批量将未读草稿标confirmed。

D7开发准备与人工标注分别记录；学习后用户主动开启手动验证，再由Codex分步带练。

## pc-d7-001 · knowledge

问题族：configuration-lifecycle；人工状态：pending。

**问题：** Project Copilot 启动时，配置在什么阶段读取并保存到哪里？关闭时执行什么？

**参考答案候选：** 在lifespan启动阶段由load_settings读取，保存到application.state.settings；关闭时执行lifespan的finally并记录停止日志。

证据 pc-d7-001-e1：[启动与配置排查笔记 / 启动命令与执行顺序](corpus/startup_and_config.md#启动命令与执行顺序)；来源类型project_experience，字符区间[345, 485)，page=null。

> 进入lifespan启动阶段后，load_settings读取本项目配置，结果保存到application.state.settings。启动成功才开始处理请求；关闭时执行lifespan的finally并记录停止日志。用户D1真实操作已观察到启动完成及Ctrl+C后的正常关闭。

## pc-d7-002 · exact_match

问题族：environment-validation；人工状态：pending。

**问题：** 把PROJECT_COPILOT_ENVIRONMENT设为staging为什么不能启动？合法值有哪些？

**参考答案候选：** environment只接受development、test、production；staging不满足Settings字段约束，会使应用启动失败。

证据 pc-d7-002-e1：[启动与配置排查笔记 / 配置字段与启动失败](corpus/startup_and_config.md#配置字段与启动失败)；来源类型project_experience，字符区间[501, 643)，page=null。

> Settings使用PROJECT_COPILOT_前缀，默认服务名是Project Copilot，默认environment是development。app_name去除首尾空白后至少有一个字符；environment只接受development、test、production。

证据 pc-d7-002-e2：[启动与配置排查笔记 / 配置字段与启动失败](corpus/startup_and_config.md#配置字段与启动失败)；来源类型project_experience，字符区间[645, 784)，page=null。

> D1用户临时覆盖APP_NAME为Copilot Learning后，健康响应的service随之变化。将APP_NAME设为全空格，或将ENVIRONMENT设为staging，均触发Settings校验错误并使应用启动失败。命令前的临时变量只影响该次启动，不会改写配置文件。

## pc-d7-003 · paraphrase

问题族：model-key-lifecycle；人工状态：pending。

**问题：** 两家模型凭据还没配好，能先把后端启动起来吗？空白凭据会在哪一步被拒绝？

**参考答案候选：** 密钥字段可缺省，所以后端可以先启动；创建Embedding或Chat客户端时，工厂检查去除空白后的密钥，在发模型请求前拒绝空值。

证据 pc-d7-003-e1：[启动与配置排查笔记 / 密钥检查发生在哪里](corpus/startup_and_config.md#密钥检查发生在哪里)；来源类型project_experience，字符区间[943, 1056)，page=null。

> 两家模型密钥字段允许缺省，应用可以在没有模型密钥时启动。真正创建Embedding或Chat客户端时，工厂才取出密钥并去除空白；空值会在发出模型请求前拒绝。这和app_name、environment在启动阶段的字段校验不同。

## pc-d7-004 · exact_match

问题族：model-key-lifecycle；人工状态：pending。

**问题：** embedding_smoke.py没有--live时退出码是什么？加--live能绕过空白智谱密钥检查吗？

**参考答案候选：** 未带--live时退出码为2；--live只是显式实调开关，不能绕过密钥检查，空白智谱密钥仍返回配置错误、退出码1。

证据 pc-d7-004-e1：[启动与配置排查笔记 / 密钥检查发生在哪里](corpus/startup_and_config.md#密钥检查发生在哪里)；来源类型project_experience，字符区间[1058, 1171)，page=null。

> D2用户运行embedding_smoke.py时，不带--live得到退出码2；显式--live后返回一条2048维向量摘要；临时空白智谱密钥返回安全配置错误、退出码1。--live是命令行的显式实调开关，并不绕过密钥校验。

## pc-d7-005 · cross_passage

问题族：curl-upload-form；人工状态：pending。

**问题：** 要正确上传一个本地文档，应请求哪个方法和路径、使用哪个文件字段，并选择哪些后缀？

**参考答案候选：** 使用POST /documents/upload，只提供一个名为file的文件字段，按-F的file=@路径提交；实现支持.txt、.md、.pdf，并匹配基本MIME，内容仍须解析校验。

证据 pc-d7-005-e1：[单文件上传排查笔记 / curl文件表单](corpus/upload_troubleshooting.md#curl文件表单)；来源类型project_experience，字符区间[183, 361)，page=null。

> 上传入口是POST /documents/upload，只接受一个名为file的文件字段。正确的表单参数是 `-F 'file=@路径;type=application/pdf'`。大写-F提交multipart表单并自动选择POST；小写-f表示HTTP错误检查，不会创建文件表单。@必须紧接等号，表示读取本地文件；file=/@路径会变成普通字段内容。

证据 pc-d7-005-e2：[单文件上传排查笔记 / 类型、空文件和额度](corpus/upload_troubleshooting.md#类型空文件和额度)；来源类型project_experience，字符区间[542, 684)，page=null。

> 实现支持.txt、.md、.pdf，并对后缀及基本MIME做匹配检查；实际内容还要经过UTF-8或PDF解析，不能只凭Content-Type判断文件有效。用户把PDF的上传名称改成sample.exe得到415；0字节TXT得到422“上传文件不能为空”。这些拒绝发生在模型请求前。

## pc-d7-006 · exact_match

问题族：curl-upload-form；人工状态：pending。

**问题：** curl里-F与-f分别做什么？为什么上传时误用-f可能得到405？

**参考答案候选：** 大写-F提交multipart表单并自动选择POST；小写-f是HTTP错误检查，不创建文件表单，用户误用后实际GET，上传路由只接受POST而返回405。

证据 pc-d7-006-e1：[单文件上传排查笔记 / curl文件表单](corpus/upload_troubleshooting.md#curl文件表单)；来源类型project_experience，字符区间[183, 361)，page=null。

> 上传入口是POST /documents/upload，只接受一个名为file的文件字段。正确的表单参数是 `-F 'file=@路径;type=application/pdf'`。大写-F提交multipart表单并自动选择POST；小写-f表示HTTP错误检查，不会创建文件表单。@必须紧接等号，表示读取本地文件；file=/@路径会变成普通字段内容。

证据 pc-d7-006-e2：[单文件上传排查笔记 / curl文件表单](corpus/upload_troubleshooting.md#curl文件表单)；来源类型project_experience，字符区间[363, 526)，page=null。

> D6用户误写小写-f后实际发送GET，得到405及Allow: POST；在引号内给字段名加入换行后，又因缺少有效file上传字段得到422。修正为file=@路径后进入正常的文件校验。Shell续行反斜杠消除换行，不会自动补空格；--show-error后缺少分隔空格会和下一行URL拼成未知选项，该错误发生在发送请求之前。

## pc-d7-007 · paraphrase

问题族：empty-upload；人工状态：pending。

**问题：** 我传了一个完全没内容的TXT，服务会拿它去计算向量吗？会返回什么？

**参考答案候选：** 0字节TXT返回422“上传文件不能为空”，在模型请求前拒绝，不会对它计算向量。

证据 pc-d7-007-e1：[单文件上传排查笔记 / 类型、空文件和额度](corpus/upload_troubleshooting.md#类型空文件和额度)；来源类型project_experience，字符区间[542, 684)，page=null。

> 实现支持.txt、.md、.pdf，并对后缀及基本MIME做匹配检查；实际内容还要经过UTF-8或PDF解析，不能只凭Content-Type判断文件有效。用户把PDF的上传名称改成sample.exe得到415；0字节TXT得到422“上传文件不能为空”。这些拒绝发生在模型请求前。

## pc-d7-008 · terminology

问题族：upload-budget；人工状态：pending。

**问题：** 文件额度和整个HTTP请求体额度有什么区别？D6默认各是多少字节？

**参考答案候选：** 文件额度是5242880字节（5MiB）；整个body默认5308416字节，比文件多65536字节用于multipart封装；二者检查对象不同。

证据 pc-d7-008-e1：[单文件上传排查笔记 / 类型、空文件和额度](corpus/upload_troubleshooting.md#类型空文件和额度)；来源类型project_experience，字符区间[686, 848)，page=null。

> 默认文件额度是5242880字节，即5MiB。D6用户上传5242881字节的TXT得到413“上传文件超过允许的大小”。框架另外限制整个HTTP请求体，默认额度为文件上限加65536字节，即5308416字节，给multipart封装留空间。这两个检查对象不同；该次用户实测证明了文件额度分支，没有单独手测整个请求体上限。

## pc-d7-009 · chinese_long

问题族：temporary-upload-copy；人工状态：pending。

**问题：** 上传因大小限制失败后，我准备的测试文件还在磁盘，但后端/tmp副本消失了。这是清理不完整吗？说明两份文件的用途和清理责任。

**参考答案候选：** 不是：data/manual-d6保存用户输入样本，服务端另外复制到随机前缀临时文件，原名只作来源元数据，finally删除临时副本；用户输入样本不因此删除。

证据 pc-d7-009-e1：[单文件上传排查笔记 / 临时文件与请求关联](corpus/upload_troubleshooting.md#临时文件与请求关联)；来源类型project_experience，字符区间[928, 1077)，page=null。

> 服务端将内容复制到随机的project-copilot-upload-前缀临时文件，原文件名仅作来源元数据。finally负责删除临时副本；用户保存在data/manual-d6的输入样本不会因此删除。D6用户在失败后检查/tmp为0；完成真实上传、问答并停止后端后再次检查，残留临时副本数也为0。

## pc-d7-010 · terminology

问题族：local-run-id；人工状态：pending。

**问题：** X-Run-ID、上传正文run_id和trace_exported=false分别说明什么？能据此认定Phoenix已导出吗？

**参考答案候选：** X-Run-ID关联响应与完成日志，上传成功正文也返回同值；trace_exported=false明确表示没有导出Phoenix Trace，不能将本地关联ID当作导出证据。

证据 pc-d7-010-e1：[单文件上传排查笔记 / 临时文件与请求关联](corpus/upload_troubleshooting.md#临时文件与请求关联)；来源类型project_experience，字符区间[1079, 1198)，page=null。

> 响应头X-Run-ID与完成日志关联；上传成功正文也有同一个run_id。trace_exported=false表示没有导出Phoenix Trace。并发忙碌409、整体body超限及上游异常由自动测试覆盖，本轮用户没有手测这些分支。

## pc-d7-011 · knowledge

问题族：pdf-page-identity；人工状态：pending。

**问题：** sample.pdf有3页却只得到2个片段，片段page应该是多少？空白页会改变后面的页号吗？

**参考答案候选：** 页索引1为空白，切分跳过空文本且不跨页合并，得到page0和page2两个片段；loader保留原页索引，不把页2改编号为页1。

证据 pc-d7-011-e1：[Dense身份与引用排查笔记 / 样本页码与原文](corpus/dense_identity_and_citations.md#样本页码与原文)；来源类型project_experience，字符区间[166, 296)，page=null。

> 自编sample.pdf共3页，原始页索引为0、1、2；页1为空白。loader保留全部页及原索引，splitter跳过空白文本且不跨页合并，所以默认只产生页0、页2的两个片段。空白页不会使原页2重新编号为页1；TXT和Markdown的page则为null。

## pc-d7-012 · paraphrase

问题族：stable-content-identity；人工状态：pending。

**问题：** 把同一文件改名再挪到另一个目录，内容没变，还算同一份文档吗？显示名称有什么作用？

**参考答案候选：** 文件字节不变时SHA-256与document_id不变；source_name仅展示来源，不参与文档身份。

证据 pc-d7-012-e1：[Dense身份与引用排查笔记 / 文档与片段身份](corpus/dense_identity_and_citations.md#文档与片段身份)；来源类型project_experience，字符区间[455, 552)，page=null。

> loader对一次读取的文件字节计算SHA-256，再以内容哈希生成稳定document_id。仅改文件名或物理路径，字节不变时文档身份不变。source_name负责展示来源，不参与文档身份。

## pc-d7-013 · cross_passage

问题族：stable-content-identity；人工状态：pending。

**问题：** 相同sample.pdf重传后为什么仍只有2个点？入库先后顺序和成功清单如何解释这个结果，它等于跨存储事务保证吗？

**参考答案候选：** 先加载切分并检查清单/集合，再Embedding、Qdrant upsert，最后写成功清单；重传复用稳定片段ID所以仍2点而非4点。清单原子替换不等于Qdrant与本地清单有跨存储事务，本例也不证明完整故障恢复。

证据 pc-d7-013-e1：[Dense身份与引用排查笔记 / 成功清单与重复上传](corpus/dense_identity_and_citations.md#成功清单与重复上传)；来源类型project_experience，字符区间[700, 827)，page=null。

> 入库依次完成加载、切分、检查清单与集合契约、Embedding、Qdrant upsert，最后写入本地成功清单。清单保存同一集合的模型、维度、切分等配置；不一致时拒绝混用。清单文件用临时写入加原子替换，不能据此声称Qdrant与本地文件具备跨存储事务。

证据 pc-d7-013-e2：[Dense身份与引用排查笔记 / 成功清单与重复上传](corpus/dense_identity_and_citations.md#成功清单与重复上传)；来源类型project_experience，字符区间[829, 946)，page=null。

> D6重复上传相同sample.pdf后，响应为indexed、2个片段、1167字节，清单仍是一份文档，Qdrant精确点数仍为2。upsert复用稳定片段ID，没有累加成4点。这个结果只说明该样本的身份复用，不等于完整故障恢复验证。

## pc-d7-014 · chinese_long

问题族：evidence-support；人工状态：pending。

**问题：** 回答已经是合法JSON、通过Pydantic且引用编号有效，能直接当成事实正确吗？项目记录要求继续核对什么，能否据单个样本报告完整准确率？

**参考答案候选：** 不能；这些只提供格式和身份约束，还需人工逐条核对问题、答案与原文证据是否支持事实。单个已知事实不能产生完整RAG准确率、Recall或MRR。

证据 pc-d7-014-e1：[Dense身份与引用排查笔记 / 引用契约与事实支持](corpus/dense_identity_and_citations.md#引用契约与事实支持)；来源类型project_experience，字符区间[1117, 1220)，page=null。

> JSON语法正确、Pydantic校验通过和引用编号有效，均不能自动证明答案得到原文事实支持；仍需人工逐条核对问题、答案与证据。本轮只验证了一个已知事实，不能据此报告完整RAG准确率、Recall或MRR。

## pc-d7-015 · knowledge

问题族：compose-variable-layers；人工状态：pending。

**问题：** Compose命令行--env-file需要在YAML中先声明吗？它与服务级env_file、environment的作用有什么区别？

**参考答案候选：** 命令行--env-file不需YAML同名声明，用来给Compose解析变量占位符；服务environment或env_file才配置传给容器的变量，命令行选项不会自动注入全部变量。

证据 pc-d7-015-e1：[Compose环境与服务边界笔记 / 命令行变量文件](corpus/compose_environment.md#命令行变量文件)；来源类型official_documentation_note，字符区间[223, 368)，page=null。

> docker compose --env-file .env.example up -d qdrant中的--env-file是Compose命令行选项，不要求YAML存在同名字段。它指定Compose读取的变量文件，供解析${变量名}之类占位符使用；不会仅凭这个选项就把所有变量注入容器。

证据 pc-d7-015-e2：[Compose环境与服务边界笔记 / 容器环境变量](corpus/compose_environment.md#容器环境变量)；来源类型official_documentation_note，字符区间[478, 603)，page=null。

> 服务的environment可以直接声明容器环境变量，也可引用Compose先解析出的值；服务的env_file让Compose从指定文件取得要传给该容器的变量。命令行--env-file与服务env_file属于不同配置层次，不能因名称相似而等同。

## pc-d7-016 · knowledge

问题族：named-vector-spaces；人工状态：pending。

**问题：** 一个Qdrant collection里所有向量都只能有同一种维度吗？命名向量如何改变这个判断？

**参考答案候选：** 不是；同一向量空间须遵循其维度和距离配置，但一个point可有多个命名向量空间，各自使用自己的维度和度量。

证据 pc-d7-016-e1：[Qdrant向量契约笔记 / Collection与命名向量](corpus/qdrant_vector_contract.md#collection与命名向量)；来源类型official_documentation_note，字符区间[207, 346)，page=null。

> collection组织可检索的points，一个point可以带向量及payload。同一向量空间的维度和距离度量须遵循集合配置；命名向量允许在一个point上声明多个向量空间，各自有自己的维度和度量。不能把“一个collection里只能有一种维度”套用于所有命名向量场景。

## pc-d7-017 · unanswerable

问题族：missing-benchmark-results；人工状态：pending。

**问题：** 本项目启用Rerank后在冻结保留集上的Recall@5提高了多少个百分点？

**无答案候选理由：** 本批语料没有Rerank实验或冻结保留集逐题/汇总结果，不能从能力描述推导提升数值；须人工检查全体语料后确认缺失。

检查范围：sources.jsonl列出的完整5份语料；gold证据为空。

## pc-d7-018 · unanswerable

问题族：missing-account-bill；人工状态：pending。

**问题：** 2026-10-16 D6用户手测固定问题的那次DeepSeek请求，在我的账号上实际扣了多少人民币？

**无答案候选理由：** 本批语料未提供该次账号账单、实际扣款或人民币结算信息；不能以调用成功、Token数或标价替代实付，须人工检查全体语料。

检查范围：sources.jsonl列出的完整5份语料；gold证据为空。

## pc-d7-019 · follow_up

问题族：upload-budget；人工状态：pending。

**问题：** 那整个请求体也只允许这么大吗？

固定历史：

- user：Project Copilot D6默认允许上传多大的文件？
- assistant：默认文件额度是5MiB，即5242880字节。

**独立问题候选：** Project Copilot D6默认文件额度5MiB时，整个HTTP请求体的默认字节额度是多少，额外空间有什么作用？

**参考答案候选：** 不是；文件默认5MiB，整个HTTP请求体默认5308416字节，多出的65536字节用于multipart封装。

证据 pc-d7-019-e1：[单文件上传排查笔记 / 类型、空文件和额度](corpus/upload_troubleshooting.md#类型空文件和额度)；来源类型project_experience，字符区间[686, 848)，page=null。

> 默认文件额度是5242880字节，即5MiB。D6用户上传5242881字节的TXT得到413“上传文件超过允许的大小”。框架另外限制整个HTTP请求体，默认额度为文件上限加65536字节，即5308416字节，给multipart封装留空间。这两个检查对象不同；该次用户实测证明了文件额度分支，没有单独手测整个请求体上限。

## pc-d7-020 · follow_up

问题族：evidence-support；人工状态：pending。

**问题：** 那能直接当成90%的正确率吗？

固定历史：

- user：Project Copilot的Qdrant检索结果出现Cosine score为0.9，这表示什么？
- assistant：这里的score表示文本的Cosine语义相关度。

**独立问题候选：** Project Copilot Qdrant的Cosine score为0.9，能解释成答案有90%概率正确吗，可靠性还应如何核对？

**参考答案候选：** 不能；Cosine score是文本语义相关度，不能把0.9解释为答案有90%概率正确，仍要核对检索证据、回答内容及拒答表现。

证据 pc-d7-020-e1：[Qdrant向量契约笔记 / 检索分数的含义](corpus/qdrant_vector_contract.md#检索分数的含义)；来源类型official_documentation_note，字符区间[1103, 1215)，page=null。

> 本项目的score表示Cosine相关度，不是答案正确概率或人工标注的置信度。分数高也可能只说明文本语义接近，不能据此跳过证据支持判断，更不能把0.9解释为答案有90%概率正确。可靠性要分别核对检索证据、回答内容及拒答表现。
