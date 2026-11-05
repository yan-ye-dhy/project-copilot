# 云端静态维护预备包与回退边界

本轮只构建本机预备产物，**不上传、不部署、不改控制台、不推main**。现有新中式前端与正式 `node scripts/build_edgeone.mjs` 不变。CLOSED/停Tunnel只关后端，不能撤销已经触发的云函数调用。

## 官方能力核实（2026-11-05）

| 操作 | 已查到的官方依据 | 不能据此推断 |
| --- | --- | --- |
| 历史重新部署 | [触发部署](https://pages.edgeone.ai/zh/document/create-deploys) 支持选择某条部署重新构建 | 不保证瞬间切流、沿用当时全部环境值或不产生新构建；须看实际部署配置和结果 |
| 失效/旧产物 | [管理部署](https://pages.edgeone.ai/zh/document/manage-deploys) 说明成功记录超过3条可能清理较早产物，可重新部署恢复访问 | 不能保证旧URL和产物永久可用；本机留审核源码及构建包，勿依赖无限回滚 |
| 删除单个部署 | [错误码](https://pages.edgeone.ai/zh/document/error-codes) 说明已删除部署的部署域名返回410且不可恢复 | 不证明固定项目域名及所有别名/地区函数即时停用。本轮不删除 |
| 删除项目 | [项目管理](https://pages.edgeone.ai/zh/document/project-management) 明确不可恢复 | 不能作为日常止损操作，不执行；断联Git仅影响关联，不证明旧部署停止 |
| 关闭自动部署 | [构建指南](https://pages.edgeone.ai/zh/document/build-guide) 描述环境管理中的自动部署开关；环境变量变更仅作用于新部署 | 不关闭现有路由、不取消已在运行的构建/请求；生产及预览、钩子/手动部署须分别核实 |
| 独立静态上传 | [直接上传](https://pages.edgeone.ai/zh/document/direct-upload) 要求上传根index.html；根函数目录用于Functions，配置仅支持headers/rewrites/redirects | 新建维护项目不会关闭原项目。Git项目现有控制台能否用静态上传替换须用户实证，不能假定能切换项目类型 |
| 同项目部署包 | 官方 [Deployment SDK](https://pages.edgeone.ai/zh/document/deployment) 接受projectId、Production/Preview及artifact目录/ZIP | 仅研究接口，不安装/调用SDK；文档未明确承诺“不含函数的新版本会卸载全部旧API/旧部署别名” |
| 一键回滚/指向历史/禁用函数路由 | 上述文档未找到足以确认当前账号可用的原子停用保证 | 全部标NEEDS_MANUAL_CHECK；不用未核实按钮名、rewrite覆盖或空cloudFunctions假装函数已停 |

官方能力描述不等于该用户当前控制台验收。没有官方可核实的瞬时全站停函数保证；**调用前额度缺口仍存在**。

## 本机准备（没有云端副作用）

```bash
# 必须指定全新路径；不覆盖已有文件/目录，不删除已有产物。
node scripts/build_maintenance.mjs --output-dir /tmp/pc-maintenance-candidate-01
```

输出为7个文件：`public/index.html`、`public/assets/maintenance.css`、3个原有被动SVG、`public/edgeone.json`及包根 `artifact-manifest.json`。只把 **public里的内容作为上传根**，index.html须在根；manifest留本地审核，不上传仓库、私有配置或工作单。

页面仅显示暂停服务，沿用竹/山水和墨绿米白；无登录、脚本、表单、业务API、外部资源、Cloud Function、server key或后端地址。独立edgeone配置只有安全headers；CSP禁止script/connect。构建不读环境变量，不修改正式dist/根edgeone.json，拒现有输出和symlink，不自动执行平台命令。源SVG如被改为可执行/外链内容会拒绝。

manifest只证明静态文件摘要及 `LOCAL_ARTIFACT_ONLY`，明确 `remote_route_removal=NEEDS_MANUAL_CHECK`；它不是STAGING证据、风险接受或远端停用证明。**仅更改主仓库构建命令/输出目录并不足够**：根 `cloud-functions` 仍可能被Makers发现。必须审查独立包/独立部署根，不能把源码根上传为“纯静态”。

## 待用户许可的云端演练步骤

以下每项都为 **REQUIRES_EXPLICIT_USER_AUTHORIZATION**，本轮不执行：

1. **先准备退路**：记录当前固定域名、生产实际commit、构建/根/输出配置和变量名称（不记录值）、部署ID（只本地）；保留已审核源码/静态包。确认CLOSED/禁Worker/无Tunnel/BACKEND未配置。本人检查生产与预览自动部署、部署钩子和进行中构建；经授权关闭可能恢复业务的触发源，核实无旧构建排队。仅断Git不是停服务。
2. **确认当前项目可用路径**：先向实际控制台/官方支持确认如何把独立static包发布到**同一项目的生产域名**。若Git项目只能从源码构建，另审一个完全无cloud-functions的部署根/专用维护分支及平台构建规则；本轮没有创建这类生产分支或修改项目配置。若只能新建项目，原项目不会停，停止宣称止损可用。SDK方案也必须单独审核凭据、项目和Production目标，不能复制猜测CLI。
3. **先做受控预览（若获授权）**：在同项目可行的非生产环境部署仅public包，检查部署产物/函数清单。此操作本身产生平台请求/构建/流量，可能有旧路由，不能保证零额度；预览通过也不证明生产和历史别名已停。
4. **获单独生产替换许可后部署**：确认目标项目/环境/包摘要，观察Success和固定项目域名实际返回维护页面；失败或超时继续认为旧版可能运行。本地等待超时不代表平台取消部署，不自动反复发布。
5. **验证函数是否真的移除**：先记录用量统计UTC时间及统计延迟，等待安静区间；用浏览器Network确认首页只请求HTML/CSS/SVG，无任何API。再各一次受控GET health/me及非法PATCH，记录脱敏响应/trace标识（不猜必须404）。检查当前生产函数清单、相应请求窗口的函数日志与次数；对仍有效的旧部署URL/别名也核实。如果仍见原proxy JSON、函数日志/次数增加或无法归因，标FAILED/UNKNOWN。首页静态/响应404/计数暂未变化单独都不足以证明停止函数。
6. **成功/失败判定**：只在当前部署无函数且少量受控请求未触发函数、历史别名也有确定处置及监控延迟已理解后，记录有限范围真实证据；仍不承诺全球瞬时生效或恶意流量无成本。若失败保持CLOSED/无Tunnel，保留部署诊断和用量，不删项目/旧部署、不猜rewrite或降认证；联系平台确认可靠停用方式。当前仍无经过实际验证的云函数额度紧急硬闸。

## 恢复原版本

恢复任一步都是 **REQUIRES_EXPLICIT_USER_AUTHORIZATION**。先核对未决running/unknown、供应商结果及额度，再审查保存的源码和当前变量；由用户明确选择可用历史记录重新部署或固定审核commit构建。不要盲目“重新部署最新”以免拉入未审版本，不保证平台失效产物仍存在。

先保持CLOSED/无上游恢复业务页面，验证健康/me503、非法方法404、错误来源403及当前部署提交。确认稳定后再由用户决定恢复自动部署；真正STAGING、Worker、Tunnel仍各走 [交接手册](STAGING_LAUNCH_HANDOFF.md) 的单独授权。恢复页面不自动恢复业务授权，维护页不处理已经提交的供应商请求。
