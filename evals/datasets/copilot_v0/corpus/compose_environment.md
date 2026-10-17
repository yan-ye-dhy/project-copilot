# Compose环境与服务边界笔记

资料核对日期：2026-10-16。适用范围：Docker Compose命令行与本项目Qdrant v1.19.2服务，项目快照3f3159965b94655095acca599333e318dc5287b7。Docker资料为核对当日官方文档，项目未记录Compose客户端精确版本。本文是Codex自编中文来源草稿，待人工核验，不是Docker官方文档译文或人工gold。

## 命令行变量文件

docker compose --env-file .env.example up -d qdrant中的--env-file是Compose命令行选项，不要求YAML存在同名字段。它指定Compose读取的变量文件，供解析${变量名}之类占位符使用；不会仅凭这个选项就把所有变量注入容器。[官方变量替换说明](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)

## 容器环境变量

服务的environment可以直接声明容器环境变量，也可引用Compose先解析出的值；服务的env_file让Compose从指定文件取得要传给该容器的变量。命令行--env-file与服务env_file属于不同配置层次，不能因名称相似而等同。[官方容器变量说明](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/)

## 当前项目为什么可以省略

截至D6，docker-compose.yml只有qdrant服务，镜像是qdrant/qdrant:v1.19.2；ports、volumes都是固定值，没有环境变量占位符，也没有服务级environment或env_file。因此--env-file .env.example不会改变该YAML中镜像、端口或卷的这些值。当前也可用docker compose up -d qdrant启动同一个服务。

这不影响宿主机运行的Python应用：应用仍由自己的Settings读取本项目配置。FastAPI没有被该Compose文件定义成容器，不能把容器启动误解为同时启动了本项目API。

## 端口和持久卷

端口127.0.0.1:6433:6333表示宿主机回环地址的6433端口转发到容器6333端口，访问范围限制在本机绑定地址。qdrant_data:/qdrant/storage把命名卷挂到容器数据目录，容器与持久数据有各自生命周期，排障时不能随意删除卷。[端口定义](https://docs.docker.com/reference/compose-file/services/#ports)、[卷定义](https://docs.docker.com/reference/compose-file/volumes/)

## Qdrant与FastAPI的边界

D6用户看到容器Up后，访问127.0.0.1:6433/readyz得到HTTP200及all shards are ready。该端口的readyz、collections和points/count均由Qdrant服务提供，不需要在本项目FastAPI里重写。项目API另由Uvicorn监听127.0.0.1:8002，提供health、documents/upload和rag/ask。Qdrant就绪只表示该存储服务的就绪检查成功，不能证明模型密钥或回答内容正确。

## 项目来源

- [固定的Compose配置](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/docker-compose.yml)
- [D6用户操作与验收记录](https://github.com/yan-ye-dhy/project-copilot/blob/3f3159965b94655095acca599333e318dc5287b7/docs/reports/DAY_6.md)
