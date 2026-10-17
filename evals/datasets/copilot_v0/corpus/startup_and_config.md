# 启动与配置排查笔记

资料日期：2026-10-16。适用范围：Project Copilot D1～D6，代码快照3f3159965b94655095acca599333e318dc5287b7。本文由Codex依据公开代码及真实开发记录整理，属于待人工核验的语料草稿，不是人工gold。

## 启动命令与执行顺序

在项目根目录执行 `uv run --frozen uvicorn project_copilot.main:app --host 127.0.0.1 --port 8002`。uv使用项目环境和锁定依赖，Uvicorn导入main模块并取得app；模块末尾的create_app构建应用、注册路由和中间件。构建应用时不连接模型、Qdrant或Phoenix。

进入lifespan启动阶段后，load_settings读取本项目配置，结果保存到application.state.settings。启动成功才开始处理请求；关闭时执行lifespan的finally并记录停止日志。用户D1真实操作已观察到启动完成及Ctrl+C后的正常关闭。

## 配置字段与启动失败

Settings使用PROJECT_COPILOT_前缀，默认服务名是Project Copilot，默认environment是development。app_name去除首尾空白后至少有一个字符；environment只接受development、test、production。

D1用户临时覆盖APP_NAME为Copilot Learning后，健康响应的service随之变化。将APP_NAME设为全空格，或将ENVIRONMENT设为staging，均触发Settings校验错误并使应用启动失败。命令前的临时变量只影响该次启动，不会改写配置文件。

## 健康检查的边界

GET /health从app.state.settings读取服务名和环境，返回status、service、environment。D1默认响应为HTTP200、status为ok。这个接口没有探测模型API或向量库，因此健康接口成功不证明外部依赖可用。

## 密钥检查发生在哪里

两家模型密钥字段允许缺省，应用可以在没有模型密钥时启动。真正创建Embedding或Chat客户端时，工厂才取出密钥并去除空白；空值会在发出模型请求前拒绝。这和app_name、environment在启动阶段的字段校验不同。

D2用户运行embedding_smoke.py时，不带--live得到退出码2；显式--live后返回一条2048维向量摘要；临时空白智谱密钥返回安全配置错误、退出码1。--live是命令行的显式实调开关，并不绕过密钥校验。

## 可追溯来源

- [启动入口与健康路由](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/src/project_copilot/main.py)
- [Settings配置契约](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/src/project_copilot/config.py)
- [模型客户端工厂](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/src/project_copilot/infrastructure/models.py)
- [D1真实验收记录](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/docs/reports/DAY_1.md)
- [D2真实验收记录](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/docs/reports/DAY_2.md)
