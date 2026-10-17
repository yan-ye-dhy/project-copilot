# Dense身份与引用排查笔记

资料日期：2026-10-16。适用范围：Project Copilot D3～D6，公开代码快照3f3159965b94655095acca599333e318dc5287b7。本文由Codex从自编样本、源码及实际记录整理，仍待人工核验，不代表已完成人工gold。

## 样本页码与原文

自编sample.pdf共3页，原始页索引为0、1、2；页1为空白。loader保留全部页及原索引，splitter跳过空白文本且不跨页合并，所以默认只产生页0、页2的两个片段。空白页不会使原页2重新编号为页1；TXT和Markdown的page则为null。

第一页片段原文是“Project Copilot page one: local document loading.”。页码0表示PDF第一页，不表示文件缺少第一页。D6用户问“样本文档第一页描述了什么功能？”，真实回答提到本地文档加载，引用来自sample.pdf页0，与该原文一致。

## 文档与片段身份

loader对一次读取的文件字节计算SHA-256，再以内容哈希生成稳定document_id。仅改文件名或物理路径，字节不变时文档身份不变。source_name负责展示来源，不参与文档身份。

chunk_id由文档身份、切分版本、原页索引、片段位置和片段文本哈希共同生成。切分参数或相关实现版本变化，会改变切分指纹并影响片段身份；相同原文出现在不同位置也不会合并为一个片段。D3用户验证了改名后身份稳定，以及将切分参数改为15/3后版本与片段ID变化。

## 成功清单与重复上传

入库依次完成加载、切分、检查清单与集合契约、Embedding、Qdrant upsert，最后写入本地成功清单。清单保存同一集合的模型、维度、切分等配置；不一致时拒绝混用。清单文件用临时写入加原子替换，不能据此声称Qdrant与本地文件具备跨存储事务。

D6重复上传相同sample.pdf后，响应为indexed、2个片段、1167字节，清单仍是一份文档，Qdrant精确点数仍为2。upsert复用稳定片段ID，没有累加成4点。这个结果只说明该样本的身份复用，不等于完整故障恢复验证。

## 引用契约与事实支持

模型只生成status、answer和citation_numbers。后端先解析JSON并用ModelAnswer校验状态、引用是否为空及编号是否重复；回答编排再结合本次上下文检查编号是否越界。随后按编号从本次真实证据构造原文、来源、页码等引用元数据。answered必须有引用，证据不足时引用必须为空。

JSON语法正确、Pydantic校验通过和引用编号有效，均不能自动证明答案得到原文事实支持；仍需人工逐条核对问题、答案与证据。本轮只验证了一个已知事实，不能据此报告完整RAG准确率、Recall或MRR。

## 可追溯来源

- [加载与原页码](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/src/project_copilot/rag/loaders.py)
- [切分与片段身份](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/src/project_copilot/rag/splitters.py)
- [入库与成功清单](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/src/project_copilot/rag/ingestion.py)
- [回答与引用映射](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/src/project_copilot/rag/answering.py)
- [D3用户验证记录](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/docs/reports/DAY_3.md)
- [D6自动与用户验证记录](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/docs/reports/DAY_6.md)
