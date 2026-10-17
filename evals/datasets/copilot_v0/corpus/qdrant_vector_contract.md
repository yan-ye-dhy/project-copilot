# Qdrant向量契约笔记

资料核对日期：2026-10-16。适用范围：本项目Qdrant服务v1.19.2、qdrant-client 1.19.1，项目代码快照3f3159965b94655095acca599333e318dc5287b7。官方在线资料会更新，本文只摘述下列基础概念与项目已有实现。本文是Codex自编来源草稿，待人工核验，不是人工gold。

## Collection与命名向量

collection组织可检索的points，一个point可以带向量及payload。同一向量空间的维度和距离度量须遵循集合配置；命名向量允许在一个point上声明多个向量空间，各自有自己的维度和度量。不能把“一个collection里只能有一种维度”套用于所有命名向量场景。[Qdrant官方Collection说明](https://qdrant.tech/documentation/manage-data/collections/)

## 本项目采用的契约

D4～D6的集合名称默认是pc_knowledge_dense_v0，仅配置一个名为dense的向量空间，维度2048，距离为Cosine，Embedding模型是embedding-3。DenseIndexSpec把模型、维度、向量名、距离、切分版本等纳入索引配置指纹。本阶段没有启用稀疏向量、Hybrid或Rerank。

DenseQdrantStore会检查现有集合恰有预期的dense向量，维度及距离均符合契约。发现配置不兼容时明确拒绝，不自动删除集合或改写旧索引。入库在Embedding前先检查成功清单和集合，避免已知存储不兼容时仍产生模型请求。

## 本地维度预期不是模型参数

PROJECT_COPILOT_EMBEDDING_EXPECTED_DIMENSIONS只用于本地响应与索引校验，不是发给模型API的dimensions参数。Embedding客户端检查每条向量的长度及有限数值；存储层也验证向量。不能通过把本地预期改成3就把2048维Embedding变成3维。

模型、维度或切分方案改变后，需要按项目规则使用新集合和新索引配置，不能将新旧结果混在原基线里。这个约束是本项目为可复现实验作出的选择，不是断言Qdrant所有版本都禁止增加其他命名向量。

## 检索分数的含义

Qdrant按选择的相似度或距离检索并给出score；排序与阈值含义取决于所用度量。[官方搜索说明](https://qdrant.tech/documentation/search/search/)

本项目的score表示Cosine相关度，不是答案正确概率或人工标注的置信度。分数高也可能只说明文本语义接近，不能据此跳过证据支持判断，更不能把0.9解释为答案有90%概率正确。可靠性要分别核对检索证据、回答内容及拒答表现。

## 项目来源

- [向量库适配与集合校验](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/src/project_copilot/infrastructure/qdrant.py)
- [索引配置和score契约](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/src/project_copilot/schemas/retrieval.py)
- [本地预期维度配置](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/src/project_copilot/config.py)
- [模型请求及响应检查](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/src/project_copilot/infrastructure/models.py)
