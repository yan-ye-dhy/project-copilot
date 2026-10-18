# Copilot V0：语料、人工复核与划分

本目录服务于D7–D10的中文研发知识检索评测。D7的20题已于2026-10-17由用户核对确认；2026-10-18用户明确确认D8新增021–040全部通过，现40题、35可回答/5无答案、confirmed40/pending0。用户已亲自创建正式冻结并复算通过。结构检查通过本身不等于人工确认，数据冻结也不表示系统效果达标。

当前开发依据为D6收尾提交`3f3159965b94655095acca599333e318dc5287b7`。语料是已公开项目记录和官方资料的自编中文整理，不能宣称来自企业生产事故；问题为Codex拟题，不冒充真实用户原始提问。来源类型、参考链接/版本和取得日期逐文记录在sources.jsonl中。

## 文件关系

| 文件 | 用途 |
| --- | --- |
| `corpus/*.md` | 实际被评测的中文原文；不包含问题、标准答案、标签或评测报告 |
| `sources.jsonl` | 语料路径、来源类型、参考依据、D3文档UUID和字节SHA256版本 |
| `questions.jsonl` | 问题ID、题型、问题族、问题文字及追问的固定历史/独立问题 |
| `labels.draft.jsonl` | 参考答案、独立证据单元、原文定位和复核状态；40题均已人工confirmed，冻结保留此文件名 |
| `REVIEW_QUEUE.md` | 便于人工复核的问题/答案/原文视图；区分已确认与待复核，JSONL为数据依据 |
| `../../validate_dataset.py` | 离线检查来源、身份、版本、引用位置和状态；没有检索、评分或模型调用 |
| `split_manifest.json` | 已冻结的`copilot-v0-family-v1`划分：30题开发、10题保留 |
| `../../freeze_dataset.py` | 离线检查划分、全量人工确认后创建冻结清单、复算冻结指纹 |
| `frozen_manifest.json` | 用户全量人工确认后实际生成的冻结清单，登记9份输入的SHA256及总指纹 |

后续入库仅使用sources.jsonl列出的corpus文件，问题、标签和REVIEW_QUEUE不得作为检索语料，否则会把答案直接泄漏到知识库。D7未把新语料写入现有Qdrant，已有sample.pdf的2点验收数据也不属于此评测语料集。

## 证据身份与位置

document_id沿用D3的内容字节哈希→UUID规则，document_version为原文件字节的SHA256；修改文字后必须重新核对版本和位置。展示名、文档身份和证据单元ID各司其职，gold不保存chunk_id，也不绑定750/150切分结果。

本批Markdown使用UTF-8与LF换行。证据位置基于D3解析返回的原始文本，不移除Markdown标记、不折叠空白、不转换简繁；page为null。start_char/end_char是Python字符串的Unicode字符索引，采用左闭右开区间，必须满足`原文[start_char:end_char] == quote`。字节哈希与字符位置是不同概念，不能把UTF-8字节偏移当字符偏移。

同一题的多个evidence是不同且均必要的证据单元；跨段题必须一起支持参考答案。D9才实现片段映射和覆盖指标，字符位置重叠本身不证明片段足以支持答案；不能通过多切几个chunk增加gold数量。

## 问题与人工复核

题型使用knowledge、terminology、exact_match、paraphrase、chinese_long、cross_passage、unanswerable、follow_up，40题数量依次为8/5/5/5/4/4/5/4。family_id把同义题或同一追问链联系起来；23族的30/10划分已于D8冻结。覆盖八类只说明题型设计有覆盖，不能仅凭计数判断难度、证据质量或代表性。

follow_up保存固定history、原始追问和standalone_question，4题均随标签人工确认。后续检索对照使用独立问题以隔离查询理解，单轮RAG接口仍不因数据文件而自动具备多轮改写能力。其余题的history为空，standalone_question为null。

可回答题必须有参考答案及原文证据；无答案题的reference_answer为null、evidence为空，原因说明缺失什么信息。无答案是相对于本目录完整语料的判断，不能仅因检索未命中就改成无答案，也不能用模型常识补充语料没有的事实。

review_status初始为pending，reviewed_by/reviewed_at均为null。用户学习后判断来源是否可信、问题是否明确、证据是否完整、答案是否受支持；追问独立问题和无答案结论也须人工核对。有问题的题标needs_changes，实际确认后才填写人工复核者和ISO8601时间并标confirmed。实际核对后允许按明确题号范围批量确认，无需逐题重复书面依据，仅存疑题补充说明。Codex或ChatGPT整理草稿、验证字符串匹配，都不能代替人工确认。

用户主动开启D7手动验证后，先验证pending草稿被严格模式拒绝，再明确反馈“001–020已核对，全部通过”。据此将D7的20题登记为confirmed，reviewed_by=yanye，reviewed_at统一为批量确认登记时间2026-10-17T22:14:55+08:00；只改这三个复核字段，来源、问题、答案及证据保持原样。用户本人随后严格检查通过并确认学习理解，D7首批人工目标已完成。

D8新增20题初稿均为pending，原20题问题与标签保留原字节和确认记录。2026-10-18用户明确回复“021–040已核对，全部通过”，据此仅更新新增题的review_status、reviewed_by、reviewed_at，分别为confirmed、yanye、统一批量登记时间2026-10-18T21:41:27+08:00；问题、答案、证据及D7记录未改。复核按实际范围批量登记，不补造逐题理由或各题单独时间。程序仍无法识别使用不同family名称的语义近重复，不能代替这项人工判断。

## 开发/保留划分与冻结

`split_manifest.json`只保存schema_version、split_id及两侧问题ID，family以questions.jsonl为准，不重复维护。已冻结划分按完整问题族分组，依据题型覆盖选出30开发/10保留，没有使用任何检索结果或效果指标，也没有随机重抽挑选更好结果。

| 保留问题族 | 整族题号 |
| --- | --- |
| curl-upload-form | pc-d7-005、pc-d7-006、pc-d8-030 |
| upload-budget | pc-d7-008、pc-d7-019 |
| named-vector-spaces | pc-d7-016、pc-d8-026 |
| health-probe-boundary | pc-d8-021、pc-d8-033 |
| missing-hnsw-configuration | pc-d8-038 |

其余18族进入开发集；两侧八类均有覆盖，开发集26可答/4无答案，保留集9可答/1无答案。较大的stable-content-identity族6题完整放开发集，local-run-id的010/032、citation-assembly的027/039等也保持同侧。划分校验要求完整、不重不漏、同族同侧、总量30–50及约3:1、整体八类覆盖、每侧都有可答与无答案，不为凑分布拆开问题族。

保留集可以正常人工标注；它约束的是后续实验选参。D10只运行开发集基线；D11–D17用开发集选择方案，D17才用保留集比较已冻结V0与最终方案，不能根据保留集结果继续选择切分、融合、重排或Prompt。来自同5份语料且保留集只有10题，不能声称代表通用能力或统计显著。

正式冻结登记sources.jsonl、questions.jsonl、labels.draft.jsonl、split_manifest.json与5份已登记corpus的原始字节SHA256，共9份输入；再对排序后的文件指纹、split_id、schema版本与固定文本定位规则求总指纹。文本规则是UTF-8/LF、Markdown和空白保留、Unicode字符左闭右开位置，沿用D3 loader。README/REVIEW_QUEUE为展示视图，不参与数据身份；生成时间也不参与总指纹。D10另存模型、Prompt/Schema和实验配置，D8不冒充已冻结这些配置。

冻结后改动问题、答案、证据、来源版本、审核元数据或划分都会使原校验失败；新增其他未登记文件不会自动成为语料。工具拒绝覆盖已有冻结，故障或标签修正须明确记录新数据版本，不能悄悄覆写旧结果。指纹是完整性检查，不是签名或防篡改权限系统；本地单进程维护，不支持并发编辑时冻结。

2026-10-18用户实际执行`--freeze`退出0，随后`--verify`退出0，两次返回同一指纹，见 [正式冻结清单](frozen_manifest.json)：

```text
dataset-v1-54806b5174b2f3d2e4bd08b91b57569c5121929731112c3d375d4d42d4306b7e
```

清单记录UTC时间2026-10-18T13:51:42.739282+00:00，即北京时间21:51:42.739282。用户另将整个目录复制到被Git忽略的data/manual-d8/tampered，只修改副本第一题的参考答案；副本`--verify`退出1，提示指纹不匹配。正式9份输入仍与清单哈希一致，测试副本不属于评测语料或待提交数据。

## 离线检查与边界

开发者可运行：

```bash
uv run --frozen python evals/validate_dataset.py --dataset evals/datasets/copilot_v0
uv run --frozen python evals/validate_dataset.py --dataset evals/datasets/copilot_v0 --require-reviewed
```

第一条检查结构与原文一致性，返回pending/confirmed等计数；第二条要求全量人工确认。D8开发时新20题pending，严格门禁拒绝；现40题已确认。ready_for_baseline只代表人工复核状态，正式冻结以frozen_manifest及verify为准，不能据此声称D9评测器已验收。

D8新增工具从项目根目录按模块运行，`-m`让Python定位evals.freeze_dataset：

```bash
# 默认只检查划分，不写文件；当前为40题、30/10、ready_for_freeze=true
uv run --frozen python -m evals.freeze_dataset --dataset evals/datasets/copilot_v0
# 当前已有正式冻结，后续使用--verify复算；无需再次执行--freeze
uv run --frozen python -m evals.freeze_dataset --dataset evals/datasets/copilot_v0 --verify
```

默认检查与verify不修改数据；freeze不修改标签，只在门禁通过后独占创建frozen_manifest.json，已有文件拒绝覆盖。CLI成功退出0，数据/冻结门禁错误退出1，命令参数错误退出2，错误信息不回显问题、证据或密钥。开发期完整冻结仅在合成夹具验证；本次正式冻结由用户在实际人工确认后执行，两者分别记录。

工具不读取.env、不建立模型/数据库连接，不自动修改数据。真实来源、无答案充分性、题目质量和证据是否足以回答，仍依赖人工判断。本目录是小型自建项目数据集，存在题目由资料反向拟定、主题较窄的局限，不能宣称通用中文RAG能力或统计显著提升。
