# 【Day 19 开发报告】

日期：2026-10-26。**D19实现、离线回归及8次单独授权的真实Prompt对照已完成。** Git按用户本次授权由Codex审核、提交推送，真实结果在会话补全。学习和学习后手动验证尚未确认，D20未开始。

## 1. 今日开发目标

区分空/弱证据、JSON/Schema/错引用与上游异常，验证中文来源和资源清理；固定新编证据比较冻结Zero-shot与一个Few-shot候选。保留D10/D17实验冻结规则，不增加检索算法、评分阈值、模型重试或新依赖。

## 2. 新增和修改的文件

**修改：**

- [rag/answering.py](../../src/project_copilot/rag/answering.py)：后端显式AnswerPrompt选项；默认v1不变；提取validate_model_answer，按JSON、Schema及编号分类脱敏错误。
- README、开发计划第10节、日志、学习/面试笔记和evals说明：更新D19交接、实验边界与当前状态。

**新增：**

- [evals/prompt_reliability.py](../../evals/prompt_reliability.py)：一个Few-shot候选，固定证据两侧对照；plan/offline/collect/verify、收费门禁、失败停止与独立归档。
- [5个新编样例](../../evals/fixtures/d19_reliability.json)：中文可答、弱证据、纯注入、事实混合注入与空证据；不从保留集选题或调整词句。
- [回答可靠性测试](../../tests/test_answer_reliability.py)、[实验器测试](../../tests/test_prompt_reliability.py)：真实回答器/ASGI合同、受控输出、门禁及归档拒绝。
- [Prompt实验报告](../../evals/reports/PROMPT_RELIABILITY_D19.md)及本日报。
- [自动审计](../evidence/D19_RELIABILITY_AUDIT.json)与[离线运行归档](../../evals/results/d19-offline-20261026-01/manifest.json)：只记录实际离线结果与执行版本，原4份产物逐字复制，不包含真实密钥/向量缓存。
- [真实对照审计](../evidence/D19_PROMPT_LIVE_AUDIT.json)与[真实运行归档](../../evals/results/d19-prompt-20261026-01/manifest.json)：单独授权后8次生成的原始消息/回答、usage、独立版本和Codex原文核对；学习/人工状态不自动改写。

## 3. 核心实现内容

默认调用仍是Dense检索→上下文筛选→v1 System/User→DeepSeek→严格JSON→ModelAnswer→本次证据编号→后端Citation。空证据不调用聊天模型，page=null合法，API沿用脱敏502/503及资源关闭，不增加响应字段。AnswerResponseError.reason区分invalid_json、invalid_schema、invalid_citation，日志不输出原始模型文本或私密错误。

实验通过后端参数显式选择候选，不允许请求体覆盖System；两侧同题证据/消息变量完全相同，只改变System模板。配置保存冻结合同、独立输入及执行身份，逐题保留失败，不自动修复、重发或删减分母；输出防覆盖，verify只读重放。

## 4. 关键设计决策

只读参考原项目`src/agent/routes/search.py:29`对page使用必填下标，以及`src/agent/backend/nodes/grading.py:39`在重试上限后仍进入生成的处理；来源审计见[REFERENCE_AUDIT](../REFERENCE_AUDIT.md)。新项目沿用已有可空page与空证据确定反馈，新增可靠性回归和独立Prompt对照；本次不移植评分LLM或改写循环。

冻结v1模板、User组织规则、ModelAnswer JSON Schema及指纹保持，Few-shot示例只放于独立候选的System；默认API不切换。合法引用不等于事实支持，测试明确展示结构合法的无依据答案不能靠Pydantic识别。上下文隔离、编号/Schema及后端权限各自限制行为，不承诺Prompt单独消除注入。

## 5. 自动测试结果

执行者为Codex，使用本项目.venv；ASGI测试在沙箱外执行，假模型/内存对象不请求真实外部服务。最终完整离线**1105 passed、1 skipped，68.28秒**，含36项D19新增。首轮相关92项和修订前全量1104项也通过，不累加重复测试次数；最终版补充了JSON转义攻击口号的检测。默认跳过项为显式启用的真实Qdrant HTTP集成，本轮不改存储，不将跳过计为通过。

实际命令：`HF_HUB_OFFLINE=1 PROJECT_COPILOT_RUN_QDRANT_INTEGRATION=0 timeout --signal=INT 180s .venv/bin/python -m pytest -q`。已运行offline/verify、缺--live和重复输出门禁；[公开离线归档](../../evals/results/d19-offline-20261026-01/manifest.json)的verify也通过。offline的8次假调用不是模型效果。218份受保护非Markdown文件、原冻结数据与历史结果保持，完整离线范围与源码指纹见[自动审计](../evidence/D19_RELIABILITY_AUDIT.json)。

随后用户明确同意本次最多8次真实生成，从干净03c2691执行`HF_HUB_OFFLINE=1 .venv/bin/python -m evals.prompt_reliability collect --live --output-dir data/auto-d19/d19-prompt-20261026-01`。实际8次DeepSeek、无Embedding/Qdrant/GPU/自动重试；4对非空模型输出与1对后端空证据结果全部保留，两版各5条合同/预期状态/口号检查符合。两侧状态探针5对不变，不表示语义普遍等效；Codex核对原文一致，用户人工支持复核仍pending，不自动计为用户手测。

真实4产物私有/公开逐字一致，分别verify退出0；完整消息、原始回答、实际模型别名deepseek-flash、usage和耗时见[真实审计](../evidence/D19_PROMPT_LIVE_AUDIT.json)。输入3318/输出257 tokens，缓存命中768/未命中2550；按[官方高峰价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)与观测缓存计数估算约¥0.00719，实付未采集，不把估算当账单。候选输入1297→2021（多724），本次状态探针无优势，默认v1保留，不重新选参或修改业务代码。

V0私有完整基线verify、V0公开12份产物字节及V1/V2/V3/D17公开verify通过，原Prompt和Schema指纹保持。第一次误将V0公开精简快照作为完整verify目录，缺manifest返回1；改用原private路径后verified，未修改工具或重签归档。

## 6. GitHub 提交信息

- 起点：[c6355934a79d85463eb46c7085638f337933e086](https://github.com/yan-ye-dhy/project-copilot/commit/c6355934a79d85463eb46c7085638f337933e086)，main与origin/main同步、工作区干净。
- 用户明确要求开启D19并提交GitHub，本次由Codex审核、提交和推送至现有origin/main；真实提交ID/推送结果在会话补全，不预填未来commit。
- 代码与离线证据已形成[03c269128562aa581b249eb115cfdf964708a33d](https://github.com/yan-ye-dhy/project-copilot/commit/03c269128562aa581b249eb115cfdf964708a33d)本地提交，真实对照从该干净版本执行；首次push因自动审批服务用量上限未执行，实际同步状态以会话核实为准。
- 新实验记录实际base commit、dirty状态及执行源码SHA；不追填未来提交身份，不修改D10/D17历史收据。

## 7. 后续学习提示

1. 跟踪回答执行链、三类校验与API脱敏；区分模型JSON模式、Schema和事实支持。
2. 对照Zero-shot/Few-shot的System差异和完全相同的User载荷，理解示例、变量注入与独立版本。
3. 解释无证据确定拒答、弱证据交由模型判断的边界，观察generation/usage为null与真实调用的区别。
4. 分析纯注入、事实混合注入、伪引用与转义口号；为什么合法编号不证明答案正确。
5. 区分受控假响应、真实小样本模型输出、人工事实复核和历史检索指标，解释不自动推广候选的原因。

## 8. 下一步计划

本次开发自动验收与真实Prompt对照已完成，完成授权Git同步后交ChatGPT学习。学习后由用户主动开启D19手动验证，核对原文支持与错误流程；本次不自动登记阶段B。D20需用户另行明确开启，按计划进入PostgreSQL工单基础，不自动推进。
