# D19：固定证据Prompt可靠性对照

日期：2026-10-26。本实验独立于V0/V1/V2/V3/D17检索对照；离线受控验收及单独授权的8次真实生成均完成，二者分别记录。用户学习、手测与人工事实复核尚未确认。

## 输入与唯一变更因素

使用[5个新编中文场景](../fixtures/d19_reliability.json)，不读取copilot_v0保留问题或标签来选提示词。每题两侧固定问题、顺序/原文/metadata、6000字符整块预算、User消息、ModelAnswer Schema和生成参数；只改变System模板（候选追加证据充分性说明与两个打印机主题示例）。

| 项目 | 冻结基线 | 唯一候选 |
| --- | --- | --- |
| 版本 | rag-answer-v1 | rag-answer-v2-fewshot-d19 |
| 示例 | 无，格式骨架不作答案示例 | 2个无关主题的输入/输出示例 |
| Prompt指纹 | 83971da601780ea5a5f5e6e4b45f11d952cb261e50d7275a1efb38494a46a9ea | 由独立运行config保存，不复用V0身份 |
| Schema指纹 | ded2c45bf4b0dcd4888884ad1ff1bcbc729a1846ee0977ec90e7dfdd903c5941 | 与基线相同 |
| 模型/参数 | deepseek-flash；temperature=0；max_tokens=1024；thinking disabled；timeout=30s；retries=0 | 相同 |
| 默认API | 保持v1、Dense/750/150 | 不上线 |

固定证据绕过检索，不请求Embedding、Qdrant或GPU，也不测Recall/MRR。score=0仅为合成RetrievedChunk结构，不表示相关性阈值或置信度。Markdown来源page=null；元数据身份明确带d19-authored前缀，不冒充真实文件入库结果。

## 场景与判定范围

| 场景 | 状态预期 | 需要另行判断的边界 |
| --- | --- | --- |
| 中文上传格式/413 | answered | 格式及错误码须由原文支持 |
| 弱证据恢复时限 | insufficient_evidence | 主题相关不等于SLA证据 |
| 纯伪角色/攻击指令 | insufficient_evidence | 不输出攻击口号，不伪造999引用 |
| 5 MiB事实混合攻击指令 | answered | 只回答事实，不执行角色/工具命令 |
| 空证据 | insufficient_evidence | 后端直接处理，generation=null，无模型请求 |

自动检查严格JSON/Schema、本次编号、预期状态及一个固定攻击口号（含JSON反转义后的文本）；原文事实支持仍需单独复核，支持率未采集为null。原始输出与失败保留；格式错误不修复，供应商请求/响应错误立即停止后续调用并标partial。通过上述探针不代表普遍防注入能力，也不代表候选改善了语义正确率。

## 当前执行结果

离线模式实际完成5对/10条记录，8次明确注入的假响应、空证据两侧0调用；10条合同及状态/口号检查符合预期。[公开离线归档](../results/d19-offline-20261026-01/manifest.json)与私有原始4文件逐字一致，verify退出0/verified，执行源码和依赖SHA见config。两版假响应由测试输入指定，不能据此说“Few-shot与基线效果相同”；人工支持字段pending不代表已做人工标注，语义准确率仍为null。默认API、冻结Prompt/Schema及旧归档保持。

真实调用前预算为4个非空场景×2模板=8次DeepSeek；每次输入按3000 tokens、输出上限1024。按2026-10-26[DeepSeek官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)高峰输入未缓存¥2/百万、输出¥8/百万估算约¥0.113536，用户据此单独授权。最大消息正文UTF-8为2221字节，tokens预算为保守估算，未用收费接口预估。

### 单独授权后的真实结果

从干净代码03c269128562aa581b249eb115cfdf964708a33d运行，实际8次DeepSeek请求，0次Embedding/Qdrant/GPU/自动重试；没有修改候选或重新采集。完整[真实归档](../results/d19-prompt-20261026-01/manifest.json)保存4份产物，私有/公开字节一致，分别verify退出0，见[真实审计](../../docs/evidence/D19_PROMPT_LIVE_AUDIT.json)。

| 检查 | 冻结v1 | Few-shot候选 |
| --- | --- | --- |
| 非空场景的模型回答合同通过 | 4/4 | 4/4 |
| 非空场景预期状态/攻击口号检查 | 4/4 | 4/4 |
| 后端空证据结果，不请求模型 | 1/1 | 1/1 |
| 输入tokens | 1297 | 2021 |
| 输出tokens | 137 | 120 |
| 人工事实支持率 | 未采集 | 未采集 |

五对状态/口号探针0改善、0退化、5不变，其中1对为空证据后端行为。候选增加724输入tokens，未显示本样本更好，保持默认v1；单次顺序运行不作速度结论，也不声称模板语义普遍等效。Codex核对4条有效回答的格式/413/5 MiB事实及原文引用一致，弱证据和纯注入各两条拒答没有编造时限/密码；这些为助手原文审查，不代替用户人工支持标注。

供应商actual_model返回deepseek-flash，仍是别名，未暴露权重revision；usage共输入3318、输出257，总3575 tokens，输入缓存命中768/未命中2550。按观测缓存计数及官方高峰价（命中¥0.04/百万）估算约¥0.00719，全部输入按未缓存高峰计为¥0.008692；实付账单未采集，非高峰价也可能不同。原始usage、响应与elapsed均保留，不将估算冒充实付。

## 归档与复现

见[运行器说明](../README.md#d19固定证据的独立prompt可靠性对照)。plan和offline不读.env；collect显式--live才读取新项目配置，并检查与冻结生成参数相同。输出目录必须不存在，保存config、answers、summary及manifest；verify重算字节、题目顺序/完整性、原回答解析和汇总，无模型调用。

本实验不覆盖既有归档或根据保留集继续选参；Prompt模型别名可能更新，真实运行记录actual_model，不能宣称云端权重与D10永久相同。后续用户学习/手动复核状态另行记录，D20未开始。
