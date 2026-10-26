# 【Day 17 开发报告】

日期：2026-10-26。**D17实现、相关自动验证及真实保留集验收已完成；本次Git由Codex按用户明确授权提交推送，实际结果在会话补全。阶段B待ChatGPT学习和用户主动开启手动验证，D18未开始。** 先依dev选型并推送冻结配置，再运行保留集，未据保留集回头改选。

## 1. 今日开发目标

汇总V0/V1/V2/V3及BM25开发实验，冻结最终Hybrid＋BGE Rerank（750/150、final k=5），在原10题保留集与冻结V0算法/索引比较；本日不新增算法或修改问答API。

## 2. 新增和修改的文件

**新增：**

- [evals/final_selection.py](../../evals/final_selection.py)：重新核对五路dev原文评分、冻结/验证最终配置，拒绝覆盖和保留集后改参。
- [evals/holdout.py](../../evals/holdout.py)：独立保留集plan/collect/verify，复用既有指标、缓存、Qdrant、BM25/RRF、D15重排及D16诊断，不改变只接受dev的旧评测器。
- [final_v1.json](../../evals/configs/final_v1.json)：完整选型合同、来源SHA、原V0的Embedding/Prompt/Schema/生成参数。
- [tests/test_final_holdout.py](../../tests/test_final_holdout.py)：27项离线用例，fake模型/向量与内存Qdrant。
- [采集前选型报告](../../evals/reports/FINAL_SELECTION.md)、[保留集报告](../../evals/reports/FINAL_HOLDOUT.md)、本日报及[独立审计收据](../evidence/D17_HOLDOUT_AUDIT.json)。
- [公开保留集归档](../../evals/results/final-holdout-20261026-01/manifest.json)：23份封存产物＋manifest，共24文件/2,037,047字节。

**修改：** 根README、计划第10节、日志、学习/面试笔记及evals/README。无新依赖，无业务源码/API、旧评测器、旧冻结数据与历史归档改动。

## 3. 核心实现内容

1. freeze先验证各开发归档SHA并重算30题gold映射/指标，只在dev选型；固定最终5块，优先证据Recall/全部证据覆盖/MRR，V3另有本地组件资源预算。规则写于已有dev实验之后、holdout之前，不称预注册。
2. 选型文件包含原V0完整合同与唯一最终参数、来源run_id/指纹、资源及限制；拒绝覆盖、写入D8数据集、自改指纹及重封后的参数变化。
3. 保留集在冻结配置通过后才进入，问题批量Embedding一次，两侧共享同一向量和Dense查询；BM25/RRF后仅重排原融合候选，保留原text/metadata/RRF score及独立logit。
4. 独立开发题预热不进10题质量/时延；前两题执行smoke失败停止，之后失败保留空排名和完整分母，无自动重试或原序回退。Embedding失败仍记调用尝试。
5. 独立verify核对闭集SHA、索引/选型、10题完整顺序、BM25/RRF/logit/最终5块、指标/分类/原文案例/计时和调用预算，不启动模型。旧D9/D12/D16的dev约束保持。

## 4. 关键设计决策

D17只做选择与验收，不引入新算法或额外参数搜索。V1丢失固定top5证据，V3在相同top5覆盖下首条证据排序最好且已测组件成本可接受，选择Hybrid＋Rerank；同时保留dev @3退化，不推导全面提升。先提交推送配置，再进行真实holdout，保存clean执行commit作为时间顺序证据。

本轮只评检索，不调用聊天模型，Prompt/Schema原样冻结；Recall/MRR不能当回答准确率，1道无答案的拒答率仍null。Final总时延是共享候选组件之和，Embedding批次单列；API依旧Dense，实验配置冻结不冒充已部署在线优化。

## 5. 自动测试结果

执行者均为Codex；以下不登记为用户学习或亲自手测。新实现没有读取或打印原参考项目.env；真实客户端只读取新项目配置，不序列化密钥。

| 实际命令/核对 | 结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_final_holdout.py tests/test_rerank_run.py tests/test_eval_metrics.py tests/test_eval_comparison.py tests/test_fusion.py tests/test_bm25.py tests/test_hybrid.py` | 最终275 passed、21.42秒，含27项新增；离线fake模型/向量及内存Qdrant，覆盖冻结/同题向量/预算/失败分母/闭集与重封语义篡改；不把重复运行累加 |
| `python -m evals.final_selection freeze/verify --dataset evals/datasets/copilot_v0 --config evals/configs/final_v1.json`（实际解释器均.venv/bin/python） | 分别退出0；配置先冻结并提交e676d50，再采集保留集 |
| `.venv/bin/python -m evals.holdout plan --dataset evals/datasets/copilot_v0 --config evals/configs/final_v1.json` | 退出0，最多1次Embedding/10次Dense/11次本地评分110pair，model_loaded=false |
| `.venv/bin/python -m evals.holdout collect --dataset evals/datasets/copilot_v0 --config evals/configs/final_v1.json --output-dir data/evals/final-holdout-20261026-01 --live` | 退出0；实际Embedding API1、Dense10、GPU11次110pair、Chat0，无重试，10题错误0；离线模型缓存/实际环境前缀见日志 |
| `.venv/bin/python -m evals.holdout verify --dataset evals/datasets/copilot_v0 --output-dir data/evals/final-holdout-20261026-01` | 退出0，verified/23产物，不加载模型 |
| 同一verify使用`--output-dir evals/results/final-holdout-20261026-01` | 公开归档退出0，原字节与私有24文件一致；人工状态pending/day17_accepted=false保持 |
| 独立原文及缓存审查 | 800个指标逐值重算一致、10个缓存问题向量与SHA一致（hit10/miss0/API0）、11份GPU观察及11份执行源码SHA核对通过；未新增模型评分 |

用户已明确批准本轮最小真实验收。实际10问题1189 UTF-8字节，缓存miss10，一批Embedding约1008.739ms；按官方0.5元/百万tokens、3万token预算约¥0.015，实际usage及账单未采集，保持null。价格来源及限制见[保留集报告](../../evals/reports/FINAL_HOLDOUT.md)。

9可答/1无答案，Recall@5两侧均1；MRR@5由0.944444到1.000000，1题改善、8题指标不变，无退化，1无答案独立。033两条gold的完成名次[3,2]→[2,1]，@1只覆盖一条仍不能支撑整题。Final局部重排median164.558ms/p95375.744ms，共享候选组件总计median202.743ms/p951202.886ms；10题单轮的p95是样本最大值，不宣称生产P95或含Embedding/回答的端到端请求。

## 6. GitHub 提交信息

- 本日从main/origin/main一致、干净的D16收尾b3f4fcf开始，沿用`https://github.com/yan-ye-dhy/project-copilot.git`。
- 用户本次明确授权Codex完成GitHub提交，适用于D17本轮。采集前冻结提交：[e676d5021d84d0bcaa7eeb700287833d844e703a](https://github.com/yan-ye-dhy/project-copilot/commit/e676d5021d84d0bcaa7eeb700287833d844e703a)，已推送main；保留集manifest记录该commit、dirty=false。
- 结果与收尾文档提交/推送经diff审核后执行，真实最终ID和同步结果在会话补全，不在本文件预填自己的未来commit。

## 7. 后续学习提示

1. 沿dev重评分→选型冻结→Git证据→holdout共享向量→Dense/BM25/RRF/重排→gold指标→verify梳理全链。
2. 比较V1的覆盖损失、V2互补、V3首证据收益与@3退化，理解为何不能只挑最高单个数字。
3. 以033解释MRR=1为何不代表前1块覆盖全部证据；以038解释检索非空为何不等于问题可答。
4. 区分一次Embedding的整批耗时、共享候选组件和真正在线端到端，解释小样本p95及模型加载/显存统计边界。
5. 理解保留集只验收不选参、原冻结身份不覆写，以及后续可靠性修复应怎样建立独立版本。

## 8. 下一步计划

按本次授权完成结果与文档的Git提交推送，核实同步结果后交ChatGPT学习。用户主动请求D17手动验证后再按协作规则带练；本次未确认D17学习或手测完成。D18需用户明确开启，届时进行入库幂等与失败恢复，不自动进入下一日，也不依据本次保留集再调Prompt或检索参数。
