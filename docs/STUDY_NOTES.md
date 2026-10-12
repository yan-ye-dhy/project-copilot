# 学习笔记

结合实际文件、调用链与验证记录学习；不把尚未实现的设计写成能力。对应实施结果见 [开发日志](DEVELOPMENT_LOG.md)。

当前分工（2026-10-11）：本文件及D1导读作为ChatGPT代码/架构教学材料。Codex负责实现、自动验证、交接报告，以及用户主动要求后的手动带练；详细流程见 [协作规则](COLLABORATION_RULES.md)。

## D1：独立工程入口与配置

历史教学材料（2026-10-10起）：[D1完整导读](D1_WALKTHROUGH.md) 保留所有新增文件、真实代码、新语法、Uvicorn操作与运行链。2026-10-11用户已确认完成ChatGPT学习、能解释相关流程，并完成主动开启的手测；文中的复现命令仍是参考，不自动开启新一轮教学。

### 1. 今天解决什么问题

后续RAG和Agent需要稳定、可测试的后端入口。今天先让新项目独立启动并返回健康响应，把环境、包路径和配置加载问题单独验证。

**包**是Python可导入代码的组织方式，当前名称为 `project_copilot`；**虚拟环境**是独立的依赖安装位置，当前为新目录的 `.venv`；**锁文件**记录本次解析的具体版本。共享同一Python解释器不等于共享虚拟环境。`uv sync --locked`核对锁文件与项目声明并同步环境；`uv run --frozen`运行时沿用已有锁文件。uv_build按当前src布局构建包，见 [uv官方构建后端说明](https://docs.astral.sh/uv/concepts/build-backend/)。

### 2. 从原项目真实代码理解取舍

原项目 `src/agent/utils/config.py` 使用Pydantic Settings集中读取参数，但模块底部直接实例化配置，dotenv路径相对工作目录。原 `src/agent/api.py` 又在入口导入和初始化过程中接入图流程、Qdrant与Phoenix。学习它们的应用组织方式时，需要注意“导入入口”可能同时触发配置读取和外部资源初始化。

本次参考的是FastAPI入口、Settings声明和日志模式；新的 `config.py`、`create_app`、lifespan与健康响应按当前最小需求自行设计。未搬原项目完整入口、供应商分支或业务代码，也未添加原项目到PYTHONPATH。

### 3. 两个核心文件怎么配合

[`config.py`](../src/project_copilot/config.py) 中，`PROJECT_ROOT`从本文件的绝对路径定位新项目根目录，`ENV_FILE`固定为该目录 `.env`。这是当前src布局与本地可编辑安装下的约定；将来打包部署时再核验配置挂载路径。

`Settings`是配置类型，声明字段、默认值和校验规则。定义类时还没有读取配置；调用 `load_settings()`才创建实例、读取当前配置。`app_name`去除首尾空白后必须为1～80字符；`environment`限制为development/test/production。

当前字段的读取顺序为：显式构造参数 → 进程环境变量 → 指定dotenv → 默认值。`PROJECT_COPILOT_`前缀使系统寻找 `PROJECT_COPILOT_APP_NAME`等名称。dotenv固定路径避免随终端目录读取另一项目配置；进程环境变量仍会参与读取。官方规则见 [Pydantic Settings文档](https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/)。

[`main.py`](../src/project_copilot/main.py) 的 `create_app()`只构建FastAPI对象并注册路由。**lifespan**是应用启动、运行和关闭的生命周期：`yield`前执行启动步骤；`yield`期间应用处理请求；`yield`后执行关闭步骤。配置在启动时创建，保存在当前应用的 `app.state.settings`。规则见 [FastAPI lifespan文档](https://fastapi.tiangolo.com/advanced/events/)。

当前只有配置对象，没有需要关闭的数据库或模型客户端，因此关闭步骤仅留日志位置。非法配置会阻止启动，让问题在首个请求前显现。

### 4. 需要能讲清的完整调用链

从终端命令出发的分阶段调用、当前源码行号和对象联系，见 [完整导读中的运行链](D1_WALKTHROUGH.md#从终端命令到实际响应把两个文件连起来)。下面的简图用于复习。

```text
Uvicorn导入 project_copilot.main:app
  → create_app注册健康路由（此时未实例化Settings）
  → Uvicorn进入lifespan
  → load_settings读取并校验配置
  → application.state.settings保存配置
  → GET /health到达health函数
  → request.app.state.settings提供当前配置
  → 返回status、service、environment
  → 服务停止，退出lifespan
```

`create_app(settings=...)`还允许验证时直接传入配置对象，减少对机器上dotenv和环境变量的依赖。本日离线检查覆盖导入时不构造配置、启动时失败和优先级；真HTTP验证默认响应和环境变量覆盖。`/health`证明当前应用能响应，其他组件接入后才设计对应就绪验证。

### 5. 学习确认与手动测试记录

2026-10-11用户主动开启D1手动测试，亲自完成正常启动、默认HTTP响应、服务名环境变量变化、空白名称/非法环境拒绝、恢复及关闭，并确认已完成ChatGPT学习、能解释启动与请求流程。详细证据见 [开发日志](DEVELOPMENT_LOG.md#用户手动验收与学习确认2026-10-11)。以下保留为复习关注点，不要求重复已完成的操作；手工编写业务代码不是必做门槛。

1. 复习：若dotenv与进程环境变量冲突，响应使用哪个值？完整优先级仍引用原自动检查，本轮没有专门构造dotenv冲突用例。
2. 已观察：命令前设置app_name为Copilot Learning，健康响应service随之改变；去掉临时前缀后恢复Project Copilot。定位启动时 `load_settings()` 与请求时读取配置的位置。
3. 已观察：三个空格名称触发string_too_short，staging环境触发literal_error，两者均在启动阶段退出。区分启动校验失败与HTTP请求错误。

结合用户手测与本人理解确认，D1阶段B完成；2026-10-12核实用户提交的4c04a29已同步GitHub，阶段A也完成。用户明确开启D2，助手历史验收与用户操作仍分别记录。

## D2：原文本到Embedding向量

Embedding将文本编码成数值向量，为后续Dense检索提供输入；D2只验证供应商请求与向量结构，没有评估检索质量，也没有接Qdrant。

原 `src/agent/utils/embeddings.py:32` 的智谱分支采用LangChain OpenAIEmbeddings，关闭框架自动文本长度处理、指定float、每批64条。新项目改为直接OpenAI兼容SDK：仍发送原字符串，使用 `encoding_format="float"` 和batch64，但新增的索引/维度/有限数值校验与错误边界属于自主实现。SDK不做框架的文本切分，因此没有照搬LangChain专属的check_embedding_ctx_length参数。

运行链供ChatGPT组织教学：脚本显式`--live` → `load_settings()` → `create_embedding_client()`检查新项目key → `embed_query()`调用`embed_documents()` → SDK发送原文本数组 → 读取原始JSON → 核验模型/条数/index/向量 → 返回float列表 → 输出长度 → with关闭客户端。普通健康接口不会沿这条链调用模型。

理解重点：batch_size按文本条数控制请求，与D3的字符切分长度不同；预期维度用于校验，不直接控制API输出。原始JSON检查能识别SDK可能自动转换的bool/字符串；index决定文本与向量配对。HTTP200之后仍需验证响应结构；超时、401、429和5xx返回脱敏异常且默认不重试。

官方契约来源与真实验收状态见 [D2报告](reports/DAY_2.md)。本轮68项测试使用假传输/向量，另由Codex单次真实请求确认1条2048维向量，两类证据分别记录。用户随后主动开启手测，亲自确认无`--live`时退出2、带`--live`单条真实请求返回2048维且退出0、空白密钥覆盖时安全报错并退出1；用户确认已完成ChatGPT学习，能说明主要调用链与空白密钥在请求前被拒绝的原因，阶段B完成。
