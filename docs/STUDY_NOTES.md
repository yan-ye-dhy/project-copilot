# 学习笔记

结合实际文件、调用链与验证记录学习；不把尚未实现的设计写成能力。对应实施结果见 [开发日志](DEVELOPMENT_LOG.md)。

## D1：独立工程入口与配置

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

### 5. 用户自主练习（待完成，约10分钟）

先阅读两个核心文件，再按以下顺序操作。默认服务已在助手验证后停止。

1. 先预测：若新项目dotenv中的app_name与进程环境变量不同，响应会使用哪一个？指出 `load_settings()`实际执行的位置。
2. 在自己的终端按 [README配置练习](../README.md#配置与d1练习) 启动，将环境变量中的名称换成你选的非空值；另一个终端curl健康接口，核对service；结束后Ctrl+C停止。
3. 仅把该环境变量值换成空格，再尝试启动；观察校验错误，解释为何尚未处理HTTP请求就失败。恢复有效名称即可，无需修改业务代码。

完成后用自己的话复述上述调用链，并在开发日志追加你的实际命令、预测与结果。该练习与复述完成后再登记D1学习验收，不预先假定掌握。
