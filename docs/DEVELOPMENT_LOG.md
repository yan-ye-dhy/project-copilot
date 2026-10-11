# 开发日志

这里只记录实际实施和验证；任务顺序见 [开发计划](DEVELOPMENT_PLAN.md)，原项目审计保留在 [参考审计](REFERENCE_AUDIT.md)。

## D1：独立环境与最小API（2026-10-10）

**状态：工程实现与已有助手验收通过。2026-10-11已首次同步GitHub；固定开发报告与完整学习/手动验收尚未登记，A/B未全部完成。** 本日只建立后续开发入口，不推进D2。以下命令、检查与HTTP请求由Codex执行，未计入用户自行学习和操作的成果；用户实际学习耗时尚未登记。

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

新概念和真实调用链见 [D1学习笔记](STUDY_NOTES.md#d1独立工程入口与配置)，对应3个回答思路见 [D1面试笔记](INTERVIEW_NOTES.md#d1工程入口与配置)。当前先交ChatGPT学习，用户主动要求手动测试后由Codex单步指导；完整B阶段不因助手HTTP检查或一次用户请求成功而自动勾选。

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
