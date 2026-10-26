# 开发日志

这里只记录实际实施和验证；任务顺序见 [开发计划](DEVELOPMENT_PLAN.md)，原项目审计保留在 [参考审计](REFERENCE_AUDIT.md)。

## D1：独立环境与最小API（2026-10-10）

**最终状态：D1阶段A/B均完成。** 2026-10-11用户完成本轮手测并确认学习理解；2026-10-12只读核实用户提交的 `4c04a29` 已同步GitHub，工作区当时干净。下文保留各次实施/交接时的历史状态，不将生成时的pending视为最新状态；用户实际学习耗时尚未登记。

### 目标与实际改动

建立新项目独立Git仓库、Python环境与可启动入口。参考原项目 `src/agent/api.py` 的应用入口和 `src/agent/utils/config.py` 的Settings模式；新代码自行精简设计，不直接导入原模块。

| 文件 | 实际内容 |
| --- | --- |
| `pyproject.toml`、`.python-version`、`uv.lock` | 最小依赖、Python3.13约束、实际解析的依赖锁定 |
| `.gitignore`、`.env.example` | 排除私密配置/虚拟环境/数据；提供两个无密钥配置示例 |
| `src/project_copilot/__init__.py`、`config.py` | 独立包；明确新项目dotenv路径，名称与环境取值校验 |
| `src/project_copilot/main.py` | `create_app`、启动时加载配置的lifespan、`GET /health` |
| `README.md`、本日志、学习与面试笔记 | 启动步骤、调用链、自主练习、实际验证与回答依据 |
| `docs/DEVELOPMENT_PLAN.md`、`REFERENCE_AUDIT.md` | 更新D1进度和历史审计引用，保留原阶段及验收约束 |

`.venv`独立创建在新项目根目录，未复用原环境的site-packages。两个项目可以使用同一份已安装的Python3.13.14解释器；依赖安装位置仍各自独立。uv版本为0.11.32。实际安装FastAPI0.138.2、Pydantic2.14.0、pydantic-settings2.15.0、Uvicorn0.49.0，其余解析结果见 `uv.lock`。

### 实际命令、预期与结果

工作目录均为 `/home/yanye/project-copilot`。受保护Git目录的初始化与本地服务监听通过本次环境的权限审核后执行；以下是命令本身。

| 命令/检查 | 目标与预期 | 实际结果 |
| --- | --- | --- |
| `git init --initial-branch=main`；`git rev-parse --show-toplevel` | 新项目具有独立仓库 | 初始化成功；顶层为 `/home/yanye/project-copilot` |
| `uv sync --offline --link-mode copy --python /home/yanye/.local/share/uv/python/cpython-3.13-linux-x86_64-gnu/bin/python3.13` | 尝试利用本机缓存安装 | 失败：缓存缺少FastAPI解析所需信息；未把失败记为安装成功 |
| `UV_CACHE_DIR=/tmp/project-copilot-d1-uv-cache UV_PYTHON_DOWNLOADS=never uv sync --link-mode copy --python /home/yanye/.local/share/uv/python/cpython-3.13-linux-x86_64-gnu/bin/python3.13` | 从包源解析并安装最小依赖，不下载解释器 | 成功：解析并安装16个包；生成 `uv.lock` |
| `.venv/bin/python - <<'PY' … PY`（一次性内联检查，下表列明范围） | 离线验证配置与启动行为 | 9组全部通过，无模型或数据库调用 |
| `.venv/bin/python -m uvicorn project_copilot.main:app --host 127.0.0.1 --port 8002`；`curl --fail --silent --show-error --include http://127.0.0.1:8002/health` | 真启动，健康接口HTTP200且符合默认返回 | HTTP200，响应见下；随后正常关闭验证服务 |
| 在受控子进程设置 `PROJECT_COPILOT_APP_NAME=Copilot Learning` 后启动同一Uvicorn命令 | 验证进程环境变量能改变响应；OpenAPI只含健康业务接口 | HTTP200且service变为 `Copilot Learning`；OpenAPI paths只有 `/health`；子进程自动停止 |
| `UV_CACHE_DIR=/tmp/project-copilot-d1-uv-cache UV_PYTHON_DOWNLOADS=never uv sync --locked --offline --check` | 现有环境与锁定配置一致 | `Checked 16 packages`；`Would make no changes` |

默认健康请求的实际响应：

```text
HTTP/1.1 200 OK
{"status":"ok","service":"Project Copilot","environment":"development"}
```

内联离线检查使用新环境的Python、`unittest.mock`、临时dotenv文件、Pydantic校验和手动进入ASGI生命周期，不新增测试框架依赖：

| 组 | 检查内容 | 结果 |
| --- | --- | --- |
| 1 | dotenv绝对路径属于新项目 | PASS |
| 2 | 禁用dotenv读取时默认值正确 | PASS |
| 3 | 显式临时dotenv能够加载 | PASS |
| 4 | 构造参数高于进程环境变量，进程环境变量高于dotenv，dotenv高于默认值 | PASS |
| 5 | 将当前目录改为含另一份 `.env` 的临时目录，不误读该文件 | PASS |
| 6 | 空名称、全空格名称、超过80字符的名称触发校验错误 | PASS |
| 7 | `environment=staging` 触发校验错误 | PASS |
| 8 | 用会抛错的替身替换Settings构造函数，再导入入口；导入成功且未创建配置实例 | PASS |
| 9 | lifespan可使用注入配置；非法环境配置在启动阶段失败 | PASS |

原始汇总输出：`Total: 9 offline verification groups; no model or database calls.` HTTP配置检查输出：`PASS HTTP 200 environment override`、`PASS OpenAPI: the only business endpoint is GET /health`。

结束前与任务开始时快照比较：原仓库136个tracked或未被忽略的未追踪文件SHA256全部一致，`git status --porcelain=v1`也一致。新项目README与5份docs的本地链接目标均存在，代码围栏闭合；`.env`、`.venv`和缓存不在Git提交候选中。

### 问题与处理

1. 阶段0中新项目不是有效Git仓库。今天在授权的新项目范围内，经环境审核成功初始化；原仓库历史未整理或提交。
2. 首次离线安装缺少解析缓存。改用联网包安装，保留失败记录；没有调用模型API。
3. 默认沙箱中Uvicorn完成应用启动，却无法绑定本机8002，提示 `could not bind on any address`。审核通过后确认端口未占用，再启动并实际请求成功；这是运行权限问题，未为此修改应用逻辑。

### 学习与提交

新概念和真实调用链见 [D1学习笔记](STUDY_NOTES.md#d1独立工程入口与配置)，对应3个回答思路见 [D1面试笔记](INTERVIEW_NOTES.md#d1工程入口与配置)。2026-10-11用户完成本轮手测，并明确确认已完成ChatGPT学习、能解释相关流程，B阶段据此登记完成；不是依据助手验收或一次请求推测掌握。

Git历史：首次工程提交为 `8c2de0c chore(project): initialize isolated project skeleton`；截至规则适配前，另有两条教学文档提交。此前未推送，且新项目尚无remote。2026-10-11起按新规范授权常规推送，仍需先创建并关联实际GitHub仓库，不把本地commit当成已同步。

本日未验证RAG、模型供应商、Qdrant、PostgreSQL、Docker或Phoenix。后续任务为D2：阅读原智谱适配，先做离线请求契约，再做最小真实Embedding验证；收费批量调用遵守计划第8.3节。

### 教学方式调整（2026-10-10）

用户指出前次缺少完整设计说明、逐段代码讲解、陌生语法与Uvicorn操作教学，单报9组检查结果不足以形成理解。本次停在D1，补 [完整导读](D1_WALKTHROUGH.md)，提供同范围9组检查的完整教学脚本及逐步HTTP复现方式；原表省略号仍代表前次一次性命令，不冒充当时已保存脚本。后续默认助手写代码并带读，用户以定位调用链、理解行为及运行反馈确认学习，手工编写业务代码可选。本次不改业务代码、不重新执行服务验收；用户理解与操作仍待实际反馈。文档检查通过：7份Markdown本地链接及围栏、教学脚本AST语法、Git空白检查；原仓库136个文件校验值及Git状态不变。

### 完整运行链补讲（2026-10-11）

用户已展示自己执行curl的截图：健康请求HTTP200，正文为默认status/service/environment。本记录依据用户截图，只确认该次请求复现，不登记全部D1学习完成。

用户指出逐行解释后仍缺终端命令到代码执行的整体联系。本次在 [D1导读](D1_WALKTHROUGH.md#从终端命令到实际响应把两个文件连起来) 加入时序图和分阶段运行链：uv环境准备、模块导入、应用构建/路由注册、lifespan配置加载、curl请求处理/JSON响应、对象引用联系与正常关闭。结合当前main/config和已安装框架源码核对执行顺序；仅修改教学文档及协作约定，没有重启或关闭用户服务，没有运行模型API或重新执行服务测试。

### 三方协作规则适配（2026-10-11）

按用户新指令建立根AGENTS入口与 [COLLABORATION_RULES.md](COLLABORATION_RULES.md)，同步计划、README和教学材料：Codex负责开发/自动验收/提交推送/8项报告，ChatGPT负责代码与架构教学，用户学习后主动开启手动测试；Codex一次带练一个环节并等待反馈。保留35日技术任务、费用规则、评测与业务安全约束。

适配开始时核实本地main分支、已有提交和干净工作区；remote未配置。用户说明尚无GitHub仓库并要求指导创建，已选择Public公开代码与文档；实际远端地址等待网页建仓后提供。此次没有开展D2、修改业务代码、自动开启用户手测或运行模型调用；历史自动结果和用户截图证据保持原意。

文档验证：9份Markdown围栏闭合，40个本地链接目标存在，固定8项报告完整，`git diff --check`通过；业务源码、依赖声明和锁文件没有改动。规则复核确认原计划的架构、评测、费用和核心验收章节保留。

### GitHub首次同步（2026-10-11）

用户创建并提供 [yan-ye-dhy/project-copilot](https://github.com/yan-ye-dhy/project-copilot)。只读查询确认Public、默认main，首次`git ls-remote`没有refs；审查原有4条提交的跟踪文件，未发现真实凭据或私密运行数据。

执行`git remote add origin https://github.com/yan-ye-dhy/project-copilot.git`。首次非交互push失败：Git未接入已登录的GitHub CLI，报`could not read Username`；在本项目配置凭据助手后，重试`git push -u origin main`成功，main开始跟踪origin/main。远端main已核对为`568ee738cf1b089872a1f0113944cd8788d6de1e`，与首次推送时的本地HEAD一致；本节和相关进度更新另作正常提交并同步。

本次属于仓库准备及进度记录，未新增业务功能、重跑历史服务验收或开启用户手动测试；D1固定开发报告和完整B阶段仍待完成。

### Git操作方式调整（2026-10-11）

用户要求后续提交GitHub的步骤改为本人在Codex指导下操作。同步协作入口、规范、README与计划：Codex保留实现、自动验证、diff审核和报告职责；用户亲自执行暂存、commit及push，Codex逐步解释、等待反馈并核实结果。此前已完成的首次同步记录保留，本次规则修改不自动暂存、提交或推送。

报告在用户Git操作前注明待提交/推送，核实真实结果后补全。Git交接指导与学习后主动开启的功能手动测试分别进行，A/B和下一Day门槛保持。本次只改文档，不开展新Day开发。

### 用户手动验收与学习确认（2026-10-11）

用户主动发送“开始D1的手动测试”，随后本人在 `/home/yanye/project-copilot` 的终端执行启动、请求与关闭；Codex逐步指导并依据截图/粘贴输出确认，未代运行服务或HTTP请求。基准复现写法为：

```bash
uv run --frozen uvicorn project_copilot.main:app --host 127.0.0.1 --port 8002
# 另一个终端
curl --noproxy 127.0.0.1 --fail --include http://127.0.0.1:8002/health
```

下表依据用户实际响应与日志登记；部分粘贴的curl命令首行有重复片段，以上保留规范复现写法，不把重复片段当成已确认执行的参数。只读检查未发现可能由误输入产生的 `url` 文件。

| 场景 | 用户输入或观察 | 实际结果 | 判定 |
| --- | --- | --- | --- |
| 正常启动与默认健康请求 | 基准启动；PID137008；GET /health | 启动完成并监听8002；HTTP200，status=ok、service=Project Copilot、environment=development，访问日志200 | 通过 |
| 正常关闭 | 在服务终端按Ctrl+C | Application shutdown complete；PID137008结束，返回命令提示符 | 通过 |
| 服务名配置变化 | 启动命令前加 `PROJECT_COPILOT_APP_NAME="Copilot Learning"`；PID138765；再次GET | HTTP200，service变为Copilot Learning，environment仍为development，访问日志200 | 通过 |
| 空白名称校验 | 启动命令前加 `PROJECT_COPILOT_APP_NAME="   "`，值为三个空格 | app_name报string_too_short；堆栈为lifespan→load_settings→Settings；启动失败并退出 | 按预期拒绝，通过 |
| 环境枚举校验 | 启动命令前加 `PROJECT_COPILOT_ENVIRONMENT=staging` | environment报literal_error，列出development/test/production；启动失败并退出 | 按预期拒绝，通过 |
| 恢复与最终关闭 | 不带临时变量前缀再次启动；PID140765；GET；最后Ctrl+C | HTTP200，恢复Project Copilot/development，访问日志200；Application shutdown complete，PID140765结束并返回提示符 | 通过 |

用户最后明确反馈“已理解，能解释这些流程”，确认已完成ChatGPT的D1学习，能解释启动时加载配置、health读取配置与非法配置的启动阶段拦截；结合上述手测证据，阶段B完成。此次手测没有重新验证完整构造参数/进程环境/dotenv优先级、跨目录隔离、超长名称或导入无副作用，这些仍引用2026-10-10助手检查，不计作用户新验收。

补交固定8项 [D1开发报告](reports/DAY_1.md)，更新计划和学习记录。此前9组自动检查及服务验收本轮未重跑；本轮只整理文档，业务源码、依赖和配置模板没有改动。报告与此前Git规则修改仍未暂存、提交或推送；核实已同步main仍为 `ec887b157251ace725fb103d2f318dcb5b2e3da0`。下一步由用户亲自完成Git交接；不自动开始D2。

收尾文档检查通过：10份Markdown围栏闭合，47处本地链接目标存在，D1报告8项完整，`git diff --check`通过；只读核实索引无暂存改动，业务源码、依赖与配置模板diff为空。

## D2：智谱Embedding最小契约（2026-10-12）

**开发期快照（D2提交前）：客户端、68项离线验证及一次2048维真实smoke完成。** 当时新改动未暂存/提交/推送；用户学习和手测未开始。最新交接与用户手测见本节末尾，不自动推进D3。

用户主动开启D2前，已核实D1本地及GitHub main均为 `4c04a29d0dff34c19660958ecd2111d1cafe2d3b`，D1交接完成。参考原智谱分支及其已安装SDK实现，保留原文本/float/batch64，使用新项目的OpenAI SDK适配以检查原始响应，避免SDK转换掩盖bool/字符串等错误。

新增infrastructure包/models.py、单条smoke脚本及三份测试；修改Settings、脱敏配置模板、依赖声明/锁文件与过程文档。main.py与/health业务代码未改。key为可选SecretStr，仅创建模型客户端时要求；模型URL/timeout与batch有校验，客户端不在导入时创建。

| 实际命令/检查 | 执行者与结果 |
| --- | --- |
| `UV_CACHE_DIR=/tmp/project-copilot-d2-uv-cache UV_PYTHON_DOWNLOADS=never uv sync --link-mode copy` | Codex，新项目独立安装成功：解析30包、安装14包；OpenAI2.54.0、HTTPX0.28.1、pytest8.4.2；未修改原项目环境 |
| 首轮 `.venv/bin/python -m pytest -q` | Codex，仅收到部分输出后中断，未计通过 |
| `timeout 45s .venv/bin/python -m pytest -q tests/test_models.py tests/test_embedding_smoke.py` | Codex，63 passed in 1.91s；全为假HTTP传输/向量及脚本默认保护 |
| `timeout 25s .venv/bin/python -m pytest -vv -s -o faulthandler_timeout=10 tests/test_main.py` | Codex，TestClient停在后台portal线程等待，25秒后退出124；保留诊断失败，不修改业务入口或跳过回归 |
| 改为显式lifespan+同进程ASGITransport后，`timeout 45s .venv/bin/python -m pytest -q` | Codex，68 passed in 1.35s；同时验证无key时健康响应和禁止模型客户端构造 |
| `OPENAI_LOG= timeout 35s .venv/bin/python scripts/embedding_smoke.py --live` | Codex，用户保存新项目key后单条真实请求成功；返回embedding-3、1条、2048维，float/index/有限数值校验通过 |
| `UV_CACHE_DIR=/tmp/project-copilot-d2-uv-cache UV_PYTHON_DOWNLOADS=never uv sync --locked --offline --check` | Codex，Resolved 30 packages、Checked 29 packages、Would make no changes；环境与锁文件一致 |

响应契约覆盖中文原文、64+1批次、输出顺序、条数/维度/数值、畸形JSON、错误状态、超时与脱敏；SDKmax_retries=0。测试假key与3维向量不是供应商实测。CLI无--live时退出2；测试中的--live缺key分支退出1且不创建HTTP客户端。

官方Markdown资料已只读核实：当前API最多64条/批、每条3072 tokens、默认2048维，标准价0.5元/百万tokens；来源见 [D2报告](reports/DAY_2.md)。encoding_format=float沿用原项目兼容参数，官方当前schema未列此字段；本次包含该参数的单条请求已成功。timeout20秒是本项目网络操作超时，不是整个任务总时限。

用户表示会配置新项目密钥。检查当时新项目没有.env，也没有对应进程变量；随后仅用新项目.env.example生成本地空key模板，并核实.env被Git忽略。之后仅检查新项目key是否可用、URL是否为官方端点；用户保存配置后完成上表中的一次真实请求。未读取或复制旧项目真实.env，未打印密钥或完整向量；费用按实际账号用量，未读取账单或编造金额。

真实命令的实际摘要如下，不以Mock结果代替：

```json
{"status": "ok", "model": "embedding-3", "texts": 1, "dimensions": 2048, "encoding_format": "float"}
```

SDK自动重试关闭，本轮未执行批量真实请求；with已关闭该模型HTTP客户端。本段写于提交前，当时阶段A待用户Git交接、阶段B待学习与主动手测；最终状态见下方补记和 [D2报告](reports/DAY_2.md)。

最终检查：11份Markdown、51处本地链接与报告8项通过，9份Python文件AST可解析，`git diff --check`通过。扫描17个拟提交变更文件未发现真实key；.env被忽略、未跟踪，索引无暂存改动。Codex未执行本批Git暂存、commit或push。

### 用户Git与手动验收补记（2026-10-12）

用户亲自暂存并核对17个文件，提交 `e63d310cfbfbcb81d41ce448412ab50d7dee3f06`（`feat(models): add validated Zhipu embedding client`），执行 `git push origin main` 成功，随后 `git status --short --branch` 仅显示 `## main...origin/main`。Codex只读核实本地HEAD和干净状态；阶段A完成，报告首版的“待Git交接”是提交前快照。

| 用户亲自执行 | 实际反馈 | 验证点 |
| --- | --- | --- |
| `uv run --frozen python scripts/embedding_smoke.py`，随后 `echo $?` | 提示必须显式添加`--live`；退出码2 | 默认保护，未进入模型请求 |
| `uv run --frozen python scripts/embedding_smoke.py --live`，随后 `echo $?` | `{"status": "ok", "model": "embedding-3", "texts": 1, "dimensions": 2048, "encoding_format": "float"}`；退出码0 | 用户自己的单条真实请求及2048维校验，独立于Codex此前的真实smoke |
| `PROJECT_COPILOT_ZHIPU_API_KEY="   " uv run --frozen python scripts/embedding_smoke.py --live`，随后 `echo $?` | `{"status": "error", "message": "请在新项目配置有效的PROJECT_COPILOT_ZHIPU_API_KEY"}`；退出码1 | 临时环境变量覆盖`.env`，客户端在请求前拒绝空白密钥 |

三项用户手测符合预期；用户明确确认已完成ChatGPT学习，能说明主要调用链及空白密钥在请求前被拒绝的原因，阶段B完成。D2阶段A/B均完成；用户未要求D3，故不启动下一日开发。

## D3：文档读取与可检查的片段（2026-10-12）

**开发期快照（提交前）：开发与离线自动验证完成，阶段A待用户Git交接，阶段B待ChatGPT学习与主动手测。** 用户明确开启D3；只读核实开始时HEAD及origin/main为D2补记提交`1602f5fdb0028b2525fb8b33d07758d0cbc1ef2c`，工作区干净。D2开发提交`e63d310`和用户验收补记均已由用户推送。

**最新状态（2026-10-13）：D3阶段A/B均完成。** 开发提交与Git核实见本节末尾；用户七项手测及本人学习确认见下方“D3用户手动验收与学习确认”。下文保留历史检查和提交前快照。

新增`schemas/documents.py`、`rag/loaders.py`、`rag/splitters.py`及包入口；新增`scripts/inspect_document.py`、两份测试、三个自编TXT/MD/PDF样本与来源说明。显式依赖为`langchain-text-splitters`和`pypdfium2`，实际锁定1.1.3与5.14.0；transitive langchain-core为1.6.9。main、配置和D2模型客户端未修改。

参考原`embedding_management.py:39–60`的读取、递归切分和metadata流程。独立试装原社区loader时出现弃用提示，核实[官方停止维护说明](https://github.com/langchain-ai/langchain-community/issues/674)后移除`langchain-community`及不再需要的依赖。最终TXT/MD对原字节严格UTF-8解码，PDF复用同款PDFium解析器；自写统一契约，未复制完整旧服务或导入旧包。递归切分仍使用成熟组件，按新计划750/150和最终字符回退。

文档身份来自同次读取的原字节SHA-256，与文件名分离；chunk UUID包含文档身份、切分指纹、真实页索引、全局片段序号和文本指纹。`index_version`当前仅标识切分，未声称完整模型/向量索引版本。PDF解析串行锁遵守原生库线程约束，显式关闭文档/页/文本句柄；PDF换行规范为LF，空白页保留原编号。TXT/MD页码为None，空文档返回无片段；没有OCR。

| 实际检查 | 执行者与结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_documents.py` | Codex，27 passed in 0.12s；真实三格式读取、PDF空页/页码、中文回退、稳定身份、空结果和错误 |
| `timeout 45s .venv/bin/python -m pytest -q` | Codex，101 passed in 2.63s；包含原68项与新增33项，脚本JSON/错误/离线保护/跨进程稳定性均通过 |
| 同一读取/切分API核对三个合成样本 | Codex，TXT 6片段最大750字符；MD 5片段最大750字符；PDF保留页0/1/2，仅页0和2产生片段 |
| `UV_CACHE_DIR=/tmp/project-copilot-d3-uv-cache UV_PYTHON_DOWNLOADS=never uv sync --locked --offline --check` | Codex，Resolved 52 packages、Checked 50 packages、Would make no changes |

本日验证全部使用本地自编文件；未读取真实.env内容、发模型请求、连接Qdrant或启动业务API。自动结果不登记为用户学习/手测。固定8项交接与学习关注点见 [D3报告](reports/DAY_3.md)，后续Git写操作仍由用户亲自完成，不自动进入D4。

2026-10-12初检：当时20个变更文件（13新增、7修改）与D3范围一致；17份Python文件AST可解析、14份Markdown围栏闭合、61处本地链接目标存在、D3报告8项完整，`git diff --check`通过，但该命令未覆盖尚未跟踪的PDF。原有锁定依赖版本未改变，最终新增22个锁定包且不含community/classic；真实.env仍被忽略且未跟踪。当时本批尚未暂存、提交或推送。

### Git交接补记（2026-10-13）

用户亲自暂存20个D3文件。Codex只读检查`git diff --cached --check`时，发现合成PDF被视为文本，xref定长记录的行末空格触发10处格式提示（退出2）。新增`.gitattributes`中的`*.pdf binary`，让Git保留PDF字节并按二进制处理；当前范围21个文件（14新增、7修改），报告和日志同步补记。业务源码和PDF原字节未改，此轮只检查Git属性与文档，不重跑业务测试或模型请求；补充文件的暂存/提交/推送仍由用户操作。

最终用户暂存统计为21个文件、PDF显示Bin；用户提交 [45e74c02eef257f85e4360c2e14213fd82a8237d](https://github.com/yan-ye-dhy/project-copilot/commit/45e74c02eef257f85e4360c2e14213fd82a8237d)并推送`main`成功。2026-10-13计划维护前，通过GitHub只读接口核实main为该提交，与本地HEAD/origin/main一致，工作区干净。当时D3阶段A已完成，阶段B没有学习理解/手测完成反馈，不自动勾选；随后实际手测与确认见下方补记。

## 35日计划学习目标微调（2026-10-13）

用户要求在现有34个主任务日＋1个可前移弹性日上补强Prompt Engineering、Agent/MCP知识与观测口径。已从GitHub读取计划、协作规则、日志和D3报告，在`45e74c0`核对四份文件与本地Git blob一致；本次不是D4开发或新的Day成果。

现计划已具备prompt/回答契约、生成配置冻结、无答案/引用回归、Tool Calling状态/Schema/预算及成功失败Trace。仅补强D5消息角色、模板变量、上下文及Pydantic/JSON Schema，D10/D17与8.3的Prompt冻结清单，D19独立小规模Prompt对照/弱证据/注入/结构化失败，D24–D28明确调用链与MCP理论，D33复用已有记录的成功率/Token/延迟/成本口径。D5优先消息模板/上下文/回答契约，示例讲解融入D6既有复盘，实调对照留D19；MCP默认只学架构，可选实践不成为核心服务或验收依赖。

日号、阶段日数、每日90–150分钟、每周3–4个项目日和D4逐日任务保持原文；基线、优化实验、工具测试、事务并发和七项验收没有裁剪。D19只比较冻结Prompt与一个候选，用开发/独立可靠性样例并固定证据，不用保留集选词、不覆写V0/D17结果；效果与离线回归分开，后续收费对照仍遵守8.3。D33不新建监控系统，缺usage/阶段/价格标未采集，估算不冒充实际账单或完整成本。

仅修改开发计划及必要的D3进度引用（README、日志、DAY_3报告），协作规则与业务源码未变；未启动服务、重跑业务测试、调用模型或推进D4。新文档改动尚未暂存/提交/推送，仍由用户亲自操作。

收尾检查通过：差异仅4份Markdown，14份Markdown围栏闭合、61处本地链接及7处锚点可定位、表格列数一致、D3报告8项完整；34个主任务日编号、D35弹性日、阶段表、工时段及D4行原文一致，原核心验收/面试要点保留，`git diff --check`通过。业务源码、脚本、测试、依赖/锁文件与协作规则没有差异；本轮为纯文档维护，不以历史101项测试宣称重新执行验证。

## D3用户手动验收与学习确认（2026-10-13）

用户主动要求开始D3手测，由本人在新项目终端执行命令并反馈退出码。Codex逐环节指导；初次TXT结果由用户粘贴，后续JSON由Codex直接读取，与源文件、默认配置或最初结果比较，没有代替用户运行查看器。

下表的文件/参数均用于`uv run --frozen python scripts/inspect_document.py`，用户随后执行`echo $?`。手测输入及输出保存于被Git忽略的`data/manual-d3/`；重命名对照通过`cp`复制相同字节，空文件通过`printf ''`创建，正常输出用`>`保存，非法配置的错误输出用`2>`保存。

| 用户实际输入/参数 | 实际结果 | 验证点 |
| --- | --- | --- |
| `data/manual-d3/short.txt`，默认配置 | ok，1块、38字符，退出0；原文与来源正确，page为null | UTF-8 TXT原文、来源及单片段 |
| `tests/fixtures/documents/sample.md`，默认配置 | ok，5块，长度178/750/750/151/55，退出0；加载原文与源文件一致，首块保留标题、列表和代码块 | Markdown原文保留及无空格中文长度边界 |
| `tests/fixtures/documents/sample.pdf`，默认配置 | ok，保留页0/1/2，页1空白；仅页0/2生成片段，长度49/62，退出0 | PDF真实页索引与空白页处理 |
| 重读`data/manual-d3/short.txt`与同内容的`renamed.txt` | 两次均ok；用户报告最后退出0；两个来源名不同，内容哈希、document_id、index_version、chunk_id一致，重读结果也与最初输出一致 | 改名及重复读取的稳定身份 |
| `data/manual-d3/short.txt --chunk-size 15 --chunk-overlap 3` | ok，4块，长度15/6/15/4，退出0；文档身份不变，切分版本及全部chunk_id改变；末两块有3字符“和来源”重叠 | 配置生效、字符上限、目标重叠与版本区分 |
| `data/manual-d3/empty.txt`，默认配置 | empty，0块、最大长度0、chunks为空，并明确提示无非空文本，退出0 | 空文件是已处理的正常结果 |
| `data/manual-d3/short.txt --chunk-size 15 --chunk-overlap 15` | error，明确提示chunk_overlap必须小于chunk_size，退出1，无未处理Traceback | 非法配置被校验拒绝 |

以上为七项用户手测。用户本人负责运行命令，Codex负责读取和比较结果，不据此宣称用户逐字段完成检查。

用户随后明确回答“已学习，能够解释”，确认已通过ChatGPT学习，并能解释文件→原文→切分→metadata→JSON调用链、PDF空白页及页码处理，以及改名/参数变化对不同标识的影响。结合开发代码已推送与上述实际手测，D3阶段A/B均完成；没有自动进入D4。

本次全部离线，没有读取真实.env、调用模型、连接Qdrant或启动API，也未重跑历史101项自动测试。同名不同内容、重复位置、跨进程身份、非法UTF-8、损坏PDF、错误路径、空MD/PDF及禁止模型调用的检查仍为开发期自动验证，不登记为用户本次手测；PDF文字层不扩展为OCR验收。

本次只补记README、开发计划、开发日志、学习笔记和D3报告，并保留此前35日计划微调。代码/测试/依赖未修改，手测数据不提交；这五份文档尚待用户亲自暂存、提交和推送。

## D4：单文件真实Dense索引与检索（2026-10-13）

**开发期快照（提交前）：代码及自动验收完成；阶段A待用户Git交接，阶段B待ChatGPT学习及主动手测。** 开始前用户已亲自提交、推送D3收尾及计划微调 [f50abd48a21edcc65cca76a46aa4007534467d8d](https://github.com/yan-ye-dhy/project-copilot/commit/f50abd48a21edcc65cca76a46aa4007534467d8d)，Codex核实GitHub main、本地HEAD/origin/main一致且工作区干净。用户随后明确开启D4；沿用D2/D3，不修改原项目或既有加载/切分/Embedding实现。

**最新状态（2026-10-14）：D4阶段A/B均完成。** 用户已推送4349c10、亲自完成8项手测，并确认已完成ChatGPT学习且能解释关键流程；具体证据见下方交接与验收补记。以下保留开发期记录。

新增Qdrant官方SDK适配、DenseIndexSpec/成功清单/检索证据契约、ingestion/retrieval服务、单文件CLI、最小Compose和四份测试。客户端实际锁定1.19.1，新增9个锁定依赖项（含Windows条件包），Linux实际新增8个依赖包，原有锁定版本未变；共Resolved61、Checked58。Compose固定qdrant/qdrant:v1.19.2，project-copilot专属network/volume，宿主仅绑定127.0.0.1:6433。

参考原vdb.py集合存在/余弦创建与retriever.py top-k接口；官方SDK提供存储基础设施，新项目设计向量/片段绑定、成功清单、双版本及检索编排。没有导入原包、初始化BM25或FastEmbed，也没有上传/问答接口。D3 metadata和UUID完整保留，另以dense_index_version标识模型/维度/切分等配置。

当前清单仅支持顺序单进程。先核对已有清单/集合兼容，再发Embedding，Qdrant确认completed后原子保存；失败不返回入库成功。检索使用成功文档和Dense版本双过滤，再核对具体chunk ID、内容哈希及切分版本；稳定ID避免重复点，不意味着重复嵌入免费或已经实现跨存储事务。完整恢复、并发及效果评测按原后续任务推进。

### 自动验证与真实服务

| 实际命令/检查 | 执行者及结果 |
| --- | --- |
| `timeout 45s .venv/bin/python -m pytest -q` | Codex：165 passed, 1 skipped in4.41s；165项全离线，默认跳过HTTP服务测试 |
| Qdrant适配测试 / pipeline+CLI测试 | 协作代理：34 passed in0.48s / 30 passed in2.16s；真实SDK配假传输或本地模式，不计作服务器实调 |
| `PROJECT_COPILOT_RUN_QDRANT_INTEGRATION=1 .venv/bin/python -m pytest -q tests/test_qdrant_integration.py` | Codex：1 passed in0.76s；真实Docker HTTP、假二维向量，验证原文/页0和2/成功范围/重复覆盖/配置拒绝；随机pc_test_集合已清理 |
| `UV_CACHE_DIR=/tmp/project-copilot-d4-uv-cache UV_PYTHON_DOWNLOADS=never uv sync --locked --offline --check` | Codex：退出0，Resolved61、Checked58、Would make no changes |
| Compose配置、启动及HTTP就绪 | Codex：Docker Engine29.6.1，Compose5.3.0；`config --quiet`通过，`up -d qdrant`成功；实际服务1.19.2，`/readyz`返回200 |

初始WSL未启用Docker集成；用户明确表示将开启Docker Desktop/WSL Integration，之后实际可用。沙箱中的socket与uv网络限制经必要执行授权解决，未通过新建其他项目或操作旧服务绕过。曾为备用验收下载官方v1.19.2二进制并核对SHA-256，最终无需运行，真实验收全部使用新Compose容器。

### 两次明确授权的模型联调

按计划8.3先说明2次请求范围及官方价0.5元/百万输入tokens，以1000 tokens估算约0.0005元；用户明确同意仅2次最小Embedding验收。未读取/打印.env原文，应用只使用新项目自身配置，SDK自动重试关闭。

实际命令均使用临时uv缓存及`uv run --frozen python scripts/dense_index.py`，输出重定向到Git忽略的`data/d4-auto/`：

| 子命令与输入 | 实际结果 |
| --- | --- |
| `ingest tests/fixtures/documents/sample.pdf --live` | 退出0；1次普通请求嵌入2片段，pc_knowledge_dense_v0写入2点并保存完成清单；0.6295秒 |
| `search "local document loading" --live` | 退出0；第二次请求嵌入1个问题；top_k5实际返回2条，首条原文为Project Copilot page one: local document loading.，页0相关度0.71600187，页2为0.44767696；0.5442秒 |

随后只读核对集合/已存Point/清单：唯一named dense、Cosine、无sparse；两个向量实际长度均2048且有限，原文、PDF页0/2、全部D3 metadata与chunk ID保留，Dense版本匹配。成功清单包含1个PDF和2个片段，应用点保留在专属volume，没有清理应用集合；不包含费用/密钥/完整向量的检查摘要保存在`data/d4-auto/storage-validation.json`。

本次费用请求共2次，已使用完，不追加Embedding或聊天模型调用。未采集usage/账单，不能报告实际tokens/实付；仅证明一个合成样例的工程链路，不报告Recall/MRR或普遍检索效果。当前未进行用户D4学习确认或手测，所有上述验证均为Codex开发期验收。

完整文件表、设计与8项交接见 [D4报告](reports/DAY_4.md)。本批20个文件（11新增、9修改）待用户亲自提交、推送；不将模型密钥、data/、Docker卷或备用下载加入Git，不自动进入D5。

收尾检查：26份Python AST、15份Markdown围栏/表格、71处本地链接及7处锚点、D4固定8项报告通过；原35日任务与预算未变。9份已跟踪差异的git diff --check及11份新增文本的行尾/换行检查通过；.env和运行数据被忽略且无暂存改动。未使用的备用二进制临时下载已清理。文档补记不重跑已通过的业务测试或模型请求。

### 用户Git交接与手动验收补记（2026-10-14）

用户亲自暂存20个文件，git diff --cached --check无输出，提交 [4349c106030649eeeeee92fc747b04664ecb8bd0](https://github.com/yan-ye-dhy/project-copilot/commit/4349c106030649eeeeee92fc747b04664ecb8bd0)并推送main成功（f50abd4..4349c10 main→main）。Codex核实GitHub main与本地HEAD/origin/main一致、工作区干净，阶段A完成；本段补齐原报告的提交前快照。

用户随后主动开启D4手测，命令均由本人在新项目终端执行。Codex逐步指导、读取被Git忽略的`data/manual-d4/`结果文件，与D3原文/metadata、成功清单或此前结果比较，没有代运行服务、查看器或模型请求。

| 用户实际操作 | 实际结果 | 验证范围 |
| --- | --- | --- |
| `uv run --frozen python scripts/dense_index.py ingest tests/fixtures/documents/sample.pdf`，无live | 提示必须显式添加live，退出2 | 保护开关在读取配置/构造客户端前拒绝 |
| `docker compose --env-file .env.example up -d qdrant`及`ps` | project-copilot-qdrant-1，v1.19.2，Up，127.0.0.1:6433→6333 | 本人启动独立Qdrant容器 |
| curl `/readyz`，绕过代理并显示响应头 | HTTP200，all shards are ready，退出0 | 实际HTTP就绪响应 |
| GET `/collections/pc_knowledge_dense_v0` | status ok，green，唯一dense2048/Cosine、无sparse/multivector，points_count统计2，退出0 | 集合schema及入库前统计；此统计不冒充精确count |
| `dense_index.py ingest tests/fixtures/documents/sample.pdf --live` | ok/DENSE，2块，UUID和原文身份保持，成功清单1份文档；0.9466秒，退出0 | 本人真实重入库及清单登记 |
| POST `/collections/pc_knowledge_dense_v0/points/count`，JSON exact=true | 精确count=2，退出0 | 结合稳定ID确认本次重入库未增加重复点 |
| `dense_index.py search "local document loading" --live` | ok/DENSE，top_k5返回2块，页0/2原文与全部metadata保留；首条页0 score0.71589005，另一条0.4474684；0.77秒，退出0 | 本人真实问题向量化与一个样例的证据检索 |
| 进程变量`PROJECT_COPILOT_EMBEDDING_EXPECTED_DIMENSIONS=3`后运行同一ingest/live | error，索引配置与完成清单不一致，请使用新集合重新索引；退出1，无未处理Traceback | 加载/切分后由ManifestStore.load拒绝，早于ensure_collection、Embedding及upsert |

手测前已说明这一轮仅2次真实请求及官方标准价/估算，用户随后亲自执行带live的入库和查询：1次含2片段、1次含1问题，与2026-10-13开发期2次授权调用分开记录。重定向路径误写为`dara/manual-64/search.json`时，Bash打开文件失败，uv/Python尚未启动，因此未新增模型调用；缺live及配置冲突也没有模型请求。没有采集实际usage/账单，不把费用预算或Cosine score当成实付或正确率。

用户提出Qdrant集合接口是否来自容器，Codex结合Compose映射及main.py解释：Qdrant程序提供自己的HTTP服务，宿主6433映射容器6333；本项目FastAPI是另一个服务，启动后用8002且当前仅health，qdrant.py是SDK客户端适配器。用户随后明确回答“已学习，能够解释”，确认已通过ChatGPT学完D4并能解释入库/检索主链、清单配置约束及服务区别；结合上述真实操作，阶段B完成。

覆盖边界：此次没有重新执行165项离线或1项HTTP测试，没有另行验证服务端集合维度拒绝、全部原始向量数值、多进程并发/完整故障恢复或Recall/MRR。它验证的是给定样本的手动工程链路；历史自动证据保留，不扩大为用户本次验收。

本次仅更新README、开发计划、开发日志、学习笔记和D4报告这5份文档；业务源码、测试、依赖与35日任务/预算未变，未由Codex新增模型请求，手测数据仍不提交。验收补记待用户亲自提交、推送，不自动进入D5。

## D5：DeepSeek结构化问答与引用（2026-10-14）

**最终状态：D5阶段A/B均完成（2026-10-15）。** 用户已推送开发提交e01ad7d，完成本轮手测并确认ChatGPT学习理解，详见本节末尾补记。下文保留开发期记录。用户明确开启D5时，本地main/origin/main和只读核实的GitHub main均为D4收尾提交`061864bea22a54026f001c038cd272e2e8ca6da1`，工作区干净，D1–D4阶段A/B已完成；本轮范围始终仅D5。

### 实际实现与来源

新增显式DeepSeek对话客户端、可选独立密钥/参数、固定System与User消息、编号上下文、JSON/Pydantic回答契约、从本次证据映射的引用、`POST /rag/ask`及一条固定问题的live脚本；复用D4 DenseRetriever、成功清单和现有索引。没有新增依赖、修改D3/D4身份或索引配置，也未实现上传、历史会话、评分、改写或重排。

原项目`graph.py:33`、`nodes/generation.py:24`、`utils/utility.py:106`分别用于参考非推理参数、消息构造与证据编号；不直接移植原context注入System的提示词。新Schema、引用映射、消息数据边界、预算/指纹及HTTP编排自主设计。逐段教学与调用链交ChatGPT，交接见 [D5报告](reports/DAY_5.md)和 [学习笔记](STUDY_NOTES.md#d5检索证据到结构化回答)。

2026-10-14核验[官方当前模型与价格](https://api-docs.deepseek.com/quick_start/pricing/)、[退役公告](https://api-docs.deepseek.com/news/news260424/)、[非推理开关](https://api-docs.deepseek.com/guides/thinking_mode/)及[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)：默认使用deepseek-flash，显式thinking.disabled、temperature0、max_tokens1024、timeout30秒、max_retries0、json_object。请求别名与实际返回模型分别记录；只靠JSON mode不能保证Schema正确或证据支持。

### Codex实际自动验证（2026-10-14，历史记录）

工作目录为新项目。离线测试使用真实已安装的OpenAI SDK2.54.0及MockTransport，或记录调用的测试替身；没有把模拟回答/向量登记为真实模型结果。

| 命令/检查 | 实际结果与范围 |
| --- | --- |
| `.venv/bin/python -m pytest tests/test_chat_models.py tests/test_models.py -q` | 初次180通过；兼容性修复后192通过。检查消息/JSON参数、显式非推理、零重试、脱敏错误、usage及资源关闭；原Embedding行为回归 |
| `.venv/bin/python -m pytest -q`，补齐回答测试前 | 280通过、1默认跳过；不是最终总数 |
| `timeout 30s .venv/bin/python -m pytest tests/test_answering.py tests/test_rag_smoke.py -q` | 42通过；消息数据边界、回答契约、预算/编号、空证据、错误与CLI gate |
| 默认沙箱中的同步HTTP测试及8秒faulthandler诊断 | 线程池与事件循环唤醒等待，超时退出124；没有登记通过，模型均为假传输。允许本机运行权限后测试正常结束 |
| `timeout 45s .venv/bin/python -m pytest -q`，第一次允许本机权限 | 357通过、1失败、1跳过。失败为测试直接在新版FastAPI application.routes查找被include的路由，实际POST已200；改为保留真实HTTP/OpenAPI契约断言 |
| 同一45秒全量命令，修复后 | **358通过、1跳过，4.83秒**。真实SDK→D4检索编排→D5回答→ASGI响应的离线全链路及异常资源关闭通过；外部Qdrant测试默认跳过 |
| 只读配置/清单检查 | 不打印密钥值；已有成功清单1份PDF、2块。初查无DeepSeek字段；用户按指导保存后确认可加载，默认flash/max_tokens1024 |
| `curl --noproxy 127.0.0.1 --fail --silent --show-error http://127.0.0.1:6433/readyz`；`docker compose --env-file .env.example ps` | 准备阶段curl退出7、服务不可达；docker命令报Input/output error退出126。等待Windows Docker Desktop/WSL集成恢复，不据D4历史状态推断当前服务可用 |

主链的结构验收不证明事实支持或效果指标。D5提供Prompt/Schema与上下文规则指纹，完整基线冻结仍留D10；Few-shot效果、弱证据/注入与Prompt对照留D19，不挤占D6上传或增加天数。

### 真实验收授权与交接状态（开发期快照）

用户已明确授权本次**1次查询Embedding＋1次DeepSeek生成**，复用已有PDF，不重入库、不自动重试。按Embedding≤1000输入tokens、DeepSeek约3000输入/最多1024输出tokens、Flash峰时缓存未命中价估算约¥0.0005＋US$0.0022；这是事前预算估算，不是实付。这次授权已用于下列自动验收，不扩展为后续批量或用户手测的无限授权。

用户在新项目`.env`保存DeepSeek key，Codex仅核对能否加载，不输出真实值；`.env.example`为模板，不自动覆盖已有`.env`。指导Windows Docker Desktop启动与WSL集成属于开发环境准备，不登记为用户已完成D5功能手测。

Docker恢复后Codex执行`docker compose --env-file .env.example up -d qdrant`启动现有项目容器，readyz成功；没有删除集合或volume。使用临时脚本`timeout 100s .venv/bin/python /tmp/project-copilot-d5-live-check.py`（不作为仓库交付文件），先确认8002未占用、启动Uvicorn并GET health200，再只发送一次POST `/rag/ask`：`{"question":"样本文档第一页描述了什么功能？"}`。返回HTTP200/answered，答案为“样本文档第一页描述了本地文档加载功能。”；实际发送2块，1条引用number1指向sample.pdf页0，原文`Project Copilot page one: local document loading.`，UUID/全部metadata与D3样本匹配。Codex核对的是这一条已知事实，不把它当人工冻结gold评测。

真实问答耗时1.606秒，请求及实际返回model均为deepseek-flash；usage为prompt358/completion24/total382、cache_hit0/cache_miss358。供应商未在本层采集Embedding usage，实际账单未查询，不从估算冒充实付。Prompt为rag-answer-v1，fingerprint为`83971da601780ea5a5f5e6e4b45f11d952cb261e50d7275a1efb38494a46a9ea`，回答Schema哈希为`ded2c45bf4b0dcd4888884ad1ff1bcbc729a1846ee0977ec90e7dfdd903c5941`。

验收后POST Qdrant points/count exact=true得到count2，原成功清单/索引不重建。临时Uvicorn PID9184通过SIGINT正常停止，日志含startup/health200/ask200/shutdown complete；Qdrant容器保留运行。自动输出位于被Git忽略的`data/d5-auto/`，无密钥或完整向量，不提交运行数据。本轮没有额外执行live CLI或第三次模型请求。

开发交接前快照：Git尚未暂存、提交或推送，由用户亲自操作；固定8项报告已生成。当时ChatGPT学习、D5手测及理解确认待用户主动开启，不自动开发D6；最新状态以下方补记为准。

收尾：`UV_CACHE_DIR=/tmp/project-copilot-d4-uv-cache UV_PYTHON_DOWNLOADS=never uv sync --locked --offline --check`成功，58个包无需改动；13份Python AST、15份Markdown围栏、88处本地链接/锚点、8项报告、20个提交候选的行尾/换行及真实凭据扫描通过，未输出任何密钥值。`git diff --check`无输出，`.env`/`data/d5-auto/`被忽略，无新增依赖/锁文件改动；没有因文档收尾重复运行已通过的业务或模型验收。

### 用户Git交接、手动验收与学习确认（2026-10-15补记）

用户亲自核对20个暂存文件及git diff --cached --check，提交 [e01ad7d0209d6db61a0b150070afe4a63163204e](https://github.com/yan-ye-dhy/project-copilot/commit/e01ad7d0209d6db61a0b150070afe4a63163204e)，执行git push origin main成功（061864b..e01ad7d main→main）。Codex当时已只读核实GitHub main与本地HEAD/origin/main一致、工作区干净，阶段A完成；2026-10-15收尾前再次核实结果相同。此前报告的待提交状态是开发期快照。

用户主动开启D5手测，以下命令、服务和请求均由本人在新项目终端操作。Codex逐步指导、读取`data/manual-d5/`保存的结果，并通过D3 loader/splitter离线对照PDF原文和全部metadata；没有代发本轮HTTP、启动服务或模型请求。

| 用户实际环节 | 实际结果 | 验证范围 |
| --- | --- | --- |
| 1. `uv run --frozen python scripts/rag_smoke.py`，无live；echo退出码 | argparse提示需live，退出2 | 配置加载/客户端构造前的CLI保护 |
| 2. Compose使用.env.example启动qdrant，查看ps | v1.19.2，Up，127.0.0.1:6433→6333 | 用户启动既有容器 |
| 3. curl 6433/readyz，echo退出码 | HTTP200，all shards are ready，退出0 | Qdrant就绪；正文末尾无换行使0与正文连在一起 |
| 4. 普通Uvicorn命令启动8002 | PID42244，startup complete | 正常配置启动 |
| 5. GET /health | HTTP200，Project Copilot/development | 当前应用与配置可响应 |
| 6. POST /rag/ask，question为三个空格 | HTTP422，string_too_short，loc为body/question | 在请求体校验阶段拒绝 |
| 7. 正常固定问题POST，保存answer.json | HTTP200/answered；2块证据、1条真实引用 | 用户真实RAG请求及Codex原文对照，见下文 |
| 8. Ctrl+C关42244，再用三空格DeepSeek key前缀启动；正常问题POST | 原进程正常关闭；PID47593启动；HTTP503，安全detail保存为missing-key.json | 进程级配置覆盖与空白密钥拒绝；未提供服务端ChatConfigError日志，不登记观察到该日志 |
| 9. 终端内联Python构造ModelAnswer | 合法引用[1]接受；answered空引用报ValidationError，回答必须包含引用编号 | 手写数据的本地契约练习，不是供应商坏输出或HTTP502测试 |
| 10. Ctrl+C关闭临时服务 | shutdown complete，PID47593退出，返回提示符 | 用户完成本轮后端收尾；Qdrant未反馈关闭 |

本轮实际答案为“样本文档第一页描述的是本地文档加载功能。”，引用number1的原文为`Project Copilot page one: local document loading.`，source_name为sample.pdf、page为0，chunk UUID为`a9834897-ad5b-573a-bcfd-dcf449878f4c`；全部metadata与样本一致。模型收到2块证据后引用其中1块；这不是对当前集合精确点数的重新验证。本轮没有重入库或执行count。

手测前只读核对默认deepseek-flash/temperature0/max_tokens1024及既有1份PDF清单，并核验[智谱标准价](https://docs.bigmodel.cn/cn/guide/start/pricing)和[DeepSeek价格](https://api-docs.deepseek.com/quick_start/pricing/)。向用户说明本轮正常POST对应1次问题Embedding＋1次DeepSeek，按1000及3000/1024 tokens保守估算约¥0.0005＋US$0.0022，用户随后本人执行一次。实际DeepSeek usage为prompt358/completion25/total383，cache_hit128/cache_miss230；请求与返回模型均为deepseek-flash。Embedding usage、实付和独立端到端耗时未采集；预算不是账单。

本轮模型请求与前一天Codex自动验收的1次Embedding＋1次DeepSeek分开记录，D5已知合计4次供应商请求。CLI拒绝、空白问题422和本地Schema练习不请求模型；临时空白key在create_chat_client中拒绝，发生在DenseRetriever.search之前，Embedding/聊天均未请求。此前Embedding/Qdrant客户端可能已构造，不能把构造客户端和发送请求混为一谈；HTTP503正文只提供安全detail。

用户最后明确回复“已学习，能够解释”，确认已通过ChatGPT学完D5，能说明问题→检索证据→System/User消息→JSON/Pydantic校验→真实引用，以及空白密钥为何在模型请求前被拒绝。结合真实手测与开发提交已推送，阶段A/B均完成，不自动进入D6。

覆盖边界：358项离线测试及开发期HTTP验收本轮未重跑；没有用户空检索、坏JSON/越界引用HTTP502、完整弱证据/注入测试或Prompt对照。单个已知事实不产生整体准确率、Recall/MRR或普遍引用支持率。业务源码、依赖、配置模板与35日计划未变；本次只补六份文档，运行结果与真实.env继续被Git忽略，交用户亲自提交、推送。

文档收尾检查通过：15份Markdown围栏、49张表格列数、88处本地链接及其中10处锚点、D5固定8项报告、六份修改文件行尾/换行；35日逐日任务行与HEAD完全一致。git diff --check无输出，暂存区为空，修改范围仅上述六份文档；.env和两份用户手测JSON被Git忽略。本次检查未运行业务测试、服务或收费模型。

## D6：单文件上传与Dense模块验收（2026-10-15）

**当前状态：D6阶段A/B均完成；2026-10-16用户手测及学习理解已确认。** 用户明确开启D6，开始时main/origin/main在D5收尾`cf5e373d7df905e196f9324e66d63cfb2dfe795b`，工作区干净，D1–D5阶段A/B均完成。用户随后要求继续，本轮始终只做D6；开发期与用户手测分别记录如下。

### 实际实现与设计边界

新增POST /documents/upload及上传成功Schema，基本名称/后缀/MIME、单file字段、实际文件字节校验，服务端随机临时文件和finally清理。source_name可选参数在D3解析后/切分前替换展示名，D4默认CLI、身份及索引指纹不变；创建客户端/入库复用既有流程，indexed仅在清单保存后返回。

新增本地run_id、X-Run-ID和完成耗时日志，复用Starlette内置中间件按真实body字节限制整体请求；默认5MiB文件＋64KiB表单余量，复制阶段另限制文件。tracing_enabled默认false，true明确拒绝，上传trace_exported=false；没有初始化Phoenix/OTel或声称导出通过。main另提供未知500安全响应及关联ID。

审查发现HTTP多请求会破坏D4清单的顺序假设，增加单进程非阻塞锁覆盖完整service生命周期；忙时409，已复制文件仍清理。只保护HTTP单进程，跨进程/CLI并发及事务仍待后续。文件名来自解析后的multipart元数据，不构造物理路径；框架对Windows旧式路径取basename，校验器仍拒绝未规范化路径。相同内容改名共享身份，最近上传可能覆盖展示来源。

参考原routes/embeddings.py:35/89的路由/服务/线程池，embedding_management.py的加载/切分/来源思路；未搬原客户路径拼接、整文件无限读取、多文件并发及导入期初始化。原api.py:27仅用于理解未来Phoenix位置。新增python-multipart0.0.32；Starlette已存在，显式限制≥1.7保证body中间件可用，其他旧包未升级。

### Codex实际自动验证

工作目录为新项目；模型全部使用测试替身，pipeline使用内存Qdrant和假二维向量，不把它们记为真实语义效果。同步ASGI线程测试沿用D5已知的本机运行权限，无收费服务请求。

| 实际命令/检查 | 结果 |
| --- | --- |
| UV_CACHE_DIR与UV_PYTHON_DOWNLOADS按D4缓存设置，uv add --no-sync新增multipart，uv sync --locked --link-mode copy | 解析62个包，安装python-multipart0.0.32；新项目独立环境更新成功 |
| 收紧multipart下界为0.0.32及显式Starlette约束，uv lock --offline、uv sync --locked --offline --link-mode copy | 成功；仅重建项目元数据，原包版本保持 |
| `timeout 45s .venv/bin/python -m pytest -q`，加入新用例前 | 原358通过、1默认跳过，5.85秒 |
| `timeout 45s .venv/bin/python -m pytest tests/test_request_context.py -q` | 9通过，1.41秒；run_id、actual/missing/underreported长度、422/404/安全500及配置失败 |
| 三个D6测试文件首轮focused | 47通过、1失败；Windows路径在multipart解析时已规范为escape.txt，原测试误要求HTTP拒绝，未作为实现漏洞掩盖 |
| 修正为验证规范化后的安全来源、另测校验器防御，并加入真实重叠HTTP用例；全量同一45秒命令 | **408通过、1默认跳过，6.14秒**；3格式、源名/页/ID、失败清理、上游/清单故障、并发409及旧功能回归 |
| 只读Settings配置检查 | 两家key可加载，只输出布尔状态；默认flash/max_tokens1024、文件5242880字节、追踪关闭 |
| curl 6433/readyz；Compose up -d qdrant后再查 | 起初连接失败退出7；启动已有容器后all shards are ready，未删除集合或volume |

授权前准备快照：拟通过HTTP上传自编sample.pdf的2块，再问一条固定问题，最多2次Embedding＋1次DeepSeek；按两次Embedding各1000 tokens及DeepSeek3000输入/最多1024输出估算约¥0.001＋US$0.0022。当时已请求本轮授权、尚未执行，不复用D5已用完的授权；随后实际执行见下方补记。

详细8项交接见 [D6报告](reports/DAY_6.md)；完整运行链与复盘图见 [学习笔记](STUDY_NOTES.md#d6上传接入与dense模块复盘)。Git尚未暂存/提交/推送；用户ChatGPT学习、D6手测与理解确认待主动开启，不自动进入D7。

补上默认应用logging初始化后，全量再次408通过、1跳过，6.11秒；锁定环境检查59个包无需改动。等待收费授权期间，Codex另运行零模型真实HTTP预检：临时启动Uvicorn，GET health200，上传unsupported.exe得415、空TXT得422，三条X-Run-ID均出现在本地完成日志，trace_exported=false；这些请求在入库工厂前退出，模型请求0次，临时服务正常关闭。结果为被忽略的data/d6-auto/http-preflight.json与日志，没有冒充用户手测。

授权前检查：9份Python AST、16份Markdown围栏、50张表格、97处本地链接及13处锚点、D6固定8项报告通过；35日逐日任务行未变。当时18个提交候选的行尾/换行及真实凭据扫描通过，未打印密钥；git diff --check无输出，暂存区为空，尚未执行D6收费调用。

### 获授权后的最小真实自动验收（2026-10-15补记）

用户明确回复“同意这3次最小真实验收”，Codex执行一次`timeout 150s .venv/bin/python /tmp/project-copilot-d6-live-check.py`，不重试。仅POST一次上传sample.pdf和一次固定问题“样本文档第一页描述了什么功能？”，日志实际记录2次智谱Embedding＋1次DeepSeek，均HTTP200；没有另跑live CLI或扩大语料。自动启动的Uvicorn进程24759已正常SIGINT关闭，原Qdrant及volume保留。

上传HTTP200/indexed，1167字节、2块，约0.9844秒；文档UUID为`c8d63f20-bc98-52e9-a839-3bda4a1a0bfc`，原source_name、哈希与片段UUID不变。问答HTTP200/answered，约1.2227秒，答案为“样本文档第一页描述了本地文档加载功能。”；2块上下文、1条引用，number1对应sample.pdf页0原文`Project Copilot page one: local document loading.`，全部metadata与D3原文切分结果一致。

上传run_id为`d8b28480-2cf3-4c74-8009-121e7fe711c8`，问答为`e3434784-afe9-4f34-84ac-c19fceeed3ac`，均可在实际完成日志中找到；上传正文和响应头同值。重复上传后精确点数仍为2，新增临时文件残留0，成功清单仍为该PDF；这验证本例稳定ID覆盖，不扩展为D18完整故障恢复或跨进程事务结论。trace_exported=false，没有验证Phoenix导出。

实际DeepSeek模型deepseek-flash，输入358/输出24/总382 tokens，cache_hit128/cache_miss230；Embedding usage和实际账单未采集。Prompt/Schema/Dense指纹均与D5一致，未修改基线。可提交的 [D6验收记录](evidence/D6_DENSE_SMOKE.json)仅含自编样本、明确白名单配置与实际输出；开发基点cf5e373及code_dirty=true如实记录，未伪造D6commit。详细原始输出和日志位于被忽略的data/d6-auto；验收脚本位于/tmp，不作为产品新增入口。

这是Codex自动验收，不登记为用户完成学习/手测，也不产生完整检索效果指标。D6代码与自动验证完成，用户Git、ChatGPT学习和用户手测仍待后续；不自动推进D7。

收尾检查通过：19个提交候选，9份Python AST、1份验收JSON、16份Markdown围栏、50张表格、102处本地链接及13处锚点；固定8项报告、34个主任务日表格行与HEAD一致，可前移D35仍保留。候选文件未包含实际密钥，.env及data/d6-auto被忽略，git diff --check无输出，暂存区为空。独立只读审查未发现阻断交付的问题；没有为文档收尾追加模型请求或重复业务测试。

### 用户Git交接与手动验证（2026-10-16补记）

用户亲自暂存19个文件、核对cached stat/check，提交 [a7ec08b616d31bcc8801f2fc0bf550210eb8f4c2](https://github.com/yan-ye-dhy/project-copilot/commit/a7ec08b616d31bcc8801f2fc0bf550210eb8f4c2)，push origin main成功（cf5e373..a7ec08b main→main）。Codex只读核实HEAD/origin/main一致、工作区干净，阶段A完成；2026-10-16手测收尾前再次核实一致。报告此前待提交文字为开发期快照，本次同步实际结果。

用户主动开启D6手测，所有启动、HTTP、文件准备及关闭操作由本人执行。Codex指导并读取data/manual-d6保存的响应/头/点数，离线对照D3 PDF原文及D4成功清单，没有代发本轮HTTP、启动服务或模型请求。

| 用户实际环节 | 实际结果与观察 | 验证范围 |
| --- | --- | --- |
| 1–2. 普通Uvicorn命令启动8002，GET /health | PID78004，startup complete；HTTP200，Project Copilot/development；响应头与完成日志run_id均为9b9ce0ae-43cf-4e6f-b89e-e11328793c21，2.03毫秒 | 应用启动、健康响应及本地请求关联 |
| 3. 上传原PDF但filename设为sample.exe | HTTP415，提示仅支持TXT/MD/PDF；run_id d9159fd5-7a12-49cc-ba25-35b6d7564ece一致，54.76毫秒 | 类型校验在入库工厂前拒绝 |
| 4. 创建0字节empty.txt并正确上传 | HTTP422，上传文件不能为空；run_id cc74ad0d-40ef-4134-8f4c-2a594109a327一致，2.11毫秒 | 复制后发现空文件、客户端构造前拒绝；前两次错命令见下文 |
| 5–6. 上传too-large.txt，并用Path.stat/glob检查 | 文件实际5242881字节；最终HTTP413，上传文件超过允许的大小；run_id 1cdf4011-8a58-4030-8f7a-535acbfdec40一致，44.40毫秒；失败后/tmp上传临时文件0 | 默认5MiB文件额度与失败清理，未冒充整个body限制测试 |
| 7–8. Compose启动qdrant、查看ps、GET /readyz | v1.19.2，Up，127.0.0.1:6433→6333；HTTP200，all shards are ready | 用户准备已有存储服务，无模型请求 |
| 9. 上传sample.pdf一次，保存响应头/JSON | HTTP200，indexed/DENSE，2块/1167字节，source_name sample.pdf；run_id d4e2adc3-d314-4f09-8f93-caa5397d5c53三处一致，1075.91毫秒 | 真实上传入库，文档/片段UUID与D3、原成功清单一致 |
| 10. Qdrant points/count，exact=true | HTTP200，result.count为2 | 重复上传稳定ID后，本例集合仍为2个片段 |
| 11. 固定问题POST /rag/ask，保存响应头/JSON | HTTP200/answered，2块证据/1条引用；run_id 6f0da6cc-b2e2-42f7-a629-9ae3cb9740b2一致，1109.41毫秒 | 本轮实际问答及原文/全部metadata核对 |
| 12. Ctrl+C关闭后端，随后检查临时文件 | shutdown complete，PID78004退出；修正检查命令后临时文件0 | 用户正常收尾；最后一次清理观察发生在停服后，未登记停服前再次检查 |

实际回答为“样本文档第一页描述了本地文档加载功能。”；引用number1对应sample.pdf页0原文`Project Copilot page one: local document loading.`，文档UUID为`c8d63f20-bc98-52e9-a839-3bda4a1a0bfc`、chunk UUID为`a9834897-ad5b-573a-bcfd-dcf449878f4c`。Codex从用户保存结果核验全部metadata、内容哈希、Prompt/Schema与Dense指纹一致；原成功清单仍1份PDF，精确点数由用户实际请求核实。

### 本轮费用、命令问题与覆盖边界

用户在收到范围和估算后明确回复“同意这3次手测调用”。本轮一次PDF上传对应1次Embedding，随后固定问答对应1次问题Embedding＋1次DeepSeek；默认batch64、2048维、deepseek-flash/temperature0/max_tokens1024，无自动重试。事前按两次Embedding各1000输入tokens、DeepSeek3000输入/最多1024输出tokens保守估算约¥0.001＋US$0.0022，依据当日核验的 [智谱价格](https://docs.bigmodel.cn/cn/guide/start/pricing/)及 [DeepSeek价格](https://api-docs.deepseek.com/quick_start/pricing/)，不是实付。

本轮实际DeepSeek usage为prompt358/completion24/total382，cache_hit128/cache_miss230；Embedding usage及实际账单未采集。用户提供了应用请求/完成日志，未另提供供应商HTTP日志；3次调用按已知请求流程及成功响应记录，不宣称查过账单。与开发期3次自动验收分开，D6已知合计4次Embedding＋2次DeepSeek，不能复用已消耗授权追加请求。两轮usage恰好相同，不覆盖原docs/evidence/D6_DENSE_SMOKE.json。

用户第一次空文件命令将-F写成-f并把file值写成/@路径，实际GET得到405及curl22/3；第二次带引号字段内换行、仍为/@，POST得到422“请提供一个名为file的上传文件”；第三次正确-F/file=@后才验证到空文件拒绝。上传PDF前另一次--show-error后缺少空格，使参数和URL拼接，curl解析退出、未发送HTTP；随后正确命令只上传一次。超限时100 Continue为临时响应，最终结果413。Codex收尾Python命令多写右括号，用户收到SyntaxError后自行修正得到0；这些修正不改业务代码、不新增模型调用。

用户另问--env-file .env.example的作用，已结合实际YAML与官方说明解释：它选择Compose变量来源，当前固定镜像/端口/卷无变量占位符，对这些值无实际影响、可以省略；不会自动把全部应用配置注入容器。没有因此修改Compose或.env。

本轮未重跑408项自动测试，未手测并发409、整体body超限、名称/MIME其他边界、上游502/503、故障恢复或Phoenix导出；这些范围保留原自动覆盖/后续验收归属，不据单条问答产生整体准确率或Recall/MRR。后端已关闭，未收到用户停止Qdrant的反馈。本次只整理文档，不自动进入D7。

用户最后明确回复“已学习，能够解释”，确认已通过ChatGPT学完D6，能解释multipart上传→校验→临时文件→入库/成功清单→检索回答，以及来源名与临时路径、run_id与Phoenix Trace的区别。结合已推送开发提交和实际手测，D6阶段A/B均完成；本次文档补记仍由用户亲自提交、推送，D7等待明确开启。

文档收尾检查通过：16份Markdown围栏、51张表格列数、104处本地链接及13处锚点、D6固定8项报告、六份修改文件行尾/换行及真实凭据扫描；未输出密钥。.env和data/manual-d6被忽略，暂存区为空，git diff --check无输出。计划只更新第10节进度，34个主任务日及D35安排保持原样；独立只读事实审核通过。本次未运行业务测试、启动服务或新增模型调用，仅更新六份文档。

## D7：语料来源与第一批证据标注准备（2026-10-16～17）

**当前状态：D7阶段A/B均完成；开发提交acd87cd、收尾92513a7均已由用户推送，人工gold为20/20，用户严格检查退出0并确认学习理解。** 用户已推送D6收尾`3f3159965b94655095acca599333e318dc5287b7`后明确开启D7，开始时main/origin/main一致、工作区干净；10月16日完成草稿及相关测试，中断后于17日接续交接和手动验证，没有重做D1–D6。下文开发期的pending/待办保留为历史快照，最新实际结果见本节末尾补记。

### 实际新增材料与范围

evals/datasets/copilot_v0保存5份Codex自编中文语料：3份取自公开的项目启动/上传/Dense真实记录及实现契约，2份依据Docker/Qdrant官方资料归纳，分别保留公开提交或官方URL、核对日期和适用版本；不是企业生产事故或已人工审核材料。参考原search.py的text/page/source及检索节点的文档边界，复用新项目D3 loader的原文/UUID/字节版本，没有直接导入原agent包。

sources.jsonl记录5份文档，questions.jsonl与labels.draft.jsonl各20条；8类题型为知识4、术语2、错误码/API精确名3、同义3、中文长文2、跨段2、无答案2、追问2。18道可回答题均有候选参考答案及原文位置，2道无答案题分别询问未提供的Rerank保留集提升数值和指定D6用户手测的实付账单；是否足以支持/确实缺失仍待用户人工判定。

证据保留document_id、document_version、page=null、quote、原始Markdown的Unicode字符[start,end)；原文为UTF-8/LF，不去Markdown标记或折叠空白，不记录chunk_id。每题多个单元均作必要证据，工具拒绝同一位置用不同ID重复计gold。20条全部pending，复核者/时间为null，没有伪造人工确认。REVIEW_QUEUE.md用于学习与逐题阅读，不能作为检索语料；尚未切分开发/保留集或冻结标签。

### 实际验证与修正

检查器evals/validate_dataset.py只使用标准库和D3 loader；不读取.env，不构造客户端，不自动修改标签。验证三文件字段/一问一标签/引用关联、文件hash与UUID、中文原文切片、源文件与gold去重、无答案/追问/人工状态门禁。数据检查通过不证明题目质量或语义充分性，ready_for_baseline仅表示本目录全量人工复核门禁满足与否，不能替代D8/D9验收。

| 实际命令/检查 | 结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_eval_dataset.py`，初稿与补来源检查 | 25通过；来源记录要求非空后26通过，约0.24秒 |
| 补题型/answerable双向一致、重复源路径/文档ID、同位置gold不可重复计数，并增加对应测试 | 5条新增回归；仅D7检查器和测试，无业务代码修改 |
| `.venv/bin/python -m pytest -q tests/test_eval_dataset.py tests/test_documents.py` | **58通过，0.60秒**；D7共31项、D3相关27项，均为离线；不据测试临时confirmed夹具计入人工进度 |
| 实际copilot_v0普通CLI检查 | 退出0，5来源/20题，18可答/2无答案候选，confirmed0/pending20，ready_for_baseline=false；10月17日恢复后再次确认 |
| 同一CLI添加--require-reviewed | 按预期退出1，安全JSON提示尚未全部人工确认；门禁正常拦截草稿，没有当成已标注 |

只读语义复核支持20题的当前草稿形式，但同样不是人工确认。修正了018“D6这一次”的指代，明确2026-10-16用户手测固定问题；003/004合并为model-key-lifecycle、005/006合并为curl-upload-form，既有008/019、012/013、014/020也各自同family，降低D8近重复拆分风险。语料另明确成功后的临时清理观测实际在停服后，并区分ModelAnswer校验与编排层的上下文编号越界检查。

本轮没有项目模型API调用、索引写入、服务启动或费用；业务源码、依赖与uv.lock未变。原408项全量应用测试没有重跑，不宣称已取得Recall/MRR、完整准确率、人工gold或保留集结果。下一步按协作规则交用户Git、ChatGPT学习，用户主动要求后才带练人工复核；D7原定约15–20题人工目标仍待完成，不自动进入D8。

收尾检查通过：18个提交候选，2份Python AST、45行JSONL、24份Markdown围栏、54张表格列数、138处本地链接与37处锚点、D7固定8项报告；计划仅更新第10节进度，34个主任务日与D35安排保持原样。文档与资料未含本项目实际密钥，未输出凭据值；.env和运行数据仍被忽略，git diff --check无输出，暂存区为空。语义及检查器只读复核未发现阻断项，人工confirmed仍0；没有为收尾追加模型调用、服务或旧业务测试。

### 用户Git交接、手动检查与人工确认（2026-10-17补记）

用户亲自提交 [acd87cd3f0287cdbe7ba7b3131500ab1db904add](https://github.com/yan-ye-dhy/project-copilot/commit/acd87cd3f0287cdbe7ba7b3131500ab1db904add)，`feat(evals): add corpus and evidence annotation drafts`；首次push将origin误写为oringin失败，修正为git push origin main后成功（3f31599..acd87cd main→main）。Codex只读核实HEAD/origin/main一致、工作区干净，资料及工具交接完成；此处更新开发报告此前待Git快照的实际结果。

用户主动开启D7手动验证，亲自运行普通检查命令及echo退出码，实际得到5来源/20题、18可回答/2无答案候选、confirmed0/pending20、ready_for_baseline=false，退出0。随后本人添加--require-reviewed，得到“标签尚未全部人工确认，不能作为已复核基线输入”、退出1，验证草稿门禁正确拒绝。以上为用户操作，不替换为Codex历史测试。

人工复核初始带练从001开始。Codex额外要求每题书面依据，用户反馈繁琐后改为：实际阅读核对后可按题号范围批量确认，仅存疑题补充说明；逐题写理由不是项目的硬性要求。用户随后明确回复“001–020已核对，全部通过”，据此将20条实际标签的review_status登记为confirmed，reviewed_by为用户终端身份yanye，reviewed_at统一为批量确认登记时间2026-10-17T22:14:55+08:00；未编造逐题书面理由或各自审核时间。

同步更新复核队列的20条状态及已确认参考内容标识；JSONL的问题、参考答案、证据、文档身份/版本、题型和问题族未改。当前人工gold为20/20，这是依据用户明确批量确认登记，不是程序或Codex自行判定。Codex随后运行离线validate_dataset(require_reviewed=True)通过，confirmed20/pending0、ready_for_baseline=true，并与HEAD逐字段/字节核对：只改复核元数据，来源、问题及corpus字节不变。

用户随后亲自再次执行`uv run --frozen python evals/validate_dataset.py --dataset evals/datasets/copilot_v0 --require-reviewed`及`echo $?`，实际返回status=ok、sources5/questions20、answerable18/unanswerable2、confirmed20/pending0/needs_changes0、ready_for_baseline=true，退出0；八类计数保持4/2/3/3/2/2/2/2。由此验证实际登记后的严格门禁，不能把同一结果当作检索效果或D8划分/冻结、D9评测、D10基线已完成。

用户最终明确回复“已学习，能够解释”，确认已通过ChatGPT学完D7，能区分结构检查与人工gold、文档版本/原文位置与chunk_id。结合已推送的开发提交、实际人工确认和手动检查，D7阶段A/B均完成；本次标签复核元数据与文档补记待用户亲自提交推送。没有新增模型调用、索引操作、服务启动或业务代码，不自动进入D8。

手测收尾核对通过：10份修改文件只涉及9份Markdown及复核JSONL；20条标签仅变更三个复核字段，sources/questions及5份corpus共7份文件字节未改，队列20题与标签一致。45行JSONL可解析，24份Markdown围栏、55张表格列数、138处本地链接及37处锚点、D7固定8项报告均通过；计划仅第10节进度变化，原35日任务安排未变。暂存区为空，.env及手测运行数据仍被忽略；Git交接由用户执行，本次没有为纯记录变更重跑旧业务测试。

用户随后亲自提交并推送 [92513a74bddc5bc07fb78ade3b8e26863a23ce3f](https://github.com/yan-ye-dhy/project-copilot/commit/92513a74bddc5bc07fb78ade3b8e26863a23ce3f)，`docs(d7): record manual validation and approved gold`。push实际显示acd87cd..92513a7 main→main，Codex只读核实HEAD/origin/main一致、工作区干净，D7收尾同步完成。

## D8：评测集扩充、问题族划分与冻结工具（2026-10-17）

**当前状态：D8阶段A/B均完成；40题人工复核、正式冻结/复算及副本变化检测均通过，用户已确认ChatGPT学习和理解。** 开发d5c782e、收尾056eff8均已由用户推送。用户明确开启D8时main/origin/main均为92513a7、工作区干净；下文开发期pending/待Git保留为历史快照，最新实际结果见本节末尾补记。

### 实际改动与设计

复用D7的5份语料及来源记录，questions/labels原20行逐字节保留；追加pc-d8-021至040，全部pending、复核者/时间均null。总40题、35可回答/5无答案；知识8、术语5、精确名5、同义5、中文长文4、跨段4、无答案5、追问4。队列区分D7已确认与D8待复核，只根据后续用户明确反馈登记新确认，不把先前001–020的授权扩成40题。

保守合并同义和追问链，共23个family。stable-content-identity含6题同侧；032与旧010归local-run-id，034与旧001归configuration-lifecycle，027/039及029/040同侧。split_manifest.json的copilot-v0-family-v1固定列出30开发/10保留，5个整族进入保留侧，18族进入开发侧；两侧八类均覆盖，可答/无答案分别26/4及9/1。划分依据题型与完整问题族，没有模型、检索指标或挑选效果更好的划分；同语料不同事实允许跨侧，不把主题相关直接等同同义题。

evals/freeze_dataset.py提供validate_split、freeze_dataset、verify_freeze。默认只读预检，检查30–50题/约3:1、八类、ID不重不漏、同族同侧及每侧都有可答/无答案。全量人工确认后才允许创建frozen_manifest.json，现有冻结拒绝覆盖；核验重新读取固定输入，不按冻结清单任意路径读取文件。9份登记输入为sources/questions/labels/split及5份corpus，均记录原始字节SHA256，固定原文定位规则和split_id参与总指纹；生成时间及展示文档不参与。审核元数据也在标签文件内，修改会使原冻结失效。

参考原routes/search.py中的page_content、metadata.page/source边界及原utils/vdb.py的集合契约，复用新项目D3 loader和D7检查器；划分、冻结与指纹逻辑自主设计，没有直接导入原agent包或复制依赖。没有新增依赖、修改业务源码/配置/锁文件或创建监控/评测框架。命令统一从根目录以python -m evals.freeze_dataset运行，避免脚本目录导入evals失败，无需修改sys.path。

### 实际自动验证

执行者为Codex；工作目录新项目，全部离线，不等于用户学习后的手动验证。

| 实际命令/检查 | 结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_eval_freeze.py` | 初版29通过；补冻结字段/文本规则边界后35通过，0.58秒；只在临时合成数据中确认/冻结 |
| `.venv/bin/python -m pytest -q tests/test_eval_dataset.py tests/test_eval_freeze.py tests/test_documents.py` | **93 passed，1.20秒**；D7检查器31项、D8冻结35项、D3相关27项，无失败 |
| `.venv/bin/python evals/validate_dataset.py --dataset evals/datasets/copilot_v0` | 退出0，5来源/40题、35可答/5无答案，confirmed20/pending20、ready_for_baseline=false |
| `.venv/bin/python -m evals.freeze_dataset --dataset evals/datasets/copilot_v0` | 退出0，30开发/10保留、23族，ready_for_freeze=false |
| 上述普通数据检查添加`--require-reviewed` | 预期退出1，提示尚未全部人工确认 |
| 上述划分工具添加`--freeze` | 预期退出1，提示不能正式冻结，执行后frozen_manifest.json仍不存在 |
| 比较HEAD中原问题/标签及来源原文 | 原20行问题/标签字节保留，来源与5份corpus字节未变 |

测试覆盖同族泄漏、ID重叠/遗漏/未知、题量/类别/比例、人工门禁、生成及复算、拒绝覆盖、合法问题/答案/复核字段/来源/划分/原文变更导致失效、展示文件无关、非法JSON、符号链接及LF定位规则。成功冻结仅发生在临时合成夹具，reviewer明确是test-human-reviewer，不登记为用户确认或正式数据冻结；未重跑旧应用全量测试、未启动HTTP/Docker或调用项目模型。

### 交接与待验收

新增20题仍需用户学习后人工核对，可按明确题号范围批量确认，无需逐题书面依据，存疑项再讨论；不重做D7。用户主动开启D8手动验证后，再观察40题状态、检查一个跨段证据与问题族，确认新标签并执行正式冻结及复算。当前冻结准备完成，不宣称D8整体验收通过，也不提前运行D9/D10。

D8开发commit/推送待用户操作，Codex仅做只读Git审核及命令指导；最新报告见 [DAY_8.md](reports/DAY_8.md)。数据集仍由同5份小型自建语料反向拟题，不能把30/10划分当作独立生产评测或通用能力证明；D10只用开发集，D17冻结方案后才使用保留集比较，不据保留结果调参。

开发收尾检查通过：13个提交候选（9个修改、4个新增）、2份Python AST、85行JSONL及划分JSON、25份Markdown围栏、58张表格列数、172处本地链接及59处锚点、D8固定8项报告。35日逐日安排与计划第10节之外内容未变；队列40题与标签ID/顺序一致，原20条字节及复核信息保留，新增20条复核者/时间为空。只读实现和分族语义审查未发现阻断项；暂存区为空，.env及运行数据仍被忽略，git diff --check无输出。未为文档收尾追加模型调用、服务或旧业务测试。

### 用户Git交接与手动验证（2026-10-18补记）

用户已亲自提交并推送 [d5c782ec656ff7a5cf81ccb0de00b97bebd45f12](https://github.com/yan-ye-dhy/project-copilot/commit/d5c782ec656ff7a5cf81ccb0de00b97bebd45f12)，`feat(evals): add family splits and dataset freezing`；push显示92513a7..d5c782e main→main，Codex只读核实本地HEAD/origin/main一致、工作区干净，D8阶段A完成。上文待Git及pending状态保留为开发期快照，当前手动结果以本补记为准。

用户主动开启D8手动验证，本人运行`uv run --frozen python -m evals.freeze_dataset --dataset evals/datasets/copilot_v0`和`echo $?`，实际40题/23族、30开发/10保留、35可答/5无答案、confirmed20/pending20，ready_for_freeze=false，退出0。随后本人添加`--freeze`，得到“标签尚未全部人工确认，不能正式冻结”，退出1；Codex只读确认没有生成frozen_manifest.json。两项结果验证预检及人工复核门禁，由用户操作，不记为Codex代做。

人工核对环节以034的三条跨段证据及001/034同属configuration-lifecycle、同在dev为示例，指导核对新增021–040的答案支持、036–038无答案及039–040固定追问。用户随后明确回复“021–040已核对，全部通过”；据此仅将新20题review_status登记confirmed、reviewed_by登记yanye、reviewed_at统一为批量登记时间2026-10-18T21:41:27+08:00。原20题复核记录不变，没有要求逐题书面理由或伪造单题时间。

同步队列的20处状态和已确认内容标识，问题/答案/证据、来源/corpus及划分未改。Codex自动核对标签仅三字段变化、原D7行字节不变，登记后validate_split通过：confirmed40/pending0、30/10、ready_for_freeze=true；此为登记完整性检查，不替代人工判断或用户实际冻结。当时尚未创建正式冻结，随后由用户亲自执行，见下方记录。

用户全量确认后再次运行`uv run --frozen python -m evals.freeze_dataset --dataset evals/datasets/copilot_v0 --freeze`及`echo $?`，实际status=ok、state=frozen、dev30/holdout10、退出0，生成正式frozen_manifest.json；清单时间2026-10-18T13:51:42.739282+00:00（北京时间21:51:42.739282）。随后本人将末尾改为--verify运行，实际state=verified、退出0，两次总指纹均为dataset-v1-54806b5174b2f3d2e4bd08b91b57569c5121929731112c3d375d4d42d4306b7e。正式9份输入为sources/questions/labels/split与5份corpus；这不是临时合成夹具的冻结。

用户另按指导使用shutil.copytree将整个目录复制到data/manual-d8/tampered，只给副本第一题reference_answer追加“（副本测试改动）”，打印副本路径及修改后答案。本人对副本执行`uv run --frozen python -m evals.freeze_dataset --dataset data/manual-d8/tampered --verify`及echo退出码，实际提示“冻结文件指纹或划分版本不匹配；不得沿用旧实验身份”、退出1，验证合法JSON内容变更也不能沿用原冻结。没有在正式数据中改答案或重建冻结，也没有手动测试重复freeze防覆盖或其余自动边界。

Codex随后只读比对正式9份输入哈希全部匹配，副本仅第一题参考答案不同、其余字段/输入和冻结清单一致；原D7的20行标签字节保留，新20题仅三复核字段变化，questions/sources/split及5份corpus与开发提交不变。测试副本被Git忽略，不能进入语料或本次提交。本次只补数据复核元信息、正式清单及文档，未重跑业务测试或新增模型调用。

用户最后明确回复“已学习，能够解释”，确认已通过ChatGPT学完D8，能解释同族同侧防泄漏、预检/冻结/verify的区别，以及合法答案内容变化为何仍使冻结校验失败。结合已推送开发提交和本人实际操作，D8阶段A/B均完成；收尾记录待用户亲自提交/推送，不自动进入D9。

手测收尾检查通过：10个文件（9修改、1新增正式冻结清单），85行JSONL、25份Markdown围栏、59张表格列数、176处本地链接与59处锚点、D8固定8项报告；计划只更新第10节，35日任务安排不变。正式9文件哈希及总指纹复算一致，队列40题与标签一致，原D7行保留、新20题仅三复核字段变化；独立只读事实审核通过。git diff --check无输出，暂存区为空，.env与data/manual-d8副本仍被忽略；未追加业务测试、模型调用或Git写操作。

用户亲自提交并推送 [056eff8fbab6506915875144827fd8dd2ffea539](https://github.com/yan-ye-dhy/project-copilot/commit/056eff8fbab6506915875144827fd8dd2ffea539)，`docs(d8): record manual validation and frozen dataset`；push显示d5c782e..056eff8 main→main，Codex核实HEAD/origin/main一致、工作区干净，D8收尾同步完成。用户随后明确开启D9。

## D9：最小离线排名评测器（2026-10-18）

**当前状态：D9阶段A/B均完成，本次学习与验证收尾文档待用户提交/推送。** 开发提交0aeadef已同步；2026-10-19用户完成重放、013核算及学习确认，剩余检查按其明确委托由Codex补验，详见本节末尾。开发从干净main/056eff8开始，只实现D9；没有改D3切分、D8冻结数据、业务源码或依赖，没有运行真实Dense基线、模型调用、服务或保留集检索。

### 实际实现与边界

evals/metrics.py复用D3 loader/splitter，将完整chunk在同页原文中唯一定位为Unicode字符[start,end)；重复文本有歧义则报错，不猜第一次出现。检索片段须与canonical目录的完整text/metadata一致，未知或篡改身份不混入普通未命中。gold按文档版本/页的原文位置判断：单片段或多个片段连续无缺口拼全并逐字匹配整个quote才计unit命中，记录首次完整覆盖的原排名；仅字符相交不计命中。这是保守原文覆盖，不是语义裁判或答案事实正确性评测。

文档Recall按gold文档去重，文档MRR取首个相关chunk原排名；证据Recall按独立unit计，证据MRR取首个完整unit完成的前缀排名，另报告all_evidence_covered。重复结果占原排名但不增覆盖；无答案各项null且不进召回分母，检索失败按空排名计0留在可回答分母。

evals/run.py只重放离线排名，先verify冻结/配置指纹，再检查dev选择、query、全部快照/片段/耗时/错误，验证后才创建新目录。question_ids=null表示全30开发题，明确列表表示子集；缺题/重复/额外题、保留集及覆盖输出均拒绝。追问使用冻结standalone_question。保存config.json、retrieval.jsonl、summary.json，包括代码commit/dirty、配置与快照SHA256、数据/切分指纹、依赖平台、原排名/分数/原文/错误及指标；阶段耗时来自快照，未采集null，本地单调时钟计算经过时间另记evaluation_ms，不冒充检索延迟或CPU time。p95采用nearest-rank并报告有效样本数。

参考原tests/fakes/rag.py的固定返回/依赖隔离，核心逻辑自主实现。配置及4题样例明确为synthetic_fixture，既没有用Embedding排序，也没有真实查询Qdrant；recorded_retrieval仅是外部排名来源声明，D10还需真实模型/索引清单/缓存/Prompt及运行记录，不将当前收据冒充完整基线采集。

### 实际验证

以下为2026-10-18开发期记录，执行者Codex，工作目录新项目；2026-10-19的用户操作与委托补验另列。

| 实际命令/检查 | 实际结果 |
| --- | --- |
| `.venv/bin/python -m evals.freeze_dataset --dataset evals/datasets/copilot_v0 --verify`，D9开始时 | 退出0，D8正式冻结verified，指纹54806b…4306b7e |
| `.venv/bin/python -m pytest -q tests/test_eval_metrics.py` | 初稿36通过，补定点边界后38通过，0.10秒 |
| `.venv/bin/python -m pytest -q tests/test_eval_run.py` | 最终24通过，0.77秒；初轮断言因新增失败分类字段更新，未发现业务实现失败 |
| `.venv/bin/python -m pytest -q tests/test_eval_metrics.py tests/test_eval_run.py tests/test_eval_dataset.py tests/test_eval_freeze.py tests/test_documents.py` | **155 passed，1.70秒**；D9共62项、D7/D8/D3相关93项，无失败 |
| `.venv/bin/python -m evals.run --dataset evals/datasets/copilot_v0 --config evals/configs/d9_replay.json --rankings evals/fixtures/d9_rankings.jsonl --output-dir data/evals/d9-auto-smoke-final` | 退出0，三份结果实际保存；4个固定开发题，3可答/1无答案/1模拟错误，mode=offline_replay |
| 逐字节/哈希检查冻结输入与收尾commit | 9份登记输入及frozen_manifest共10份文件均未改，与HEAD一致；4条结果及配置/快照哈希正确 |

固定样例001在rank2命中、rank3重复；013在rank1仅文档相关、rank2含全部gold；017无答案空结果；023模拟timeout但没有真实请求。因此@3文档Recall2/3、MRR1/2，证据Recall2/3、MRR1/3，完整覆盖2/3，与手算一致。无答案排除，模拟错误保留在3道可答分母内。阶段timings全部samples0/median与p95为null，refusal_accuracy=null；不将这些数字写成真实Dense效果或30题全量评测。

首次smoke保存在data/evals/d9-auto-smoke；修正本地经过时间的表述后，用新目录d9-auto-smoke-final运行最终版本，没有覆盖旧结果。两个目录均为固定样例重放、无网络或模型费用；测试中的时间值也只属于合成夹具。最终四条逐题结果保留本地计算经过时间，不作为任何检索性能结论。没有为纯评测工具重跑全部旧应用测试。

### 交接与下一步

学习入口及字段说明见 [evals/README.md](../evals/README.md)，固定8项报告见 [DAY_9.md](reports/DAY_9.md)。开发期交接时Git与学习/手动验证尚待用户操作，以下补记实际结果；不自动进入D10或发起收费索引/Embedding。

收尾检查通过：13个候选文件（5修改/8新增）、4份Python AST、27份Markdown围栏、65张表格列数、193处本地链接及60处锚点、4行固定排名JSONL、配置JSON及D9固定8项报告。计划仅第10节变化，35日任务表不变；D8全部10份冻结相关文件字节未改，业务源码/依赖无diff。独立只读实现与事实审核通过，git diff --check无输出，暂存区为空，.env及data/evals运行结果被忽略；未追加模型、服务或Git写操作。

### 用户Git同步、学习及验证补记（2026-10-19）

用户已亲自提交并推送 [0aeadef3bd84e66649d72a4358c10cbe62f9f7fe](https://github.com/yan-ye-dhy/project-copilot/commit/0aeadef3bd84e66649d72a4358c10cbe62f9f7fe)，`feat(evals): add offline retrieval metrics and replay runner`；push显示056eff8..0aeadef main→main。Codex只读核实HEAD/origin/main一致，开始手动验证时工作区干净。

用户主动开启D9手动验证，亲自执行：

```bash
uv run --frozen python -m evals.run \
  --dataset evals/datasets/copilot_v0 \
  --config evals/configs/d9_replay.json \
  --rankings evals/fixtures/d9_rankings.jsonl \
  --output-dir data/manual-d9/replay-01
echo $?
```

实际status=ok、退出0，保存config.json/retrieval.jsonl/summary.json；4题含3可答、1无答案及1模拟检索失败。Codex只读核对文件，收据代码为0aeadef完整提交、dirty=false、model_calls=0，canonical目录16片段，输入SHA256与固定快照一致。用户对013亲自给出@1文档Recall=1/证据Recall=0、@3证据Recall=1/MRR=0.5，符合两条gold在第2个片段全部覆盖的结果。

用户随后明确要求“你直接验证吧”，将剩余核算和门禁委托Codex；下列结果不登记为用户亲自操作：

| 执行者 | 实际检查 | 结果 |
| --- | --- | --- |
| Codex，用户本次委托 | 读取原4条结果独立汇总，检查无答案及模拟失败分母 | 证据Recall@3=(1+1+0)/3=2/3，MRR@3=(1/2+1/2+0)/3=1/3；023失败仍在分母3，017指标null并单列 |
| Codex，用户本次委托 | 内存合成abcd/defg/ghij及gold cdefgh，调用指标函数 | @1/@2证据Recall为0，@3为1；首次完整覆盖rank3、MRR=1/3；未新增正式gold |
| Codex，用户本次委托 | `.venv/bin/python -m evals.run`，副本排名只删除失败题023，其余使用原配置 | 退出1；拒绝缺题，未创建结果目录 |
| Codex，用户本次委托 | 同一CLI，副本配置只改split=holdout，其余使用原排名 | 退出1；保留集门禁拒绝，未创建结果目录，未计算保留集效果 |
| Codex，用户本次委托 | 同一CLI，原输入及`--output-dir data/manual-d9/replay-01` | 退出1；拒绝覆盖，原三份结果逐字节未变 |

三项CLI均返回“评测输入或输出校验失败；请检查冻结、配置和排名快照”。完整实际命令、输入副本及结果保存在被忽略的`data/manual-d9/codex-verify-0i0rmfd1/verification.json`；正式9份冻结输入及清单共10文件哈希与HEAD字节均未变。阶段耗时仍全部未采集，refusal_accuracy=null；固定4题不代表全部30开发题或真实Dense成绩。本轮未重跑pytest、启动服务或调用模型，Codex未进行Git写操作。

用户最后明确确认“已完成，理解主流程”，确认已通过ChatGPT完成D9代码学习。结合本人重放和013核算、明确委托的剩余补验及已同步开发提交，D9阶段A/B均完成；本次仅补记六份文档，待用户亲自提交/推送，等待用户要求才进入D10。

本次文档检查通过：28份已跟踪Markdown的围栏、67张表格、196处本地链接及61处锚点，D9报告固定8项；计划仅第10节进度变化，34行主任务与独立D35弹性节保持原样。六份文档diff及git diff --check通过，暂存区为空，无未忽略的新文件；.env、原重放及补验记录仍被忽略。正式冻结10文件与HEAD字节一致；独立只读复核通过，README首页状态已同步修正。

用户随后亲自提交并推送 [91eea87624778ec88ff71ee65c0e129b1dc8de22](https://github.com/yan-ye-dhy/project-copilot/commit/91eea87624778ec88ff71ee65c0e129b1dc8de22)，`docs(d9): record verification and learning completion`；push显示0aeadef..91eea87 main→main。Codex只读核实HEAD/origin/main一致、工作区干净，D9收尾同步完成，用户明确开启D10。

## D10：真实Dense基线与配置冻结（2026-10-19）

**当前状态：D10阶段A/B均完成，学习与手动验证收尾记录待用户提交/推送。** 开发12226d7已同步；2026-10-20用户完成机器完整性、离线重评分、缓存复用/Prompt副本拒绝、八题人工核验及学习理解确认，详见本节末尾。从干净main/91eea87开始，仅实施D10；下文初期环境/费用/人工pending保留为历史快照，不自动进入D11。

### 实现范围与参考

新增evals/cache.py按供应商URL/模型/维度/float/原文SHA持久复用向量；按唯一文本和明确batch分批，原子保存并校验内容哈希，损坏时停止，不自动重收费。原文与密钥不进入缓存文件，完整问题向量另存被忽略的运行目录。仅顺序单进程，原始客户端由外层关闭。

evals/baseline_config.py和configs/dense_v0.json冻结完整System/User模板/模板哈希/变量、Zero-shot无示例、6000字符整块预算/排序/去重/编号、Pydantic Schema及哈希、模型URL/生成参数/关闭thinking/JSON模式、Embedding/索引/切分配置，运行前对照实际代码和设置。配置指纹为baseline-config-v1-2e43988981644b482c5917093586b25e406e5158452e84a0478a3326d793bea1；模型别名可能更新，真实响应模型与日期仍须记录。

evals/baseline.py复用D4 IngestionService/ManifestStore/DenseQdrantStore、D5 RagAnswerer和D9 run_evaluation/evaluate_ranking，自主编排数据采集，不复制旧项目实现或导入旧包。正式dev30题、26可答/4无答案，独立pc_eval_dense_v0集合；默认全16片段预取，2题smoke通过后才嵌入余28题。每题保存实际query、排名/原文/分数/耗时或错误，失败留分母；按D9规则评分。批量Embedding准备耗时单列，逐题Embedding计时只表示本地向量缓存读取，不能冒充冷API耗时。

预先按每类首题固定001/010/002/003/009/013/017/020八道dev回答样本；仅复用本次排名前5条，不再次检索，保存消息、原始content、实际模型/usage、结构校验结果或失败。review.jsonl的claims/支持/拒答判断pending，不因程序通过自动确认。机器产物manifest记录字节哈希、代码/dirty、日期/平台、索引清单、缓存条件及实际调用数；人工review不纳入机器哈希，verify不等于人工质量验收。

### 实际离线验证与费用准备

Codex在新项目运行：缓存59项通过（0.20秒）、配置39项通过（1.14秒）；采集编排初轮13通过/1失败，发现cold批次数误写死为3，修正为唯一文本分批计算后，连同D9/D8/D7/D3相关回归共267项通过（5.07秒），见 [D10报告](reports/DAY_10.md)。配置初轮一处测试类别预期与冻结题型不符，已修正断言；没有改数据迁就测试。测试全为fake/offline，不冒充真实效果或用户手动操作。

实际plan命令`.venv/bin/python -m evals.baseline plan --dataset evals/datasets/copilot_v0 --config evals/configs/dense_v0.json`退出0，5文档/16片段/30问题、10330片段字符/1663问题字符、默认冷缓存3批/最多8次回答、model_calls=0。本地只输出“密钥已配置”布尔值和参数一致结果，未输出密钥或执行收费调用。

2026-10-19核实[智谱官方定价](https://docs.bigmodel.cn/cn/guide/start/pricing)Embedding-3标准￥0.5/百万tokens（同步数组不是Batch API折扣）；[DeepSeek官方定价](https://api-docs.deepseek.com/quick_start/pricing/)flash峰值未命中输入$0.30/百万、输出$1.20/百万。按Embedding总25k tokens、八次回答各12k输入/1024输出估算￥0.0125＋US$0.0386304，非账单或硬额度。已按计划8.3请求本轮最多3次Embedding/8次回答授权，尚待明确回复；SDK自动重试0。

Docker CLI当前报Input/output error，沙箱本机HTTP检查受限，不能据此断定Qdrant已停；已异步请用户恢复Docker Desktop和WSL集成。真实执行、环境核实及产物报告后续按实际结果补记；本次不改业务源码、依赖、D8冻结数据或35日任务表，不暂存/提交/推送Git。


### 环境恢复与真实基线补记

用户先回复“继续，wsl已经打开”，随后在已说明的3次Embedding＋最多8次回答/费用范围后要求“继续完成”；按该次指令继续本轮范围，不追加重试调用。只读检查受限沙箱Docker socket报permission denied，经正常权限提升后docker ps可读。Codex用现有Compose启动Qdrant；环境恢复后容器曾退出255，重新启动时短暂连接重置，随后readyz HTTP200、服务1.19.2、独立评测集合尚不存在，才发模型请求。期间未删除卷、旧集合或数据。

最终离线命令仍为上述八个测试文件组合，实际 **269 passed，5.35秒**；补充了verify必备产物/完整路径集合与status门禁，采集文件单独测试 **16 passed，7.70秒**。源码收据补记SDK依赖版本、16份相关源码SHA256；之后没有改动这些已记录源码。

实际运行`.venv/bin/python -m evals.baseline collect --dataset evals/datasets/copilot_v0 --config evals/configs/dense_v0.json --output-dir data/evals/dense-v0-20261019-01 --cache-dir data/evals/embedding-cache --live --answers`退出0，2026-10-19 19:07:20（UTC+8）完成。只发3次Embedding（16/2/28文本）和8次DeepSeek，无自动重试；30道dev检索无错误、26可答/4无答案，原始问题/独立追问、排名/分数/片段/耗时/错误完整保存。Qdrant独立集合dense/Cosine/2048维、精确16点，原应用集合仍2点。

真实证据Recall@1=0.666667、@3=0.858974、@5与@10=1.0；证据MRR@5=0.836538，文档Recall@5=1.0。pc-d7-003、pc-d7-012、pc-d8-034、pc-d8-039在@3证据不完整、@5恢复；这不是优化对照或保留集成绩，小型自建语料不能证明通用泛化。30题向量已在本地query_vectors保存，另用禁止发请求的delegate逐值核对全部缓存命中，hit30/miss0/api_calls0；没有重新索引或生成回答来证明复用。

八类固定回答中7条结构/引用编号通过，pc-d7-003返回的citation_numbers键少开头双引号，json.loads在字符153抛JSONDecodeError；模型原文、消息及usage保留，未修补、重试或修改Prompt，作为V0生成失败。pc-d7-017输出一次insufficient_evidence，仅记录输出，不据此代替人工拒答判断或声称4题拒答正确。八题review均pending，人工声明/支持率/拒答正确率未填。

DeepSeek八次合计prompt17277/completion687/total17964 tokens（含非法JSON），服务端缓存命中0；按官方峰值未命中价折算约US$0.0060075，仅估算。Embedding usage及实际账单未采集保持null。逐题缓存读向量median0.725/p950.845ms、实际search median5.090/p956.735ms、含记录/本地校验total median6.489/p958.142ms；均30样本，三批准备耗时1168.07/246.14/460.94ms单列，不伪造冷API逐题延迟。

完整本地12份机器产物通过baseline verify，human_review=pending/day10_accepted=false；运行时commit91eea87、dirty=true如实保留，16份源码SHA支持后续核对。公开快照`evals/results/dense-v0-20261019-01/`包含14份非敏感文件（12份机器/派生文件哈希、1份归档清单、1份待人工review）；完整query_vectors和重复本地清单只留data，公开保留它们的SHA及原完整run_manifest。原始采集没有被覆盖，当前用户Git提交/推送待执行，阶段B待学习与主动验证；完整边界见 [V0报告](../evals/reports/DENSE_V0.md)。

收尾实际检查通过：29候选文件（6修改/23新增）、6份Python AST、30份Markdown围栏、72张表格、223处本地链接/64处锚点、全部新JSON/JSONL及日报8项；新文件尾空白/结尾换行通过。公开12份机器/派生文件SHA和运行16份源码SHA匹配，完整本地verify通过。另用公开evaluation_config/rankings在`data/evals/dense-v0-20261019-public-replay-check`离线重评分，指标、分母、错误及阶段耗时与原汇总逐值一致，model_calls=0。正式D8冻结10文件与HEAD字节一致，业务src/D9指标/依赖无diff，计划仅第10节变化；未发现实际密钥出现在候选文件，.env/data及向量缓存仍被忽略，git diff --check通过，暂存区为空。用户学习/手测与人工支持判断没有自动确认，Codex未进行Git写操作。


### 用户Git同步、学习与手动验证补记（2026-10-20）

用户亲自提交并推送 [12226d7b2c52b14e1d853288f508a8aff35cc2f6](https://github.com/yan-ye-dhy/project-copilot/commit/12226d7b2c52b14e1d853288f508a8aff35cc2f6)，`feat(evals): add dense V0 baseline and frozen configuration`；push显示91eea87..12226d7 main→main。Codex只读核实HEAD/origin/main一致、工作区干净，开发阶段A完成；当时学习/手测尚待用户主动开启。

2026-10-20用户明确开启D10手动验证，以下操作由用户本人执行，Codex只读核对其输出/文件，不把开发期269项自动测试改记为用户操作：

| 实际操作 | 用户反馈与Codex核对 |
| --- | --- |
| `uv run --frozen python -m evals.baseline verify --dataset evals/datasets/copilot_v0 --output-dir data/evals/dense-v0-20261019-01`及`echo $?` | verified、12文件，human_review=pending/day10_accepted=false，退出0；此处是原采集快照的机器核验 |
| `uv run --frozen python -m evals.run --dataset evals/datasets/copilot_v0 --config evals/results/dense-v0-20261019-01/evaluation_config.json --rankings evals/results/dense-v0-20261019-01/rankings.jsonl --output-dir data/manual-d10/replay-01` | 用户反馈“已运行”；Codex读取三份输出，30题/26可答/4无答案/错误0，逐题除本地evaluation_ms外同原结果，所有汇总/耗时一致，model_calls=0，代码12226d7/dirty=false |
| 纯读缓存，CacheOnly.embed_documents仅抛错，CachedEmbeddings加载已保存30道query并assert逐值相等 | 用户输出“向量逐值一致：30”，hit_texts30/miss_texts0/api_calls0，说明实际复用同一向量而未重新请求 |
| 复制配置到`data/manual-d10/changed-prompt.json`，只追加System消息，再运行`uv run --frozen python -m evals.baseline plan --dataset evals/datasets/copilot_v0 --config data/manual-d10/changed-prompt.json`及`echo $?` | status=error、退出1，按预期拒绝Prompt变化；没有修改正式配置或执行模型请求 |

随后Codex整理八题事实/引用核对表，用户实际核对后明确回复：“001、010、002、009、013、020事实与引用支持通过；003保留结构失败；017拒答正确。”按该反馈登记8行review confirmed，reviewed_by=yanye，登记时间2026-10-20T18:44:18+08:00。六条有效回答拆分24项需证据声明，23项直接支持，009的“用户负责管理自己样本”1项注明为用户接受的合理推论；每项保留该次有效引用编号、chunk_id及原文子串，不能用未被用户接受的AI判定代替确认。

人工主结果为预选八题端到端7/8（87.5%）：六条有效事实回答＋一次正确拒答；003的answer=null、非法JSON原文和错误保留，继续作为8题失败。局部声明支持24/24仅适用六条有效回答，不含003无有效回答/引用映射或017纯拒答，也不能当作8/8或30题回答正确率。拒答正确1/1仅017，支持率N/A，不外推开发集全部4道无答案题。所有需证据声明均记录，若缺引用同样须记未支持；本次有效六条未发现此类项。

用户另明确确认“已学习，能够解释”，涵盖真实采集→向量缓存→Qdrant排名→离线评分→回答契约及003仍为失败的原因。结合开发已同步、本人手动验证与人工核对，D10阶段A/B均完成；本次九份收尾文件待用户亲自提交/推送，未开启D11。

本次仅更新八份文档和公开review.jsonl，本地data/evals下被忽略的review同步登记。原始answers/rankings/Prompt、公开12份机器/派生产物、archive_manifest/status/run_manifest及D8冻结文件保持原字节；它们的pending/day10_accepted=false表示采集时状态，当前人工结论读取独立review及基线报告。没有为显示完成改写机器快照，没有重跑pytest、采集、Docker或模型调用，Git写操作仍由用户执行。

收尾实际检查通过九文件、8行review/24项声明，所有引用编号/来源/chunk_id及原文子串正确；公开12机器/派生文件及归档清单与HEAD原字节一致，本地12机器文件、运行16源码SHA及D8冻结10文件均匹配。30份Markdown围栏、75张表格、225处本地链接/64锚点及固定8项报告通过，计划仅第10节、34主任务＋1弹性日不变。独立只读事实/状态复核通过，git diff --check无输出，暂存区为空，无未忽略的新文件；.env、手动副本及本地review保持被忽略，检查新增模型调用0。

### D10收尾同步核实（2026-10-20）

用户亲自提交并推送 [1b69fbefd83c0998dfaf0976b91f44a7f484d274](https://github.com/yan-ye-dhy/project-copilot/commit/1b69fbefd83c0998dfaf0976b91f44a7f484d274)，`docs(d10): record manual validation and answer reviews`。开启D11时Codex只读核实main/origin/main均指向此提交，工作区干净；D10阶段A/B及收尾同步均完成，下文才按用户明确请求开始D11。

## D11：单因素切分候选与离线边界（2026-10-20）

**最终状态：D11阶段A/B均完成。** 开发提交d53fe15已由用户推送；2026-10-21用户完成四项手动验证并确认已通过ChatGPT学习、能解释关键区别，本次收尾文档待用户提交。下文保留开发期和后续学习验收的实际记录，D12仍待用户明确开启。

从V0真实dev的003/012/034/039四条检索Bad Case提出size750→500的单一假设，保留overlap150、分隔符、Dense算法及冻结Prompt/模型合同。原四题@3证据不完整、@5恢复；不是遗漏gold，也不能认定size为确定原因。003的非法JSON生成失败单独保留，未作为本日修复目标。

新增`evals/configs/chunk_size500.json`、`evals/chunking.py`、两份边界/输入测试、离线`inspection.json`和 [候选报告](../evals/reports/CHUNK_SIZE500_PREFLIGHT.md)。复用D3现有SplitConfig/split_document及D9 build_chunk_catalog，默认业务src未修改，无新增依赖。原项目只读参考递归组件及750/200/分隔符用法；候选选择、归档绑定、边界检查及测试是自主实现，没有照搬不存在的中文标题解析器。

检查器先绑定V0归档哈希与三个实际消费输入、完整基线配置指纹/D8冻结，再限制关注题属于dev且来自真实检索Bad Case；按同源文档重建两个原文位置目录，输出六段gold及相交/单块完整包含的片段。不读取.env、不写数据或创建模型/存储客户端；输出中候选retrieval_evaluation/answer_evaluation均为null，原V0完成rank单独标注，不能将旧排名套给新chunk。

| 实际自动命令/检查（执行者Codex，新项目Python3.13） | 结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_chunking_inspection.py tests/test_chunking_boundaries.py` | 25 passed，3.13秒；覆盖单因素/归档/冻结/dev门禁与中文/Markdown边界 |
| 上述两文件加`tests/test_documents.py tests/test_eval_metrics.py tests/test_eval_run.py tests/test_baseline_config.py tests/test_baseline.py tests/test_eval_cache.py tests/test_eval_freeze.py tests/test_eval_dataset.py`，同一pytest命令 | **294 passed，8.19秒**；D11新增25项，相关旧验证269项 |
| `.venv/bin/python -m evals.chunking --dataset evals/datasets/copilot_v0 --baseline-dir evals/results/dense-v0-20261019-01 --candidate evals/configs/chunk_size500.json`，stdout保存到`evals/results/chunk-size500-preflight/inspection.json` | 退出0；原文9331字不变，16→27块、累计片段字符10330→10389，四题六段gold仍各自单块完整包含；model_calls=0、qdrant_requests=0 |

离线检查中003/012/039承载片段682/698/678→385/386/431字符；034仍需跨文档，两份来源的承载块609/643→389/499。小块可能聚焦也可能碎片化；全27块ID改变，1条相同文本具备同模型缓存复用条件，26条新文本共10129字符，是否命中其他缓存/Token/费用未采集。点数+68.75%不能当费用同比增长；150重叠预算也不保证每对段实际重叠150字，测试保留中文截句、标题分离及520字gold需多块等限制。

独立只读代码复核未发现阻塞问题，另执行25项相关测试通过；主记录以上述实际命令为准。D11没有候选向量、排名、Recall/MRR、回答、延迟或费用结果，没有Qdrant/模型请求、业务/Prompt/标签变更。Git提交、ChatGPT学习和手动验证均未代用户完成，完整8项交接见 [D11报告](reports/DAY_11.md)。D12仍需本日A/B完成和用户明确开启，再遵守费用授权、复用30个问题原向量及固定k/Prompt等条件做真实对照。

收尾核实13个改动文件（6修改/7新增），空白/AST/JSON、8份Markdown围栏、48张表格、172处本地链接/12处锚点及8项报告通过。离线快照重算一致；V0公开12产物/16运行源码哈希一致，D8冻结10文件与HEAD字节一致且verify通过。计划第10节外不变，无业务/依赖diff，git diff --check通过，暂存区为空，常见凭据格式检查通过且.env/data仍被忽略；未读取真实密钥。

### 用户Git同步、学习与手动验证补记（2026-10-21）

用户亲自提交并推送 [d53fe15fc0101e893076fe8dc8595d7a091eb605](https://github.com/yan-ye-dhy/project-copilot/commit/d53fe15fc0101e893076fe8dc8595d7a091eb605)，`feat(evals): add single-factor chunking candidate and boundary checks`，push显示1b69fbe..d53fe15 main→main。Codex已核实开发同步；2026-10-21手测开始时再次只读确认main/origin/main一致、工作区干净，阶段A完成。

用户主动开启D11手动验证，逐环节本人操作与反馈如下；Codex仅读取结果文件或核对反馈，未代用户运行检查器，也未重跑开发期294项测试：

| 用户实际操作 | 反馈与Codex核对 |
| --- | --- |
| 创建data/manual-d11，执行`uv run --frozen python -m evals.chunking --dataset evals/datasets/copilot_v0 --baseline-dir evals/results/dense-v0-20261019-01 --candidate evals/configs/chunk_size500.json > data/manual-d11/inspection.json`及`echo $?` | 退出0；Codex读取JSON，与开发快照逐值一致，16→27块、原文9331字符、模型/Qdrant调用0、效果评估null |
| 在JSON中观察003题的gold及两种承载块，反馈“已看到” | gold`[943,1056)`不变，承载块由`[499,1181)`682字符变为`[796,1181)`385字符，完整原文均匹配；V0完成rank4不能套作候选排名 |
| 运行带练内联Python，创建甲/乙/丙各300字、双换行分隔的paragraphs.txt，按500/150切分并打印位置 | 用户输出长度300/302/302，范围`[0,300)`、`[300,602)`、`[602,904)`，相邻实际重叠0；Codex只读确认904字符样本与命令一致，2字符差来自保留换行 |
| 读取正式候选后另存two-factors.json副本，只改overlap150→100，再用检查器运行该副本 | 用户输出明确双因素拒绝、退出1；副本size仍500，Codex核对仅overlap不同，正式配置未修改 |

用户随后明确确认“已学习，能够解释”，能说明冻结校验→两种切分→原文位置→边界JSON、overlap预算，以及边界完整包含gold不等于检索改善。结合已推送开发提交与实际四项手测，D11阶段A/B均完成。未使用测试通过自动推定个人掌握；D12尚未开启，候选Recall/MRR、回答与延迟仍待真实对照。

本次只补六份Markdown收尾文档；D8冻结10文件、D10公开12机器产物/16运行源码及D11正式配置/检查器/边界快照按字节或SHA保持一致。三个手动样本/结果文件均被Git忽略，没有覆盖机器归档、Prompt或gold；未读取.env、运行服务、模型、真实采集或pytest，Git写操作仍由用户亲自完成。

本次收尾检查通过六份Markdown围栏/空白、42张表、158处本地链接/12处锚点、固定8项报告与独立只读事实复核。计划仅第10节变化，git diff --check无输出、暂存区为空，无未忽略新文件；本次收尾提交和推送待用户亲自执行。

### D11收尾同步核实（2026-10-21）

用户亲自提交并推送 [559190f13583765dd3ffbec59c435845fe198737](https://github.com/yan-ye-dhy/project-copilot/commit/559190f13583765dd3ffbec59c435845fe198737)，`docs(d11): record manual validation and learning completion`；Codex只读核实main/origin/main相同、工作区干净。D11阶段A/B和收尾同步均完成，随后按用户明确请求开启D12。

## D12：V0与单因素切分候选的真实对照（2026-10-21）

**最终状态：D12阶段A/B均完成。** 开发提交3bb656f已由用户推送；2026-10-22用户完成四项离线手动验证、012/003原文核对并确认ChatGPT学习理解，本次收尾文档待用户提交。下文保留开发期与后续学习验收的实际记录，D13仍待用户明确开启。

新增`evals/chunking_run.py`、`evals/comparison.py`和两份测试，分别负责plan/显式live/verify、离线成对统计；复用D3原文切分、D4清单入库、D9评分和D10缓存/合同。使用独立候选集合pc_eval_dense_v1_size500，原V0必须存在且16点/payload一致，不能自动创建空基线；候选/输出既有则拒绝，不删除数据。业务src、依赖与锁文件不变。数据/原源码/归档→历史30问题向量哈希链、数值/模型合同和32段dev gold完整映射均在请求前核验；新编排与统计为自主实现。

| 实际命令/检查（Codex，新项目环境） | 结果 |
| --- | --- |
| `.venv/bin/python -m evals.chunking_run plan --dataset evals/datasets/copilot_v0 --baseline-dir evals/results/dense-v0-20261019-01 --baseline-private-dir data/evals/dense-v0-20261019-01 --candidate evals/configs/chunk_size500.json --cache-dir data/evals/embedding-cache` | 退出0，5文档16→27块、30原问题向量；初始1命中/26缺失/10129字符，1次预计Embedding、60本地查询，plan模型调用0 |
| `.venv/bin/python -m pytest -q tests/test_chunking_run.py tests/test_eval_comparison.py tests/test_chunking_inspection.py tests/test_chunking_boundaries.py tests/test_baseline_config.py tests/test_baseline.py tests/test_eval_cache.py tests/test_eval_metrics.py tests/test_eval_run.py tests/test_eval_freeze.py tests/test_eval_dataset.py tests/test_documents.py` | **333 passed，9.74秒**；D12新增39项，相关旧294项。合成向量/内存Qdrant不是供应商效果 |
| Docker/运行时预检 | 初期docker退出126 I/O错误，用户恢复Desktop/WSL后沙箱socket受限，经环境权限审核只读检查并启动现有服务；Qdrant1.19.2，原V0点16、应用示例点2匹配，候选尚不存在；模型白名单符合原合同，仅输出密钥是否配置 |
| `.venv/bin/python -m evals.chunking_run collect`，同plan的5个路径参数，再加`--output-dir data/evals/dense-v1-size500-20261021-01 --live --max-new-texts 26` | 退出0，实际1次Embedding/26新文本，复用30问题原向量，60次交错Qdrant查询，两侧错误0，问题Embedding/聊天0，无自动重试 |
| `.venv/bin/python -m evals.chunking_run verify --dataset evals/datasets/copilot_v0 --output-dir data/evals/dense-v1-size500-20261021-01`及对应公开归档路径 | 均verified、19份封存产物；manual_review=pending、day12_accepted=false为本次自动采集快照 |

离线初轮328项通过；独立审查和测试随后补齐历史源码哈希与全部gold付费前映射两项保护，另补清单漏文件、V0缺失/错误payload及费用上限拒绝，最终上述333项全绿后才实调。未将程序检查替代人工gold/学习确认。

用户明确同意“这1次Embedding及60次本地检索”后才运行真实采集，2026-10-21T08:55:34.385402+00:00完成（UTC+8为16:55）。初始1条缓存命中/26缺失，批向量1200.433ms；入库重用27条，最终累计hit28/miss26/api1，命中次数非独立原文数。问题原向量SHA绑定，未重新Embedding，未调用DeepSeek。按本日[官方价格](https://docs.bigmodel.cn/cn/guide/models/embedding/embedding-3)0.5元/百万tokens，以3万输入tokens保守预算约¥0.015，实际Embedding usage/账单未采集，保持null。

真实26道可答题6改善/5退化/15不变，mixed0；4无答案两侧均非空命中，未生成答案，拒答率null。证据Recall@1为0.666667→0.769231，@3为0.858974→0.897436，@5为1.0→0.923077，@10均1.0；MRR@5为0.836538→0.858974。012原文gold完整，完成rank4→1；003更短块仍完整却4→6，007由2→6，故保留750/150默认，不能只展示上升项。V0本轮重查与历史所有逐题指标一致；机器原始文件未覆盖。

search中位4.380→4.362ms，30对差中位约−0.028ms、p95约+0.702ms，单轮/无显式预热/小语料不支持提速结论。原问题向量读校验等准备41.777ms与文档Embedding批次单列，逐题Embedding/fusion/rerank保持null；索引写入总耗时未采集。云端embedding别名跨日不能保证权重永久固定，日期/模型合同和21份源码哈希已保存。

公开`evals/results/dense-v1-size500-20261021-01/`为本地20文件原字节副本（19份机器产物＋manifest），不含问题向量/密钥，约2.94MB；两侧完整排名、评分、32段gold映射及逐题原文均保留。独立只读结果审查通过；人工学习及012/003代表改善/退化的原文映射核验仍待用户主动手测。完整交接见 [D12报告](reports/DAY_12.md)与 [实验报告](../evals/reports/CHUNKING_V1.md)，本轮不进行Git写操作或自动进入D13。

收尾检查通过32个候选文件（6修改/26新增）、4份Python AST、新JSON/JSONL、源码与文档空白；机器清单保留D4无结尾换行的原格式，不改写封存字节。8份Markdown围栏、51张表、190处本地链接/13锚点及固定8项报告通过。V0 12产物、D8冻结10文件、D11候选/快照不变，V1 19产物/21源码哈希与公私20文件字节一致。常见凭据模式检查通过，.env/data/缓存仍被忽略，计划仅第10节变化，业务/依赖无diff；git diff --check通过，暂存区为空，独立只读事实/状态复核通过。

### 用户Git同步、学习与手动验证补记（2026-10-22）

用户亲自提交并推送 [3bb656fd325db3fb0ddc5260c53d27c7fe0a6359](https://github.com/yan-ye-dhy/project-copilot/commit/3bb656fd325db3fb0ddc5260c53d27c7fe0a6359)，`feat(evals): add paired chunking experiment and V1 results`，push显示559190f..3bb656f main→main。Codex只读核实同步；本轮手测开始时再次确认main/origin/main一致、工作区干净，阶段A完成。

用户主动开启D12手动验证，以下由本人执行与反馈，Codex只读核对文件和整理阅读材料，不将333项开发期自动测试登记为用户操作：

| 用户实际操作 | 反馈与Codex核对 |
| --- | --- |
| `uv run --frozen python -m evals.chunking_run verify --dataset evals/datasets/copilot_v0 --output-dir evals/results/dense-v1-size500-20261021-01`及退出码 | verified/19文件、退出0；manual_review=pending、day12_accepted=false保留原采集快照 |
| 分别用归档中baseline/candidate_evaluation_config.json及对应rankings.jsonl执行`uv run --frozen python -m evals.run`，输出data/manual-d12/baseline-01、candidate-01 | 用户反馈0、0；Codex只读比较，两侧逐题除evaluation_ms外完全相同，汇总除run_id外相同，均30题/26可答/4无答案/错误0、评分模型调用0，代码3bb656f/dirty=false |
| 阅读Codex从封存gold/命中提取的case-review.md，明确回复“012改善、003退化的原文与排名已核对。” | 012 gold`[455,552)`不变、rank4→1；003 gold`[943,1056)`不变、rank4→6，双方实际命中保留完整quote及同一文档版本；两条代表人工核对通过，不外推全部32段 |
| 复制归档到data/manual-d12/tampered-01，仅在comparison.json末尾追加换行，再verify副本及退出码 | JSON仍合法且解析对象不变，校验按预期拒绝、退出1；Codex核对20文件仅comparison.json多一个LF，正式封存文件未变 |

用户随后明确确认“已学习，能够解释”，涵盖原问题向量复用→两侧检索→离线评分→成对比较，以及MRR上升仍保留750/150的原因。结合已推送开发提交、本人四项验证和代表原文核对，阶段A/B均完成；当前人工结论写在文档，机器pending/accepted=false及其字节哈希保留。

本轮只补8份Markdown（根README、计划第10节、日志、学习/面试笔记、D12报告、evals说明和V1报告），原始排名/合同/状态及D8/D10/D11内容不改。重评分与副本、辅助阅读材料均在被忽略的data，不读取.env、运行collect/服务/Qdrant查询、模型或pytest；新增模型调用0，Git写操作仍由用户亲自完成，D13尚未开启。

本次收尾检查通过8份Markdown空白/围栏、54张表、190处本地链接/13锚点及固定8项报告，计划第10节外不变。V1公私20文件/19产物/21源码SHA、V0公开12产物、D8冻结10文件、D11候选/快照均保持一致；独立只读事实/状态审查通过。git diff --check无输出，暂存区为空，无未忽略新增文件；仅登记012/003代表人工复核，8份收尾文档待用户亲自提交/推送。

## D13：中文BM25最小检索（2026-10-22）

**最终状态：D13阶段A/B均完成。** 用户已推送开发提交d4c5bfd，手动验证前及本次收尾前main/origin/main一致、工作区干净；同日用户完成8个手动环节并确认ChatGPT学习理解，收尾文档待用户提交。下文分别保留开发与人工验证记录，Codex未执行Git写操作，D14等待用户明确开启。

新增`rag/bm25.py`和`bm25_index.py`的tokenize/build/search入口。jieba0.42.1搜索模式关闭HMM，不用停用词/用户词典；NFKC/casefold后中文与技术词分开处理，路径、错误码、下划线及命令选项保留整体。rank-bm250.2.2提供BM25L固定k1=1.5/b=0.75/delta=0.5，适应小语料常见词，不按dev效果选参。新增两个依赖；NumPy2.5.4和其他已有锁定包不升级。

只读集合schema和成功清单精确chunk ID，不scroll未完成点、不获取向量或写Qdrant。构建验证完整ID集合、文档/来源/切分及D3原文UUID5关系，本地JSON保存原文、元数据、Dense清单、规则/词典/依赖与内容指纹；查询重建库词频并核对当前清单。文件拒覆盖，清单/依赖改变应显式重建；不接入上传自动同步或问答路由。无匹配返回空，并列按UUID稳定排序，score不等于概率/置信度。

### 实际自动验证（执行者：Codex）

| 命令/操作 | 结果与边界 |
| --- | --- |
| `uv add --cache-dir /tmp/project-copilot-uv-cache 'jieba>=0.42.1,<0.43' 'rank-bm25>=0.2.2,<0.3'` | 安装成功；首次默认缓存/网络受沙箱限制，改用/tmp并经权限审核下载，未读旧项目环境 |
| `.venv/bin/python -m pytest -q tests/test_bm25.py tests/test_bm25_cli.py tests/test_dense_pipeline.py tests/test_qdrant.py tests/test_documents.py tests/test_chunking_run.py tests/test_eval_comparison.py tests/test_baseline_config.py tests/test_eval_freeze.py tests/test_eval_dataset.py` | **297 passed，12.56秒**；其中D13新增54项核心＋15项CLI，不调用外网模型 |
| `docker compose --env-file .env.example up -d qdrant`及`curl --noproxy 127.0.0.1 --connect-timeout 2 --max-time 3 --silent --show-error http://127.0.0.1:6433/readyz` | 启动既有容器，readyz返回all shards are ready；Docker socket/本机端口经沙箱权限审核，未改Compose或卷 |
| `.venv/bin/python scripts/bm25_index.py build --manifest evals/results/dense-v0-20261019-01/index_manifest.json --output data/bm25/d13-v0.json` | 退出0；真实Qdrant的5文档/16片段完整对齐，快照在忽略目录；模型请求/集合写入0 |
| CLI固定查询`/documents/upload`、`--env-file`、`上传文件不能为空`、`unknownxyz987`，保存到data/auto-d13 | 命中数1/2/3/0，前三者首条来自upload/compose/upload。以--开头的位置参数须放在分隔符`--`之后，首次未分隔被argparse拒绝后已修正；全部查询离线 |
| 只读重取16片段与快照逐值比较 | 一致，集合仍16点；[验收收据](evidence/D13_BM25_SMOKE.json)保存来源/指纹/词项与分数，不含向量或密钥 |

复审发现BM25L在b=1且含空词项片段时产生0/0；现于构造阶段明确拒绝该组合，默认配置及正常词项的b=1仍可用，回归通过。jieba首次编译曾给出上游正则转义SyntaxWarning，实际导入及本轮测试成功，未修改第三方包。

复用D3稳定片段和D4成功清单契约；原`utils/vdb.py:13`只作稀疏分支参考，不复制全局模型或导入原包，分词/绑定/CLI为新项目自主实现。[BM25库](https://github.com/dorianbrown/rank_bm25)不负责输入预处理，[jieba](https://github.com/fxsjy/jieba)提供搜索分词，文档/查询共用规则。

本日无收费调用、重切分/向量重建/回答生成或保留集评测，未运行RRF/三路效果实验。D8数据及D10/D11/V1归档保持，V1原21源码/19产物SHA均相同；默认750/150与Dense API不变。交接见 [D13报告](reports/DAY_13.md)，D14等待本日A/B完成及用户明确要求。

开发时收尾检查：`uv lock --check --offline --cache-dir /tmp/project-copilot-uv-cache`退出0；D8 freeze verify、D10 baseline verify（原私有目录）和D12 chunking_run verify（公开目录）均通过，机器pending快照不改写既有人工完成结论。7份Markdown空白/围栏、51张表列数、195处本地链接/11锚点及固定8节报告通过；计划第10节外不变，旧日志原文完整保留。收据6份源码/依赖SHA一致；`.env`、本地快照与自动输出仍被忽略，git diff --check无输出、暂存区为空，当时14个文件待用户亲自暂存/提交/推送。只读复审已完成，发现的进度标题和历史段落归属已修正。

### 用户学习与手动验证（2026-10-22）

用户已亲自提交并推送 [d4c5bfd1ab58325dd34db16fe92900f45c9605ef](https://github.com/yan-ye-dhy/project-copilot/commit/d4c5bfd1ab58325dd34db16fe92900f45c9605ef)，输出b364f0e..d4c5bfd main→main；Codex只读核实本地与跟踪分支同指、工作区干净。用户随后主动开始D13手动验证，实际操作及8项结果见 [D13报告](reports/DAY_13.md)第5节。

用户依次完成：①混合中文/技术词tokenize，大小写规范化且embedding-3/4区分，退出0；②启动已有Qdrant并亲自curl readyz，HTTP200；③依据V0正式清单构建data/manual-d13/index.json，5文档/16片段、退出0；④路径查询命中1条上传说明；⑤中文查询命中3条，首条包含空文件提示；⑥用`-- '--env-file'`查询命中2条Compose说明；⑦未知词查询正常返回空、退出0；⑧只修改清单副本切分版本后用旧快照查询，JSON error、退出1。四次查询top_k均3，分别保存search-upload/chinese/compose/miss.json；副本与错误为manifest-changed.json/stale-manifest-error.json，全部在被忽略的手测目录。

Codex只读取用户产物并核对：原文/metadata与快照片段逐值一致，16个ID与成功清单完整对应；快照内容指纹与本次build及原开发收据一致，正式清单字节与已提交版本及原SHA相同，副本唯一语义变化为spec.splitter_version追加-manual-test。用户询问index.json结构时，结合实际文件解释了其保存原文/元数据、规则/参数和指纹，加载时再重建词频等统计。无匹配成功返回空与陈旧清单失败退出分别保留。

用户最终明确答复“已学习，能够解释”，确认已通过ChatGPT理解成功清单→Qdrant片段→统一分词→BM25快照→本地查询及清单变更拒绝原因，故阶段A/B均完成。模型调用和Qdrant写入均0，本轮Codex未代跑用户CLI、服务、pytest或模型；手测没有覆盖全角NFKC、空白问题、全量dev或回答质量，不将原自动用例扩大为本人手测成果。

本次仅补7份Markdown收尾记录（README、计划第10节、日志、学习/面试笔记、D13报告和evals说明），源代码、依赖、正式清单、自动收据及历史冻结内容保留；收尾提交仍由用户亲自操作。D14须等待用户明确要求。

本轮文档检查通过空白/围栏、52张表列数、197处本地链接/11锚点及固定8节报告；计划第10节外和旧日志/笔记章节保持。D13收据6份源码/依赖、V1原21份源码及19份机器产物SHA均一致；独立只读事实复审通过。git diff --check无输出，暂存区为空，无未忽略新增文件；本次七份收尾Markdown待用户提交推送。

## D14：应用层RRF与真实三路对照（2026-10-22）

**开发期状态快照（2026-10-22）：实现与Codex自动验证完成，阶段A待用户提交/推送，阶段B待ChatGPT学习及主动开始手测。** 用户已推送D13收尾 [ca769dc4ea85010e30193c42f5c04991333b5268](https://github.com/yan-ye-dhy/project-copilot/commit/ca769dc4ea85010e30193c42f5c04991333b5268)，开启D14时main/origin/main一致、工作区干净；Codex没有Git写操作。后续同步及人工完成情况见本节末补记。

新建fusion.py的`Σ1/(60+rank)`（排名从1开始），不相加两路原始分数；同路重复只计首次且原名次不压缩，跨路原文/metadata必须一致，并列按UUID。hybrid.py校验同一完整成功清单、各路预算及原文，空路可融合、异常向上抛出。用独立文件扩展，避免改写D10已记录源码哈希的retrieval.py；原问答API和Dense默认、750/150及Prompt/Schema保留，无新增依赖。原Qdrant HYBRID源码和当前SDK融合实现只读参考，新项目融合/编排/实验自主实现；SDK默认及排名起点不能等同本项目c60的1-based约定。

新增hybrid_run.py和固定配置hybrid_v2.json，公开V0归档→私有manifest→原30问题向量逐行绑定文本/合同/维度/有限值；不调用模型或回退重收。冻结原文目录与当前V0完成清单/16个payload逐值相等后，只读复用集合。两路各10候选，实际并集10～14，输出深度10供k=1/3/5/10评分，最终证据预算仍5块。每题交替先执行Dense或BM25，各一次后共享候选融合；候选异常会将Hybrid登记失败，不伪装成正常空路。

### 实际自动验证（执行者：Codex）

| 实际命令/操作 | 结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_fusion.py tests/test_hybrid.py tests/test_hybrid_run.py tests/test_bm25.py tests/test_bm25_cli.py tests/test_dense_pipeline.py tests/test_qdrant.py tests/test_documents.py tests/test_chunking_run.py tests/test_eval_comparison.py tests/test_baseline_config.py tests/test_eval_run.py tests/test_eval_metrics.py tests/test_eval_freeze.py tests/test_eval_dataset.py` | 465 passed，32.26秒；本日当时106新增，其余为相关回归 |
| `.venv/bin/python -m pytest -q tests/test_hybrid_run.py -k future_prompt` | 后新增1项通过，30 deselected，2.30秒；模拟将来Prompt变化仍能核验旧归档，实际旧Prompt未改。合计466个不同测试，本日107新增（44融合/32编排/31采集） |
| `.venv/bin/python -m evals.hybrid_run plan --dataset evals/datasets/copilot_v0 --baseline-dir evals/results/dense-v0-20261019-01 --baseline-private-dir data/evals/dense-v0-20261019-01 --config evals/configs/hybrid_v2.json` | 退出0，5文档/16片段/30原问题向量，预算各10、c60、最终5，模型/集合写入0 |
| `docker compose --env-file .env.example up -d qdrant` | 已有容器启动成功，复用服务器1.19.2；沙箱无法访问socket/本机端口，按权限审核后访问，不改Compose或持久卷 |
| `.venv/bin/python -m evals.hybrid_run collect`，沿用plan四项输入并加`--output-dir data/evals/hybrid-v2-20261022-01` | 退出0，实际30次query_points、30次本地BM25、30次融合，三路错误0，Embedding/问题Embedding/Chat及Qdrant写入均0。集合/retrieve/count/info预检读请求另外记录，不把30次检索说成全部HTTP请求数 |
| `.venv/bin/python -m evals.hybrid_run verify --dataset evals/datasets/copilot_v0 --output-dir data/evals/hybrid-v2-20261022-01` | verified，23封存文件；manual_review=pending/day14_accepted=false保留自动快照，用户尚未学习验收 |

真实dev含26可答题、4无答案。三路证据Recall@5均1.0；证据MRR@5为Dense0.836538、BM250.932692、Hybrid0.897436，Recall@3为0.858974/0.961538/1.0。Hybrid相对Dense6改善/0退化/20不变，相对BM252改善/4退化/20不变；BM25相对Dense7改善/2退化/17不变。012 gold完成名次4/1/3，RRF目标1/64+1/61≈0.03201844低于共识块1/61+1/63≈0.03226646；007为2/4/2。三路4道无答案均有命中，拒答率null，没有回答质量结论；历史V0逐题指标全部一致。

准备852.423ms单列；本轮搜索中位数Dense5.3447ms、BM250.4589ms，融合0.0516ms，Hybrid顺序合计中位数5.8652ms。该合计来自共享候选组件，不是单独执行的Hybrid请求或云端端到端耗时；没有显式预热/重复计时，不能外推统计提速或泛化。

独立只读审查通过：23产物/23源码SHA、V0归档到原向量哈希链、90排名的canonical身份，标准库复算全部RRF及原文连续区间gold指标均一致。文档向量未读取，不能声称历史文档向量字节已核验。公开 [hybrid-v2-20261022-01](../evals/results/hybrid-v2-20261022-01/manifest.json)为本地24文件原字节副本，约3.67MB，不含问题向量、密钥或缓存。verify使用保存的基线合同及指纹，不重新生成当前Prompt。

完整交接见 [D14报告](reports/DAY_14.md)与 [V2报告](../evals/reports/HYBRID_V2.md)。本轮固定一个c/预算组合，未调停用词、查询改写、阈值或保留集，未生成回答；尚不选最终方案或替用户手算。D15只在本日A/B完成且用户明确开启后推进。

收尾检查：公开及私有V2 verify均通过23份封存产物，D8冻结校验、原私有V0的12份文件、公开V1的19份文件均verified；机器pending状态保留。8份Markdown空白/围栏、56张表列数、232处本地链接/11锚点及固定8节报告通过，计划第10节外、历史日志/笔记保持。V2公开/本地24文件逐字节一致，23源码/23产物SHA、V1原21源码/19产物及D13收据6源码/依赖SHA一致；独立只读事实/数学审查通过，修改了易误读为逐题领先的MRR措辞。git diff --check无输出，暂存区为空；本轮39文件待用户亲自提交推送，.env、问题向量及本地结果仍被忽略。

### D14用户Git同步与学习后验证补记（2026-10-23）

**当前状态：阶段A/B均完成，本次收尾文档待用户提交，D15未开启。** 用户已亲自提交并推送 [26b219711c8d02f28b504840e5a5f1906865827c](https://github.com/yan-ye-dhy/project-copilot/commit/26b219711c8d02f28b504840e5a5f1906865827c)，说明为`feat(rag): add RRF hybrid retrieval and V2 evaluation`。Codex只读核实main与origin/main相同，手测开始时工作区干净；没有Git写操作。

用户主动开启D14手动验证后，亲自运行以下离线命令。无需启动Qdrant或FastAPI，没有模型调用或重新采集排名：

```bash
uv run --frozen python -m evals.hybrid_run verify \
  --dataset evals/datasets/copilot_v0 \
  --output-dir evals/results/hybrid-v2-20261022-01

mkdir -p data/manual-d14
for branch in dense bm25 hybrid; do
  uv run --frozen python -m evals.run \
    --dataset evals/datasets/copilot_v0 \
    --config evals/results/hybrid-v2-20261022-01/evaluation_config.json \
    --rankings "evals/results/hybrid-v2-20261022-01/${branch}_rankings.jsonl" \
    --output-dir "data/manual-d14/${branch}-replay" \
    > "data/manual-d14/${branch}-console.json"
  echo "$branch 退出码：$?"
done
```

归档verify返回verified、files23、退出0；用户反馈三路退出码均0。Codex直接只读检查生成的console、config、retrieval和summary：每路30题、26可答/4无答案、检索错误0；90条记录除本地evaluation_ms外逐值一致，summary除run_id外一致，输入SHA、评分合同、有效问题顺序及原文身份相同，model_calls均0。Recall@5均1.0，MRR@5仍为0.836538/0.932692/0.897436，重评分没有重新测检索耗时。

针对012两块，用户收到按名次1/3与4/1计算`1/61+1/63`、`1/64+1/61`的练习，反馈“已经计算”，未另贴计算值；Codex提供A≈0.03226646、B≈0.03201844作为标准对照，解释gold块4/1/3及融合不保证最优。未将此反馈扩写为其他案例逐条人工原文核对、全量gold复核或实际执行RRF函数。

随后用户用`shutil.copytree`复制完整归档到`data/manual-d14/tampered`，仅将副本`hybrid_retrieval/summary.json`写为原字节加`b"\n"`，再执行：

```bash
uv run --frozen python -m evals.hybrid_run verify \
  --dataset evals/datasets/copilot_v0 \
  --output-dir data/manual-d14/tampered

echo $?
```

用户返回JSON error、退出1，符合预期。Codex只读核对两个目录各24文件、无缺增，唯一差异为副本汇总2029→2030字节，严格等于追加一个LF，JSON解析对象相同。正式24文件与HEAD逐字节一致，23封存产物/23源码SHA全部匹配；D8冻结9输入SHA及连同manifest共10文件的已提交字节保持。机器manual_review=pending/day14_accepted=false和base_commit/dirty=true保留原采集身份，人工完成记录不回写机器归档。

用户最终明确选择“已学习，能够解释”，确认已通过ChatGPT理解候选→RRF融合→离线评分及候选预算、融合分数、答案可信度的区别，阶段B据此完成。用户亲自运行手测，Codex仅读取产物/核对；本轮没有重采检索、RRF函数执行、服务启动、pytest、收费模型或回答/拒答验收。

本次仅更新8份Markdown（README、计划第10节、日志、学习/面试笔记、D14报告、evals说明和V2实验报告）；原源码、依赖、D8/V0/V1/V2封存文件及35日任务表保留，手测数据仍被忽略。收尾提交与推送由用户操作，D15等待明确要求。

本次收尾检查：8份Markdown空白/围栏、57张表列数、233处本地链接/11锚点及日报固定8节均通过；计划第10节外、D14之前日志/笔记保持。独立只读事实复审通过，手测目录及.env仍被忽略，git diff --check无输出、暂存区为空。以下8份文档待用户亲自提交推送。

## D15：本地Cross-Encoder最小适配（2026-10-23）

以下保留CPU阶段开发快照；用户随后明确选择GPU，安装及真实GPU结果见本节末补记。

**实现、自动验证及真实CPU小样本完成，阶段A待用户Git同步，阶段B待ChatGPT学习与用户主动手测。** 用户已推送D14收尾 [4e3cfbae05150a11f0a2ae17ea6596da06317387](https://github.com/yan-ye-dhy/project-copilot/commit/4e3cfbae05150a11f0a2ae17ea6596da06317387)，开启时main/origin/main一致、工作区干净。所有实现只在新项目，参考旧reranker.py的延迟加载及原文映射，没有调用原包或改变旧环境。

本机核对20逻辑CPU、WSL总内存7973540KiB、GPU为RTX4060 Laptop 8188MiB；沙箱内NVML受限，沙箱外只读取得型号。本轮按已说明的推荐CPU方案完成验证，未登记用户选择GPU，也没有验证CUDA执行。新环境初始没有Torch/Transformers；新增可选rerank依赖组，官方CPU源锁定torch2.14.1+cpu及transformers5.20.2，基础依赖版本保留。uv lock首次因沙箱网络不可达失败，权限审查后成功；uv sync --frozen --group rerank完成安装，CPU wheel约187.2MiB，不拉CUDA运行库。

模型固定为BAAI/bge-reranker-v2-m3，revision为953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e。snapshot_download显式token=False、cache_dir=models/huggingface，allow_patterns仅6份config/tokenizer/safetensors文件，max_workers=2；首次准备含SHA检查约158.275秒。共2293242108字节，其中model.safetensors为2271071852字节，SHA为d9e3e081faff1eefb84019509b2f5558fd74c1a05a2c7db22f74174fcedb5286，与官方pointer一致。权重及准备中间文件只在忽略目录；没有收费API或密钥读取。

新rag/rerank.py把模型输入和返回证据分开，config固定默认CPU float32/4线程、batch2、max_length512、候选10/输出5。LocalCrossEncoder首次非空评分才import并加载本地固定revision，trust_remote_code=False/use_safetensors=True/local_files_only=True；每实例复用模型。Tokenizer仅截第二段，query预算用尽则拒绝，返回原hit/metadata/score不变，另存candidate_rank/raw logit；负logit正常，并列保留候选原序。输入、分数数量/有限值、模型加载及评分异常均明确失败，不自动下载、重试、扩召回或回退原排名。

### 实际自动验证（执行者：Codex）

```bash
.venv/bin/python -m pytest -q tests/test_rerank.py
.venv/bin/python -m pytest -q tests/test_rerank_smoke.py

timeout --signal=INT 120s .venv/bin/python -m pytest -q \
  tests/test_rerank.py tests/test_rerank_smoke.py \
  tests/test_fusion.py tests/test_hybrid.py tests/test_hybrid_run.py \
  tests/test_rag_api.py tests/test_rag_pipeline.py tests/test_answering.py tests/test_main.py
```

新增核心61项、CLI9项分别通过；最终联合235项全部通过、21.49秒，其中70项本日新增，不把重复执行累加成更多不同测试。沙箱内首轮联合在旧test_rag_api首个请求阻塞后Ctrl+C退出130；诊断重跑加faulthandler_timeout=20同处等待，随后停止退出130。栈显示AnyIO线程池已空闲而主asyncio仍等待。沙箱外单独旧用例1 passed/0.84秒，随后同一联合集合235 passed；未修改旧API或测试来绕过沙箱问题，没有未解决的测试失败。

离线测试覆盖惰性加载、严格参数/候选预算、负分/并列、原文身份与score保留、token截断诊断、空候选、模型/分数异常无回退，以及CLI保护、陈旧归档、缺缓存、安全错误和真实脚本路径执行。测试中的fake scorer与下述真实模型执行分别记录。

真实CLI命令为：

```bash
HF_HOME=/home/yanye/project-copilot/models/hf-home \
HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_PROGRESS_BARS=1 \
TOKENIZERS_PARALLELISM=false \
/usr/bin/time -f 'peak_rss_kib=%M\nwall_seconds=%e' \
  -o data/auto-d15/smoke-resources.txt \
  .venv/bin/python scripts/rerank_smoke.py --local > data/auto-d15/smoke.json
```

退出0：脚本先验证公开D14归档，固定012及其原Hybrid前10块，首次与热态各评分一次、各输出5块，共20pair。两遍10个logit完全一致，目标17b8925a...由候选第3排到第1，raw logit=-3.1111164；首/热模型推断分别9370.546/8131.491ms，首次模型加载3580.835ms，首/热整个rerank分别12963.559/8143.689ms。10块都未触发512截断；两次选中片段的原文、全部metadata及原score与归档逐值一致，全部20条输入ID/score/hash核对通过。

CLI前另独立调用同一适配器做1条合成中文passage诊断：3800字符、pair原长2120 tokens，实际输入512、passage_truncated=true，调用方文本SHA不变；随后用超长query调用，推断前拒绝，inference_ms=0。该进程首次加载11385.796ms，推断1378.100ms。两次调用属于不同进程，CLI前OS文件缓存已有热度，不能把3.58秒写成冷盘启动；预下载时间单列。CLI进程peak RSS为2097024KiB、总墙时22.89秒，GPU只核对型号未使用；没有并发运行模型或生产吞吐结论。

本轮总计3次成功本地评分、21个query-passage pair，另1次超长query在推断前拒绝；外部Embedding/Chat/Rerank API与Qdrant请求均0。公开 [D15验收记录](evidence/D15_RERANK_SMOKE.json)含模型6文件hash、27份源码/依赖SHA、运行配置/硬件、全部10候选分数与两遍5块完整原文、合成诊断和边界。原D14的23源码/23产物SHA保持；没有将本次单题结果登记为完整dev/保留集/回答效果，未开始D16或用户手测。

本次仅新增独立适配器、验证入口、测试和报告，更新可选依赖及必要文档；默认Dense问答、750/150、D8冻结数据和V0/V1/V2归档保留。用户亲自完成Git提交推送后交ChatGPT学习，再主动返回开始手测；阶段A/B完成后仍须明确要求才开启D16。

本轮收尾：7份Markdown围栏/空白、52张表列数、231处本地链接/12锚点及固定8节报告通过；uv lock --check --offline通过，既有依赖版本保留。D8的9输入、V0的12产物、V1的19产物、V2的23产物及本次27源码/依赖SHA核对保持；计划第10节外和早先日志/笔记原文保留。独立只读审查通过，git diff --check无输出，暂存区为空；14份新增/修改文件待用户亲自提交推送，模型缓存/本地中间文件/.env继续忽略。

### D15按用户明确选择补充GPU验证（2026-10-23）

用户明确答复“使用GPU验证，接受额外CUDA依赖与准备时间”，故继续本日GPU准备与验证，不改变既有CPU测量或开启D16。当前GPU路径已实测完成，阶段A待用户Git同步、阶段B待学习与主动手测。

CPU的rerank组保留，新增互斥的rerank-gpu组，两个Torch来源分别绑定官方CPU与cu130索引。驱动610.62满足官方CUDA13.x兼容要求；安装前观测8188MiB总显存、6325MiB可用。以下真实执行完成：

```bash
uv lock --cache-dir /tmp/pc-d15-uv
uv sync --frozen --group rerank-gpu --cache-dir /tmp/pc-d15-uv
```

锁文件解析105包；CUDA wheel及运行库准备约7分22秒，安装Torch2.14.1+cu130、CUDA13.0构建依赖与Triton等，未改系统驱动。显式同时选择rerank与rerank-gpu的dry-run按预期退出2，提示依赖组冲突；基础组默认不拉CPU/GPU重排运行库。已有模型文件复用，无第二次权重下载。

GPU验收仍调用已有run_smoke，core与CLI未改。开发期临时wrapper在每遍前同步CUDA并重置allocator峰值，完成后同步；遍历所有浮点参数，实际验证cuda:0和float16。完整wrapper源码及SHA保存在 [GPU记录](evidence/D15_GPU_RERANK_SMOKE.json)的instrumentation中，不增加业务模块。实际运行命令：

```bash
HF_HOME=/home/yanye/project-copilot/models/hf-home \
HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_PROGRESS_BARS=1 \
TOKENIZERS_PARALLELISM=false \
TRITON_CACHE_DIR=/home/yanye/project-copilot/models/triton \
TORCHINDUCTOR_CACHE_DIR=/home/yanye/project-copilot/models/torchinductor \
CUDA_CACHE_PATH=/home/yanye/project-copilot/models/cuda-cache \
.venv/bin/python /tmp/pc-d15-gpu-validation-20261023.py
```

退出0，实际设备RTX4060 Laptop、compute capability8.9、Torch2.14.1+cu130、CUDA编译版本13.0。相同012题及相同10候选首/热各一次，20pair；目标gold由3到1，GPU raw logit=-3.109375。首/热全部分数一致，选中原名次均[3,4,2,1,5]且与CPU相同；对原score、原文hash和每遍选中片段完整metadata逐值核对通过。CPU/GPU最大logit差0.087717，硬件与精度同时变化，不把该差异写成纯硬件单因素对照。

首次模型加载10856.167ms，首遍模型推断1112.377ms、整个rerank12007.819ms；同实例热重复模型推断160.087ms、整个rerank174.468ms、模型加载0。Torch已预导入、CUDA context已初始化、权重和OS文件缓存非冷盘；该first/warm记录不代表完整进程冷启动或生产吞吐。

每遍PyTorch allocator最大allocated为1166032896字节（约1.09GiB）、reserved1189085184字节；首遍包含加载权重，热遍包含常驻模型基线，不是全卡峰值或全部CUDA context开销。验收进程host peak RSS4593236KiB（约4.38GiB）另列，不与显存混用。

CUDA环境重新执行同一9文件联合pytest，沙箱外235 passed、22.16秒；与CPU环境21.49秒是同一235个用例的两次执行，其中70项本日新增，不累加为470不同用例。未改旧API或测试；真实GPU模型调用与fake测试分别记录。

新增GPU验证2次成功评分、20pair；加原CPU阶段为5次成功评分、41pair，另1次CPU超长query推断前拒绝。外部模型API及Qdrant请求仍0；未另跑GPU float32、全量dev、保留集或回答评测，D16未开始。

CPU旧证据SHA保持7040b1cc02099e0c083a1da588d8fa528ce24d5bd65d0bd7258165aa6f1b2ff6，保留其当时CPU依赖hash。新GPU证据登记当前27源码/依赖SHA；相对CPU仅pyproject.toml和uv.lock变化，其余25份源码相同。新配置指纹为rerank-config-v1-263ecd9b1bbc4a7665f6277ec68344cee99569bea3956fe82cfc7b31f50c457c，CPU阶段指纹与数据不改写。当前共15份新增/修改文件待用户审核提交，模型缓存和本地验收中间文件不入Git。

GPU补充收尾检查：7份Markdown围栏/空白、53张表列数、241处本地链接/13锚点及日报固定8节通过；当前27源码/依赖SHA匹配，CPU历史证据原字节保留，其余25份代码来源保持。D8/V0/V1/V2分别9/12/19/23份封存文件SHA核对一致，计划第10节外、既有基础依赖版本与早先笔记/日志保持。uv lock --check --offline和独立只读事实审查通过，git diff --check无输出，暂存区为空；当前15个新增/修改文件待用户亲自提交推送，模型及CUDA编译缓存、本地中间文件和.env仍被忽略。

### D15用户Git同步、学习与手动验证补记（2026-10-25）

**当前状态：D15阶段A/B均完成，本次7份收尾文档待用户提交，D16未开启。** 用户已亲自提交并推送 [9c332a57bf86053063080d6c802332372b7bb185](https://github.com/yan-ye-dhy/project-copilot/commit/9c332a57bf86053063080d6c802332372b7bb185)，说明为`feat(rag): add local BGE reranking and GPU validation`。Codex只读核实main与origin/main相同，手测开始时工作区干净；未执行Git写操作。

用户主动要求开始D15手动测试，随后本人按GPU方案完成五个环节。无需Qdrant/FastAPI服务或外部模型API；开发期自动验证保持单独记录。

第一步验证默认门禁，实际命令与结果：

```bash
uv run --frozen --group rerank-gpu python scripts/rerank_smoke.py
echo $?
```

未加--local时提示未加载模型或读取评测输入，退出2。第二步由用户核对运行时并真实执行CUDA小张量加法：

```bash
uv run --frozen --group rerank-gpu python - <<'PY'
import torch

print("PyTorch：", torch.__version__)
print("CUDA编译版本：", torch.version.cuda)
available = torch.cuda.is_available()
print("CUDA可用：", available)

if not available:
    raise SystemExit("CUDA不可用，请先保留输出并反馈")

print("GPU：", torch.cuda.get_device_name(0))
tensor = torch.tensor([1.0], device="cuda")
print("张量设备：", tensor.device)
print("计算结果：", (tensor + 1).item())
PY

echo $?
```

用户反馈Torch2.14.1+cu130、CUDA编译版本13.0、available=True、RTX4060 Laptop、张量cuda:0、结果2.0，退出0；此步没有加载重排模型。第三步本人执行真实GPU单题重排：

```bash
mkdir -p data/manual-d15

HF_HUB_OFFLINE=1 HF_HUB_DISABLE_PROGRESS_BARS=1 \
  uv run --frozen --group rerank-gpu python scripts/rerank_smoke.py \
  --local --device cuda --dtype float16 \
  > data/manual-d15/smoke-gpu.json

echo $?
```

用户反馈退出0。Codex只读取结果：来源D14先verified，同一012题及原序10候选，首/热各10pair、各选5块；新增2次本地评分、20pair，外部API/Qdrant请求0。20条输入的chunk ID、原score和文本SHA，以及10条选中完整hit（text、全部metadata、原score）均与原归档逐值相同。首/热分数与选中顺序一致，原候选名次[3,4,2,1,5]，均未截断。

本轮用户实际首模型加载11500.209ms，首遍模型推断926.304ms、重排总计12446.690ms；热重复模型加载0、推断146.443ms、重排总计158.899ms。没有以旧自动测量替代这些值，也不把单次热态耗时解释为生产P95。

第四步用户亲自查看smoke-gpu.json的first_pass.selected_hits第一项，反馈“已看到”：candidate_rank=3、rerank_rank=1、原hit.score约0.03201844、新rerank_score=-3.109375；来源dense_identity_and_citations.md，chunk ID为17b8925a-2b6a-5793-87ad-1ca2c52f581e，原文包含改名/移动不改变内容身份的说明。未要求重复手算，未扩大为其他案例或全量gold人工复核。

第五步使用不存在的专用测试缓存路径，实际执行：

```bash
HF_HUB_OFFLINE=1 \
  uv run --frozen --group rerank-gpu python scripts/rerank_smoke.py \
  --local --device cuda --dtype float16 \
  --cache-dir data/manual-d15/missing-model-cache \
  2> data/manual-d15/missing-cache-error.json

echo $?
cat data/manual-d15/missing-cache-error.json
```

用户反馈退出1，JSON error为“本地模型快照缺失；请先准备冻结revision对应缓存，本脚本不自动下载”。Codex只读确认错误文件一致、测试缓存路径仍不存在；该分支在创建LocalCrossEncoder前结束，没有替用户重跑或下载。

用户最终明确选择“已学习，能够解释”，确认已通过ChatGPT理解候选→query/passage成对输入→logit→排序→完整原文/metadata返回及截断不修改返回原文的原因，阶段B据此完成。本人手测没有长文截断/超长query、CPU推断、逐模型参数device/dtype或显存插桩、全dev/保留集或回答质量验证；开发期的合成截断与GPU wrapper仍属Codex自动证据，本次CLI只输出请求配置和实际评分结果，不冒称额外输出了显存或参数检查。

本次仅补7份Markdown（README、计划第10节、日志、学习/面试笔记、D15报告和evals说明）。CPU/GPU正式证据与已提交字节相同，GPU27份源码/依赖SHA匹配；D8/V0/V1/V2的9/12/19/23份封存输入/产物及清单保持。源代码、依赖、模型缓存和35日任务表不变，手测JSON仍在忽略目录。收尾Git操作由用户亲自完成，D16等待明确要求。

本次收尾检查：7份Markdown围栏/空白、54张表列数、242处本地链接/13锚点及日报固定8节通过，计划第10节外、D15之前笔记和既有日志原文保持。独立只读事实审查通过，正式证据及旧冻结内容保留；手测数据、模型缓存和.env仍被忽略。git diff --check无输出、暂存区为空，7份文档待用户亲自提交推送；本次Codex没有代跑手测CLI、模型、pytest或服务。

## D16：固定候选的GPU重排对照（2026-10-25）

**实现、必要自动验证与真实GPU采集完成；阶段A待用户提交/推送，阶段B待ChatGPT学习及用户主动手测。** 用户已推送D15收尾 [e6cc917b5505357317ff5506cfa7e1735e9f6c31](https://github.com/yan-ye-dhy/project-copilot/commit/e6cc917b5505357317ff5506cfa7e1735e9f6c31)，开启时main/origin/main一致、工作区干净。用户要求常规选择直接采用推荐方案，必须人工确认才暂停；本轮采用既有本地CUDA方案，无外部收费调用或新模型下载，Git写操作仍由用户本人执行。

新增独立evals/rerank_run.py、固定rerank_v3.json和test_rerank_run.py，不修改D15核心重排器、旧评测器或业务API，也不新增依赖。先校验D8冻结、父V2 manifest固定SHA、同一30dev题与每题同序10候选及原文/metadata、父23份及D15GPU来源源码/依赖，再一次性校验本地6份模型文件SHA。模型仍为BGE v2-m3固定953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e，CUDA float16、batch2、输入512 tokens；排名深度10供k=1/3/5/10评分，最终上下文合同独立固定5块，没有回答生成。

第一次首题pc-d7-001的10pair作独立warmup，加载与首推断不进质量分母或30题暖态统计；随后原序001/002为smoke，包含在30题中，不额外重复。预热或前两题失败立即停止并保留partial，无封存成功；后续失败空排名保留完整分母，不回退原序。尝试调用/输入pair与完整有限分数成功返回单独计数，detail.score_succeeded与其后的诊断错误分别保留。

### 实际自动验证（执行者：Codex）

新测试最终60 passed、5.14秒；fake PairScorer与真实GPU结果分别记录。覆盖同候选、原分数/身份、预热及预算、失败分母、公开重评分、父来源/配置漂移、重封后指标不自洽、缺失/额外文件、未来Prompt/模型默认独立及CLI显式门禁。首轮两个测试预期不符已修正（collect门禁为exit1、安装包METADATA属正常只读依赖），最终不累计中间运行结果。

本轮实际命令：

```bash
HF_HUB_OFFLINE=1 .venv/bin/python -m evals.rerank_run plan \
  --dataset evals/datasets/copilot_v0 \
  --hybrid-dir evals/results/hybrid-v2-20261022-01 \
  --config evals/configs/rerank_v3.json > data/auto-d16/plan.json

HF_HOME=/home/yanye/project-copilot/models/hf-home \
HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_PROGRESS_BARS=1 \
TOKENIZERS_PARALLELISM=false \
TRITON_CACHE_DIR=/home/yanye/project-copilot/models/triton \
TORCHINDUCTOR_CACHE_DIR=/home/yanye/project-copilot/models/torchinductor \
CUDA_CACHE_PATH=/home/yanye/project-copilot/models/cuda-cache \
/usr/bin/time -f 'peak_rss_kib=%M\nwall_seconds=%e' \
  -o data/auto-d16/resources.txt \
  .venv/bin/python -m evals.rerank_run collect \
  --dataset evals/datasets/copilot_v0 \
  --hybrid-dir evals/results/hybrid-v2-20261022-01 \
  --config evals/configs/rerank_v3.json \
  --output-dir data/evals/rerank-v3-20261025-01 --local \
  > data/auto-d16/collect.json

.venv/bin/python -m evals.rerank_run verify \
  --dataset evals/datasets/copilot_v0 \
  --output-dir data/evals/rerank-v3-20261025-01

.venv/bin/python -m evals.rerank_run verify \
  --dataset evals/datasets/copilot_v0 \
  --output-dir evals/results/rerank-v3-20261025-01

timeout --signal=INT 120s .venv/bin/python -m pytest -q \
  tests/test_rerank_run.py tests/test_rerank.py tests/test_rerank_smoke.py \
  tests/test_eval_comparison.py tests/test_eval_run.py tests/test_eval_metrics.py \
  tests/test_hybrid_run.py tests/test_rag_api.py tests/test_main.py
```

plan实际退出0，申明30题、26可答/4无答案、31调用/310pair预算；导入运行器后sys.modules中的Torch/Transformers均未加载。真实collect退出0，31次评分/310pair全部成功，重排错误0，API及Qdrant请求0，没有重试、下载或读取.env。运行时Torch2.14.1+cu130、Transformers5.20.2、CUDA编译版本13.0、RTX4060 Laptop/compute capability8.9；首次GPU空闲显存约6541MiB，模型实际31份观测全部cuda:0/torch.float16。

两份verify均退出0、verified、19份封存产物；公开目录为私有目录20份文件原字节副本，共3725251字节，不含权重/向量/密钥。verify仅使用公开归档和冻结数据，验证SHA、同候选全排列/logit排序、最终5块并重算gold指标/汇总/成对结果；不重跑GPU、私有缓存或当前Prompt。GPU观测由真实collect逐参数校验、独立审查逐条核对；机器manual_review=pending/day16_accepted=false保留，未替用户验收。

联合9文件实际266 passed、25.09秒，含60项本日新增，不重复累加为326项。ASGI相关离线测试使用沙箱外执行，fake服务不调用真实模型或数据库。无未解决的自动测试失败。

### 真实效果、退化和计时边界

26可答题证据MRR@5由0.8974358974升至0.9711538462，证据Recall@5和@10保持1.0；Recall@3由1.0降到0.9358974359，@3全部gold覆盖题率由26/26降至24/26。逐题按所有公开指标及k判断4改善、2退化、20指标不变，4无答案独立。改善为003、012、024、040；退化为007、034，均保留原文与分数，不据此直接更换默认方案。

007空TXT上传问题的真实422/请求前拒绝gold原rank2降至4，前排出现向量配置/凭据等其他相关术语片段。可观察到具体上传拒绝证据被排后，不能仅凭logit证明模型为何如此打分。034跨段题中Compose证据仍rank1，启动命令和lifespan两段从rank3退至4，证据完成名次[1,3,3]→[1,4,4]；首gold的MRR保持1但@3缺两段，说明单看MRR会漏掉多证据退化。完整案例在 [V3报告](../evals/reports/RERANK_V3.md)及逐题归档。

主30题的rerank组件中位数161.644ms、p95 169.081ms；每题含tokenize/排序及诊断，单独模型推断另列score_details。warmup总11733.402ms、模型加载10330.751ms、首次模型推断1368.537ms，不进入主统计，主30模型加载均0。warmup10pair与主300pair均无截断，主输入168–494 tokens；只说明这批样本未触发512上限。

基线rankings按原字节保留D14历史timings；本轮V3只测rerank，embedding/search/fusion/端到端total均null。score_details.timings_ms.total是局部重排总计，不能与rankings里的端到端total混同。所有paired_timings共同样本均0，不能将旧V2约5.865ms历史total与新组件相减称请求增量延迟或端到端提速。

本次进程wall28.56秒、host peak RSS4583248KiB约4.37GiB，准备/权重哈希/加载/写盘不都属于逐题时延。GPU allocator最大allocated1167455232/reserved1189085184字节（约1.09GiB allocated），包括常驻模型，非全卡峰值；CUDA context已先初始化，OS缓存未清，不称冷盘或生产吞吐。4道无答案两侧都有非空命中，refusal_accuracy仍null；没有回答/拒答/引用质量结论。

独立标准库审查重算60份排名×4k×5指标共1200个值、完成名次、汇总/分类/Bad Cases，均与产物一致；同一原10候选全字段全排列、原RRF分数保留、logit稳定排序及top5均符合。20份公开/私有文件、19份封存产物、28份源码/配置/依赖及6份模型文件hash保持；31份GPU观测与全部成对计时边界已核对。D8/V0/V1/V2/D15证据、源代码与依赖保留，默认仍Dense及750/150。

完整交接见 [D16报告](reports/DAY_16.md)、[实验报告](../evals/reports/RERANK_V3.md)与 [公开manifest](../evals/results/rerank-v3-20261025-01/manifest.json)。本轮保留一组配置，没有用保留集/额外参数搜索选优；D17最终选择及冻结仍等待本日A/B完成与用户明确开启。用户未主动开启本日手测，自动结果不登记为本人学习或案例复盘。

收尾检查：31份交付文件范围符合本日清单；8份Markdown的围栏/空白、56张表、273处本地链接及14个锚点、日报固定8节均通过。计划第10节之外及既有日志/笔记原文保持；19份新产物、28份运行源码/配置/依赖SHA和20份公开/私有归档字节再次一致。独立只读文档审查无剩余问题，运行参考中的输出目录已改为可直接使用的新目录示例。git diff --check通过，暂存区为空；Git提交与推送仍交用户执行，不因此开启手测或D17。

## D16：学习确认与本次委托验证（2026-10-26）

D16开发提交[10951130c26d0a85a8a8e6f2ed53fdbbc746bbde](https://github.com/yan-ye-dhy/project-copilot/commit/10951130c26d0a85a8a8e6f2ed53fdbbc746bbde)已由用户推送，阶段A完成；本次开始时main/origin/main一致、工作区干净。用户主动开启D16验证，并明确要求Codex直接执行及提交GitHub；随后确认已通过ChatGPT学习、能够解释主流程。本次阶段B以“学习已确认＋用户授权代操作验证”完成记录，命令实际执行者为Codex，不新增用户亲自终端操作或逐案例人工gold复核声明。仅本轮例外，不修改长期协作规则。

Codex实际在新项目根目录执行7条CLI，输出保存在忽略目录data/manual-d16/verification-20261026-01，完整命令/退出码/输出SHA及核对结果另存[委托验证收据](evidence/D16_DELEGATED_VERIFICATION_20261026.json)：

| 命令或检查 | 实际结果 |
| --- | --- |
| evals.rerank_run plan，原冻结配置 | 退出0，30题/26可答/4无答案；31次310pair为计划预算，未启动模型 |
| evals.rerank_run verify，公开V3归档 | 退出0，verified、19产物、model_loaded=false；原人工状态快照保持 |
| evals.run，baseline与rerank各重放30题 | 各退出0；60条记录除本次evaluation_ms外一致，summary除run_id外一致；重新生成的comparison逐值相等 |
| collect不加--local | 预期退出1，采集目录未创建 |
| 配置副本max_candidates改为9再plan | 预期退出1，不读取模型 |
| 归档副本comparison的题数改为29再verify | 预期退出1，原归档不变 |

30题的同一10候选全字段及原RRF分数逐值保持，独立logit稳定排序及最终5块符合；完整深度证据Recall守恒，4无答案仍单列，所有paired_timings样本0。Codex另核对012改善3→1、007退化2→4，以及034的三段[1,3,3]→[1,4,4]与冻结语料字符区间原文；034在@3的首条MRR仍1而证据Recall仅1/3，保留退化。新增模型API、Qdrant及GPU评分均0，未重新采集模型分数、下载模型或重跑pytest；开发期266项测试不与本轮7条CLI叠加计数。

检查前后186份既有源码/依赖/测试/评测及证据文件SHA保持，D8、V0/V1/V2/V3和D15原字节不改写。新收据单独记录本次授权及执行者，原机器manual_review=pending/day16_accepted=false仍是历史快照。仅更新8份Markdown与1份验证收据，不实现功能、不读取真实.env；按用户明确授权完成本次Git提交和推送，结果在会话补全。D17未开始。

本次收尾检查：8份Markdown的围栏/空白、58张表、280处本地链接及14个锚点、日报固定8节通过，计划第10节外和既有日志原文保持。最终范围为8份Markdown＋1份独立验证收据；除两份有意更新的evals说明/报告外，原受检文件184份及28份运行源码SHA、V3的19份封存产物保持。Git提交前再次检查diff与新JSON格式，不增加业务代码或依赖改动。

## D17：最终方案冻结与保留集自动验收（2026-10-26）

用户明确开启D17并授权Codex完成本次GitHub提交，起点为已同步D16收尾b3f4fcf、干净main。D16学习和委托验证状态按原记录保留，不把上日代操作授权扩为D17用户学习/手测完成。本日仅选择并验证最终检索方案，不新增算法或开始D18。

### 开发集选择与采集前冻结

新增evals/final_selection.py、evals/holdout.py、tests/test_final_holdout.py、evals/configs/final_v1.json及evals/reports/FINAL_SELECTION.md。旧业务、旧评测器及依赖不改。对V0/V1/V2/V3与BM25的原30dev题重算gold指标，以固定top5的覆盖/全部证据覆盖/MRR和V3已测组件资源预算选择Hybrid＋BGE Rerank，保留V1覆盖损失与V3 @3退化；规则于已有dev实验之后、holdout之前书面化，不冒称开发实验前预注册。

配置指纹final-selection-v1-d77720ad02c9ac3caa83b3fa6981c6d197b1c7238d220cf64cca3dd5ced2f450。先提交并推送[e676d5021d84d0bcaa7eeb700287833d844e703a](https://github.com/yan-ye-dhy/project-copilot/commit/e676d5021d84d0bcaa7eeb700287833d844e703a)，随后才执行真实holdout；manifest记录该commit、dirty=false，配置与该提交原字节一致。

### 自动测试与真实命令（执行者：Codex）

新增用例使用内存Qdrant、fake向量及PairScorer，禁止网络、真实.env及Torch；覆盖冻结、重封后改参、同一向量、原候选/分数、失败尝试计数和完整分母、人工状态、闭集SHA及语义篡改。最终相关7文件275 passed、21.42秒，包含27项D17新增；首轮24项及中间重复回归不累加为更多不同用例。

```bash
HF_HUB_OFFLINE=1 .venv/bin/python -m pytest -q \
  tests/test_final_holdout.py tests/test_rerank_run.py tests/test_eval_metrics.py \
  tests/test_eval_comparison.py tests/test_fusion.py tests/test_bm25.py tests/test_hybrid.py

HF_HUB_OFFLINE=1 .venv/bin/python -m evals.final_selection freeze \
  --dataset evals/datasets/copilot_v0 --config evals/configs/final_v1.json
HF_HUB_OFFLINE=1 .venv/bin/python -m evals.final_selection verify \
  --dataset evals/datasets/copilot_v0 --config evals/configs/final_v1.json
HF_HUB_OFFLINE=1 .venv/bin/python -m evals.holdout plan \
  --dataset evals/datasets/copilot_v0 --config evals/configs/final_v1.json

HF_HOME=/home/yanye/project-copilot/models/hf-home \
HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_PROGRESS_BARS=1 \
TOKENIZERS_PARALLELISM=false \
TRITON_CACHE_DIR=/home/yanye/project-copilot/models/triton \
TORCHINDUCTOR_CACHE_DIR=/home/yanye/project-copilot/models/torchinductor \
CUDA_CACHE_PATH=/home/yanye/project-copilot/models/cuda-cache \
/usr/bin/time -f 'peak_rss_kib=%M\nwall_seconds=%e' -o data/auto-d17/resources.txt \
.venv/bin/python -m evals.holdout collect \
  --dataset evals/datasets/copilot_v0 --config evals/configs/final_v1.json \
  --output-dir data/evals/final-holdout-20261026-01 --live \
  > data/auto-d17/collect.json

HF_HUB_OFFLINE=1 .venv/bin/python -m evals.holdout verify \
  --dataset evals/datasets/copilot_v0 --output-dir data/evals/final-holdout-20261026-01
HF_HUB_OFFLINE=1 .venv/bin/python -m evals.holdout verify \
  --dataset evals/datasets/copilot_v0 --output-dir evals/results/final-holdout-20261026-01
```

所有实际CLI退出0。用户明确批准最多1次Embedding、10次本地Dense及110对GPU；实际Embedding一批10文本/1189 UTF-8字节，cache hit0/miss10/api1，约1008.739ms；10次Dense、10次BM25、10次RRF及11次GPU110pair（开发题001独立warmup10pair＋保留集100pair）均成功，Chat0，未重索引、下载或自动重试。价格已于10月26日核对[智谱官方Embedding-3文档](https://docs.bigmodel.cn/cn/guide/models/embedding/embedding-3)：0.5元/百万tokens，授权前以3万tokens预算¥0.015；usage/实付未采集记null，不用字节数冒充token数或账单。

### 结果、资源与证据边界

保留集原10题/8类，9可答/1无答案；两侧检索错误0，证据Recall@5均1，MRR@5为0.944444→1.000000；1改善、8指标不变，1无答案独立，未因结果改选。改善033两条gold首次覆盖[3,2]→[2,1]，首条MRR=1仍需第二块补齐；005两条gold两侧同rank1；038实际HNSW参数无证据，两侧都非空，拒答率仍null，本轮无回答生成或引用支持判断。

GPU为RTX4060 Laptop，Torch2.14.1+cu130/CUDA13.0、Transformers5.20.2；11次实际观察均cuda:0/float16，110对模型输入294～487 tokens且截断0，不因此新增长文截断验收。warmup总11873.864ms/加载10312.918ms/推断1521.046ms不进保留集统计。热态Final rerank中位数164.558ms、样本p95375.744ms；共享候选组件和中位数202.743ms、p951202.886ms，不含整批Embedding或LLM；Dense查询中位数37.660ms、p95672.216ms。没有独立线上请求或Dense预热，10样本p95等于最大值，不声称生产P95、端到端SLA或一般提速。

模型最大allocated/reserved1167384064/1189085184字节，含常驻模型且非全卡显存；采集进程wall28.74秒、peak RSS4528648KiB。问题向量为本日同一批新向量，文档向量沿用原V0；记录日期/合同/向量SHA，不能保证云端别名权重永久不变。

公开final-holdout-20261026-01目录24文件/2037047字节，与私有原字节一致，manifest封存23产物及11份执行源码/依赖SHA。私有/公开verify均verified，人工manual_review=pending/day17_accepted=false保持。独立标准库原文区间审查重算4路40条记录×4k×5指标共800值并验证汇总；10个问题缓存向量SHA逐值一致，hit10/miss0/API0，11份GPU观测及采集前配置Git字节均匹配，见[D17审计](evidence/D17_HOLDOUT_AUDIT.json)。审计未新增模型调用或评分。

完整交接见[D17日报](reports/DAY_17.md)、[采集前选型](../evals/reports/FINAL_SELECTION.md)和[最终保留集报告](../evals/reports/FINAL_HOLDOUT.md)。当前API仍Dense/750/150，冻结实验不冒充已经部署。D17学习及用户主动手动验证尚待完成；本次Git收尾按用户明确授权提交推送，真实ID/结果会话补全，不自动进入D18或用保留集继续选参。

D17收尾检查：本次结果提交共33文件，连同采集前5文件共38份交付；8份收尾Markdown的围栏/空白、55张表、303处本地链接及15个锚点、2段新bash示例语法及日报固定8节均通过。计划第10节外和既有日志原文保持，业务源码/依赖、原D8和V0/V1/V2/V3/D15/D16封存内容不变；采集后未修改已提交的选型、运行器或测试源码。23份新产物SHA、11份执行源码SHA及公开/私有24文件再次一致，真实.env、模型缓存、向量缓存与本地运行输出仍被忽略。Git收尾仅提交已审核结果和文档，不开始D17用户手测或D18开发。

## D17：学习确认与本次委托验证（2026-10-26）

开发与真实保留集结果[efa46fa401811a8b121295651f5a9d12164af788](https://github.com/yan-ye-dhy/project-copilot/commit/efa46fa401811a8b121295651f5a9d12164af788)已推送，阶段A完成；本次开始时main/origin/main一致、工作区干净。用户主动要求直接完成验证并提交，另明确确认已通过ChatGPT学习并能解释D17主流程。本次阶段B以学习确认＋明确授权代操作方式完成记录，实际执行者为Codex，不新增用户亲自终端操作或逐题人工gold复核声明；长期默认协作分工不变。

Codex实际执行下列9条CLI，全部退出码符合预期，输出保存在忽略目录data/manual-d17/verification-20261026-01；完整命令、退出码/输出SHA及案例见[D17委托验证收据](evidence/D17_DELEGATED_VERIFICATION_20261026.json)。

| CLI或检查 | 结果 |
| --- | --- |
| final_selection verify，原冻结配置 | 退出0，指纹保持 |
| holdout plan | 退出0，最多1次Embedding、28000 UTF-8字节及110pair仅为计划预算，本轮未采集 |
| holdout verify，原公开归档 | 退出0，verified/23产物，不加载模型 |
| final_selection freeze，目标为已有配置 | 预期退出1，原配置原字节不变 |
| collect不加--live | 预期退出1，未创建采集目录或客户端 |
| 选型副本top5改3并重新计算指纹后verify | 预期退出1，不能只重封配置就改选 |
| 归档副本只给comparison追加换行后verify | 预期退出1，即使JSON语义不变仍拒绝字节变化 |
| 归档副本MRR@5改0.99并重封文件SHA后verify | 预期退出1，指标必须与排名/原文gold一致 |
| 归档副本少一条Final排名并重封后verify | 预期退出1，不能缩小10题分母 |

此外，使用既有holdout.score_rows/compare_rows重放两侧20条评分和汇总/比较，逐值相等；保存至本地baseline-scored/final-scored及summary/comparison文件，实际文件名与SHA见收据。10题原Hybrid候选与重排结果全字段同集合、原RRF score保持、logit稳定排序及前5块符合；1改善/8指标不变/1无答案、分批Embedding计时不均摊的口径保持。

Codex核对033两段gold原文及首次覆盖名次[3,2]→[2,1]、005两条均rank1、038无gold且指标null。仅用户总体学习已确认，这三条的原文复核执行者仍为Codex。10条已保存问题向量命中缓存且逐值指纹一致，hit10/miss0/API0；没有实际新增Embedding/Chat、Qdrant或GPU评分，没有重跑pytest，不把本轮9条CLI叠加为开发期275项测试。

验证执行前后201份受检非Markdown源码/依赖/测试/评测及既有证据SHA保持，选型配置与采集前e676d50提交原字节一致；原开发和保留集归档、机器pending/day17_accepted=false快照及旧审计均不改写。新建独立委托验证收据并更新8份关联Markdown；本次Git提交推送按用户当前授权由Codex执行，真实提交信息会话补全。D18未开始。

本次收尾检查：8份Markdown的围栏/空白、57张表、310处本地链接及15个锚点、日报固定8节通过；计划第10节外和既有日志原文保持。最终范围为8份Markdown＋1份验证收据，201份受检非Markdown文件、11份执行源码及23份保留集产物SHA保持。git diff --check及新JSON格式检查通过；仅按本轮授权提交推送记录，不改业务代码、依赖或冻结实验，不开始D18。
