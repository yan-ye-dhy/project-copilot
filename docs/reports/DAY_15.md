# 【Day 15 开发报告】

开发与Codex自动验证日期：2026-10-23；学习与用户手动验证补记：2026-10-25。**D15开发提交9c332a5已由用户推送，用户已完成五个GPU手测环节并确认ChatGPT学习理解，阶段A/B均完成；本次收尾文档待用户提交。** 本日保持现有Dense问答默认与750/150切分，D16完整效果实验尚未开始。

## 1. 今日开发目标

接入一个固定版本的中文/多语言Cross-Encoder候选，先确认本机资源、候选身份、输入截断和加载/推断耗时。只用公开D14归档的一道真实问题及一条独立自编长中文片段验证链路，保留输入排名、检索分数、重排logit、原文和metadata，中文整体效果留待D16对照。

## 2. 新增和修改的文件

**新增：**

- `src/project_copilot/rag/rerank.py`：冻结配置、候选重排合同、惰性本地模型加载与逐对token诊断。
- `scripts/rerank_smoke.py`：显式`--local`门禁，核验公开D14归档后，对固定一题的相同10候选评分两遍。
- `tests/test_rerank.py`、`tests/test_rerank_smoke.py`：61项核心合同和9项CLI测试；评分及模型依赖使用fake/mock。
- [CPU自动验证证据](../evidence/D15_RERANK_SMOKE.json)、[GPU自动验证证据](../evidence/D15_GPU_RERANK_SMOKE.json)与本报告：分别保存真实配置、硬件/依赖、各自27份源码/依赖SHA、候选与完整选中片段、诊断及活动计数；CPU证据原字节保留。

**修改：** `pyproject.toml`、`uv.lock`新增互斥的可选`rerank`（CPU）/`rerank-gpu`（CUDA）依赖组及PyTorch官方索引；README、开发计划第10节、开发日志、学习笔记、面试笔记和evals/README补充D15入口、结果及边界。模型缓存位于已忽略的`models/huggingface`，不提交权重。

## 3. 核心实现内容

1. `RerankConfig`记录模型revision、device/dtype、线程数和输入/候选预算。`Reranker`只排序给定候选，拒绝重复身份、超预算、非有限或数量不符的分数；并列沿用原候选顺序，返回原`RetrievedChunk`及原排名、独立的`rerank_score`。
2. `LocalCrossEncoder`首次非空评分才导入并加载模型，固定`local_files_only=True`、`trust_remote_code=False`。缺缓存或执行失败即报错，不自动下载、回退或重试。返回未经过sigmoid的原始logit，负数有效，也不是答案概率。
3. 模型输入使用`truncation="only_second"`，保留完整query，只截断passage的token输入；query自身用尽预算时，在模型推断前拒绝。逐候选记录原pair token数、实际输入token数及是否截断，返回证据仍保留完整原文。
4. CLI在导入项目/重模型依赖和读取评测输入前检查`--local`，缺开关退出2。开启后先用`verify_experiment`验证公开D14归档与冻结数据，固定`pc-d7-012`的原序10候选，首遍及同实例热重复均选5块；错误仅向stderr输出安全JSON并退出1。

## 4. 关键设计决策

固定`BAAI/bge-reranker-v2-m3`及revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`，batch=2、max_length=512、10候选→5块。先完成CPU float32/4线程验证，随后按用户明确选择，使用RTX 4060 Laptop执行CUDA float16验证；模型实际参数设备与精度均已核对。512是项目输入预算，CPU与GPU共用同一模型缓存、问题和候选。

依赖放入互斥可选组，CPU历史实测Torch **2.14.1+cpu**，GPU实测Torch **2.14.1+cu130**、CUDA编译版本13.0，Transformers均为 **5.20.2**。基础服务默认不装这两个组，普通API启动不加载模型；GPU运行使用`--group rerank-gpu --local --device cuda --dtype float16`，两组同时选择会明确拒绝。参考旧项目`utils/reranker.py`的结果身份映射思路，在新项目独立实现配置、重排和诊断，没有沿用旧FlashRank默认模型。现有问答API、Dense默认、切分、Prompt/Schema和历史归档保持。

本日只确认适配与指定样例的实际排序，候选外证据无法由重排补出。原检索score与模型logit分别保存，不相加，也不据单题结果选定最终默认方案。

## 5. 自动测试结果

执行者均为Codex，工作目录新项目；以下不登记为用户学习或手动测试。真实结果详见[D15证据](../evidence/D15_RERANK_SMOKE.json)，完整执行命令与环境补记见[开发日志](../DEVELOPMENT_LOG.md)。

| 实际验证 | 结果与范围 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_rerank.py` | 61项通过；fake模型/Tokenizer覆盖身份、预算、分数校验、截断、加载失败与诊断，不代表真实模型效果 |
| `.venv/bin/python -m pytest -q tests/test_rerank_smoke.py` | 9 passed，2.81秒；覆盖纯标准库默认门禁、真实子进程入口、公开归档变化拒绝、缺缓存、安全错误和无重试；本日共70项新增用例通过 |
| 相关9文件联合回归 | 沙箱外CPU环境235 passed，21.49秒；随后CUDA环境同一235项再次通过，22.16秒，仍含70项D15新增，不重复累加为470项不同测试；覆盖rerank、CLI、fusion、hybrid、hybrid_run、rag_api、rag_pipeline、answering、main。此前沙箱内在旧ASGI首例阻塞，两次人工中断均退出130；该旧例在沙箱外单独1 passed、0.84秒，未改业务源码绕过 |
| 显式准备固定revision模型缓存 | 6文件共2,293,242,108字节，下载及哈希158.275秒；其中权重2,271,071,852字节，SHA-256与固定commit公布值匹配。下载属于准备步骤，CLI不执行下载 |
| `HF_HUB_OFFLINE=1 … .venv/bin/python scripts/rerank_smoke.py --local` | 退出0；公开归档先verified，真实012题首/热各10对，共20对；每遍保存10个原始分数/排名、raw logit及5块完整原文/metadata，Qdrant请求0 |
| 独立自编长中文pair诊断 | 1对真实评分，原pair 2120 tokens→512，`passage_truncated=true`；原文3800字符及哈希保持。另一个超长query在推断前拒绝，推断耗时0 |

真实012题中，目标块`17b8925a-2b6a-5793-87ad-1ca2c52f581e`从Hybrid原rank 3升至重排rank 1，原score为0.0320184426，raw logit为−3.1111164093。首/热两遍全部分数及选中顺序一致，候选原pair为348～462 tokens，均未截断；这只是一题的观察，没有计算完整dev或保留集Recall/MRR，也没有生成答案或评测拒答/引用质量。

| 真实CLI计时/资源 | 实际结果 |
| --- | --- |
| 进程内首次模型加载 | 3.5808秒，单独列出 |
| 首遍10对纯模型推断 / 含加载等的重排总计 | 9.3705秒 / 12.9636秒 |
| 同实例热重复10对纯模型推断 / 重排总计 | 8.1315秒 / 8.1437秒，本遍模型加载0 |
| CLI进程wall / peak RSS | 22.89秒 / 2,097,024KiB，约2.0GiB |

机器为Linux x86_64、Python 3.13.14、i5-13500HX、20逻辑CPU，观测内存7,973,540kB。独立合成诊断先于CLI运行，模型文件已下载且OS文件缓存未清；上述首次加载仅指该进程首次加载，不能称为冷盘测速。单轮首/热差异不构成统计提速或生产吞吐结论。

合计成功本地评分调用**3次、21个query-passage pair**，另有1次超长query在推断前拒绝；外部模型API调用、Qdrant请求均**0**。没有读取真实`.env`，没有收费模型调用；本地模型推断不能写成“模型调用0”。

### 按用户选择补充GPU验证

CUDA可选依赖准备约7分22秒，复用已缓存的模型，不重新下载权重。采用现有`run_smoke(..., device="cuda", dtype="float16")`路径；开发期临时wrapper在每遍前后同步CUDA、重置并记录allocator峰值，逐一验证所有浮点参数确实位于`cuda:0`且为`torch.float16`。wrapper完整源码及SHA封存在GPU证据中，生产适配器和CLI源码与CPU阶段相同。

| GPU真实计时/资源 | 实际结果 |
| --- | --- |
| 设备 / 构建 | RTX 4060 Laptop，compute capability 8.9；Torch2.14.1+cu130，CUDA编译版本13.0，观测驱动610.62 |
| 本进程首次模型加载 | 10.8562秒；Torch已预导入、CUDA context已初始化，非冷盘或完整进程启动 |
| 首遍10对模型推断 / 重排总计 | 1.1124秒 / 12.0078秒 |
| 同实例热重复10对模型推断 / 重排总计 | 160.087毫秒 / 174.468毫秒，本遍模型加载0 |
| 每遍PyTorch allocator峰值 allocated / reserved | 1,166,032,896 / 1,189,085,184字节；allocated约1.09GiB，不是全卡或全部CUDA context显存峰值 |
| GPU验收进程host peak RSS | 4,593,236KiB，约4.38GiB；不同于显存统计 |

同一012题的10个候选及输入SHA保持，目标仍由3排到1，GPU目标raw logit为−3.109375。GPU首/热两遍分数相同，选中原名次均为`[3,4,2,1,5]`，与CPU选中顺序相同；GPU float16相对CPU float32的最大logit差约0.087717，不能声称跨精度分数相同。硬件和精度同时变化，单题首/热记录不能当成单因素加速或生产吞吐结论。

GPU新增2次成功本地评分、20个pair；与前述CPU阶段合计**5次、41个pair**，超长query推断前拒绝仍为CPU阶段1次，外部模型API/Qdrant均0。没有另跑GPU float32或完整开发/保留集。首遍峰值含模型加载，热遍峰值含常驻模型基线，显存测量范围在证据中明确。

CPU证据SHA仍为`7040b1cc02099e0c083a1da588d8fa528ce24d5bd65d0bd7258165aa6f1b2ff6`；GPU证据另记当前27份源码/依赖SHA。相对CPU阶段仅`pyproject.toml`、`uv.lock`有新身份，其余25份注册源码保持；旧CPU依赖hash表示历史执行，不回填为当前CUDA依赖。验证细节见[GPU证据](../evidence/D15_GPU_RERANK_SMOKE.json)。

### 学习后用户手动验证（2026-10-25）

以下由用户在项目根目录亲自执行，Codex只读核对保存结果；开发期自动测量保持原样，不登记为用户操作。完整命令见 [开发日志](../DEVELOPMENT_LOG.md)。

| 用户实际操作 | 实际结果与核对 |
| --- | --- |
| `uv run --frozen --group rerank-gpu python scripts/rerank_smoke.py`，未加`--local` | 参数保护按预期退出2，提示未加载模型或读取评测输入 |
| 通过同一依赖组检查Torch/CUDA，再在`device="cuda"`上创建小张量并加1 | Torch2.14.1+cu130、CUDA编译版本13.0、available=True、RTX4060 Laptop；张量cuda:0、结果2.0、退出0，不加载重排模型 |
| `HF_HUB_OFFLINE=1 HF_HUB_DISABLE_PROGRESS_BARS=1 uv run --frozen --group rerank-gpu python scripts/rerank_smoke.py --local --device cuda --dtype float16`，输出到`data/manual-d15/smoke-gpu.json` | 退出0，D14来源先verified；首/热各10对、各选5块，新增2次本地评分/20pair，外部API/Qdrant均0。Codex核对20条输入的ID/原分数/文本SHA和10条选中完整hit，与原归档逐值一致 |
| 在`first_pass.selected_hits`观察第一块，用户反馈“已看到” | 目标17b8925a...原名次3→新名次1、来源dense_identity_and_citations.md；原RRF score约0.03201844保留，新logit=-3.109375，原文包含改名不改变内容身份的说明 |
| 指定不存在的`--cache-dir data/manual-d15/missing-model-cache`，错误写入`missing-cache-error.json` | 退出1，固定JSON error提示缺少本地快照、不自动下载；Codex只读核对错误文件和测试路径仍不存在，此时未创建模型评分器 |

本次首模型加载11500.209ms，首遍模型推断926.304ms、整个重排12446.690ms；热重复模型加载0、推断146.443ms、整个重排158.899ms。两遍分数与选中名次`[3,4,2,1,5]`完全一致，均未触发截断；耗时是本轮用户实际结果，不覆盖10月23日开发期记录，也不推导生产P95。

用户最终选择“已学习，能够解释”，确认已通过ChatGPT理解候选→query/passage成对输入→logit→排序→完整原文与metadata返回，以及截断不修改返回原文的原因，阶段B据此完成。本人手测没有CPU推断、长文截断/超长query、逐模型参数device/dtype或显存插桩、全dev/保留集及回答评测；这些开发期记录和未测范围分别保留。CLI记录请求的cuda/float16配置，实际CUDA小张量就绪由用户另测，不能把自动wrapper的显存/逐参数检查改记为本轮用户CLI输出。

## 6. GitHub 提交信息

- 约定分支main、远端`https://github.com/yan-ye-dhy/project-copilot.git`。用户已亲自提交并推送 [9c332a57bf86053063080d6c802332372b7bb185](https://github.com/yan-ye-dhy/project-copilot/commit/9c332a57bf86053063080d6c802332372b7bb185)，说明为`feat(rag): add local BGE reranking and GPU validation`；只读核对main/origin/main同指该提交，阶段A完成。Codex未执行Git写操作。
- 运行证据记录base commit为`4e3cfbae05150a11f0a2ae17ea6596da06317387`、`dirty=true`，对应开发期未提交内容；两份证据各自的27份源码/依赖SHA用于追溯对应CPU/GPU实际运行，不追填未来commit。
- 2026-10-25本次只补7份Markdown学习/手测记录，仍待用户亲自add、commit、push；未预填收尾提交ID或推送状态。

## 7. 后续学习提示

1. Bi-Encoder候选检索与Cross-Encoder联合输入评分的分工，为什么重排不能补出候选外证据。
2. `RetrievedChunk`→`RerankedHit`的身份传递，如何同时保留原rank/score与新rank/logit，以及并列排序规则。
3. query/passage成对token输入、特殊token预算、`only_second`和完整原文保留之间的关系。
4. 惰性本地加载、冻结revision、可选依赖和CLI门禁的运行链；模型加载失败为何直接暴露而不替换结果。
5. 结合真实012题及合成诊断，区分原logit、答案置信度、单题排序变化、完整检索指标和不同计时范围。

## 8. 下一步计划

开发、Git同步、ChatGPT学习和用户手动验证均已完成，接下来指导用户审核并亲自提交、推送本次7份收尾文档。原CPU/GPU自动证据、运行身份及源码保持，不为更新人工状态回写归档。

D15阶段A/B均已完成，仍等待用户明确开启D16，再基于相同Hybrid候选和固定最终k开展V3效果对照。本日不跑完整dev、不用保留集选参，也不切换默认问答方案。
