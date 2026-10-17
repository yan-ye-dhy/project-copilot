# Copilot V0：语料与标注草稿

本目录服务于D7–D10的中文研发知识检索评测。D7整理5份可追溯语料、20题问题及证据候选；2026-10-17用户实际核对并明确确认001–020全部通过，现为20题人工gold。结构检查通过本身不等于人工确认，也不表示系统效果达标。

当前开发依据为D6收尾提交`3f3159965b94655095acca599333e318dc5287b7`。语料是已公开项目记录和官方资料的自编中文整理，不能宣称来自企业生产事故；问题为Codex拟题，不冒充真实用户原始提问。来源类型、参考链接/版本和取得日期逐文记录在sources.jsonl中。

## 文件关系

| 文件 | 用途 |
| --- | --- |
| `corpus/*.md` | 实际被评测的中文原文；不包含问题、标准答案、标签或评测报告 |
| `sources.jsonl` | 语料路径、来源类型、参考依据、D3文档UUID和字节SHA256版本 |
| `questions.jsonl` | 问题ID、题型、问题族、问题文字及追问的固定历史/独立问题 |
| `labels.draft.jsonl` | 参考答案、独立证据单元、原文定位和逐题人工复核状态；本批20题已confirmed，D8冻结前保留文件名 |
| `REVIEW_QUEUE.md` | 便于学习和人工复核的问题/答案/原文视图；已同步确认状态，JSONL为数据依据 |
| `../../validate_dataset.py` | 离线检查来源、身份、版本、引用位置和状态；没有检索、评分或模型调用 |

后续入库仅使用sources.jsonl列出的corpus文件，问题、标签和REVIEW_QUEUE不得作为检索语料，否则会把答案直接泄漏到知识库。D7未把新语料写入现有Qdrant，已有sample.pdf的2点验收数据也不属于此评测语料集。

## 证据身份与位置

document_id沿用D3的内容字节哈希→UUID规则，document_version为原文件字节的SHA256；修改文字后必须重新核对版本和位置。展示名、文档身份和证据单元ID各司其职，gold不保存chunk_id，也不绑定750/150切分结果。

本批Markdown使用UTF-8与LF换行。证据位置基于D3解析返回的原始文本，不移除Markdown标记、不折叠空白、不转换简繁；page为null。start_char/end_char是Python字符串的Unicode字符索引，采用左闭右开区间，必须满足`原文[start_char:end_char] == quote`。字节哈希与字符位置是不同概念，不能把UTF-8字节偏移当字符偏移。

同一题的多个evidence是不同且均必要的证据单元；跨段题必须一起支持参考答案。D9才实现片段映射和覆盖指标，字符位置重叠本身不证明片段足以支持答案；不能通过多切几个chunk增加gold数量。

## 问题与人工复核

题型使用knowledge、terminology、exact_match、paraphrase、chinese_long、cross_passage、unanswerable、follow_up。family_id把同义题或同一追问链联系起来；当前没有开发/保留划分，D8按问题族划分，随后才冻结版本。覆盖八类只说明题型设计有覆盖，不能仅凭计数判断难度、证据质量或代表性。

follow_up保存固定history、原始追问和standalone_question，本批独立问题已随标签人工确认。后续检索对照使用独立问题以隔离查询理解，单轮RAG接口仍不因数据文件而自动具备多轮改写能力。其余题的history为空，standalone_question为null。

可回答题必须有参考答案及原文证据；无答案题的reference_answer为null、evidence为空，原因说明缺失什么信息。无答案是相对于本目录完整语料的判断，不能仅因检索未命中就改成无答案，也不能用模型常识补充语料没有的事实。

review_status初始为pending，reviewed_by/reviewed_at均为null。用户学习后判断来源是否可信、问题是否明确、证据是否完整、答案是否受支持；追问独立问题和无答案结论也须人工核对。有问题的题标needs_changes，实际确认后才填写人工复核者和ISO8601时间并标confirmed。实际核对后允许按明确题号范围批量确认，无需逐题重复书面依据，仅存疑题补充说明。Codex或ChatGPT整理草稿、验证字符串匹配，都不能代替人工确认。

本轮用户已主动开启D7手动验证，先验证pending草稿被严格模式拒绝，再明确反馈“001–020已核对，全部通过”。据此将20题登记为confirmed，reviewed_by=yanye，reviewed_at统一为批量确认登记时间2026-10-17T22:14:55+08:00；只改这三个复核字段，来源、问题、答案及证据保持原样。用户本人随后严格检查通过并确认学习理解，D7首批人工目标已完成。D8继续到30–50题（建议40），仍待用户开启，不用形式合法的草稿替代数量和质量要求。

## 离线检查与边界

开发者可运行：

```bash
uv run --frozen python evals/validate_dataset.py --dataset evals/datasets/copilot_v0
uv run --frozen python evals/validate_dataset.py --dataset evals/datasets/copilot_v0 --require-reviewed
```

第一条检查结构与原文一致性，返回pending/confirmed等计数；第二条要求全量人工确认。用户复核前第二条退出1，登记后本人再次运行得到confirmed20/pending0、ready_for_baseline=true，退出0。该字段只代表本目录人工复核状态满足前置检查，D8分组划分/冻结与D9评测器仍需各自验收，不能据此跳到D10。

工具不读取.env、不建立模型/数据库连接，不自动修改数据。真实来源、无答案充分性、题目质量和证据是否足以回答，仍依赖人工判断。本目录是小型自建项目数据集，存在题目由资料反向拟定、主题较窄的局限，不能宣称通用中文RAG能力或统计显著提升。
