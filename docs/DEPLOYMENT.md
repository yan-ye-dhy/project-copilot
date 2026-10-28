# Project Copilot Docker 部署

本机完整栈包括API、一次性Alembic迁移、PostgreSQL、Qdrant和Phoenix。业务检索始终为Dense 10＋BM25 10→RRF 10→固定BGE重排5；CPU与GPU只是同一重排模型的显式运行方式。源码位于项目容器镜像，真实密钥、索引快照和模型缓存仅在运行时挂载。

## 1. 运行前准备

在项目根目录操作。Docker Desktop需启用当前WSL集成。保留现有`.env`，仅补充缺少的智谱和DeepSeek变量，参考[配置模板](../.env.example)，不要用模板覆盖真实文件。

```bash
cd /home/yanye/project-copilot
test -f .env
mkdir -p data/manifests
test -d models/huggingface
export PROJECT_COPILOT_CONTAINER_UID="$(id -u)"
export PROJECT_COPILOT_CONTAINER_GID="$(id -g)"
export PROJECT_COPILOT_VCS_REF="$(git rev-parse HEAD)"
```

UID/GID使非root API进程能够写入本机清单目录；模型缓存只读。运行前必须准备`BAAI/bge-reranker-v2-m3`固定revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`的完整Hugging Face缓存，当前约2.2GB。应用使用local_files_only且不自动下载模型。缓存缺失会使检索明确失败。

Compose插值使用`.env.example`中的本地演示值，真正的项目`.env`以只读文件挂到`/app/.env`，其中的API Key不会进入构建上下文或镜像层。容器环境覆盖数据库和Qdrant URL为服务名：`postgres:5432`、`qdrant:6333`；不要在容器内使用宿主的127.0.0.1访问这两个服务。

默认数据库仍为项目已有的`project_copilot`，口令是原本机演示值。若现有卷使用其他口令，应同时为Compose设置实际`PROJECT_COPILOT_POSTGRES_PASSWORD`和包含URL编码密码的完整`PROJECT_COPILOT_DOCKER_DATABASE_URL`。修改环境变量不会重置已有卷中的数据库口令。

## 2. 显式GPU完整部署

GPU模式需要Docker的NVIDIA支持及兼容CUDA 13的驱动，构建使用锁定的rerank-gpu组。可先用无数据挂载的临时容器检查运行时：

```bash
docker run --rm --gpus all ubuntu:22.04 nvidia-smi
docker compose --env-file .env.example \
  -f docker-compose.yml -f docker-compose.deploy.yml -f docker-compose.gpu.yml \
  build api
docker compose --env-file .env.example \
  -f docker-compose.yml -f docker-compose.deploy.yml -f docker-compose.gpu.yml \
  up -d --wait api
```

API等待PostgreSQL、Qdrant和Phoenix健康、迁移容器成功退出之后启动。GPU参数固定为CUDA float16、batch2、max_length512。模型在首次检索加载和预热，每应用进程复用一个实例；单进程GPU内存和请求延迟以实际验收为准。多个API进程会各自占用模型显存；当前演示默认只有一个进程。故障时不会自动切换CPU或纯Dense。

## 3. 显式CPU完整部署

不具备GPU时省略GPU overlay：

```bash
docker compose --env-file .env.example \
  -f docker-compose.yml -f docker-compose.deploy.yml build api
docker compose --env-file .env.example \
  -f docker-compose.yml -f docker-compose.deploy.yml up -d --wait api
```

默认镜像`project-copilot-api:cpu`安装CPU torch组并设置cpu/float32，仍执行完整Hybrid＋BGE流水线。CPU会占用更多主机内存且重排较慢；CPU运行不作为D17的GPU性能复现。GPU镜像为`project-copilot-api:gpu`，两种依赖组分别构建，避免混装。

## 4. 端口、健康与追踪

| 服务 | 宿主地址 | 容器地址与用途 |
| --- | --- | --- |
| API | `127.0.0.1:8002` | `/health`用于存活检查；首次模型加载在实际检索时发生 |
| PostgreSQL | `127.0.0.1:5433` | `postgres:5432`，真实工单与提案 |
| Qdrant | `127.0.0.1:6433` | `qdrant:6333`，`/readyz`用于就绪检查 |
| Phoenix | `127.0.0.1:6007` | `phoenix:6006`，`/healthz`和`/v1/traces` |

```bash
docker compose --env-file .env.example \
  -f docker-compose.yml -f docker-compose.deploy.yml -f docker-compose.gpu.yml ps
curl --noproxy 127.0.0.1 --fail http://127.0.0.1:8002/health
curl --noproxy 127.0.0.1 --fail http://127.0.0.1:6007/healthz
```

部署overlay默认开启应用追踪，`PROJECT_COPILOT_DOCKER_TRACING_ENABLED=false`可显式关闭；容器内OTLP HTTP/protobuf目标是`http://phoenix:6006/v1/traces`，默认项目名`project-copilot`。Phoenix使用独立`phoenix_data`卷保存本地追踪数据库；业务工单始终存于PostgreSQL。界面仅绑定本机且当前未启用Phoenix登录，不适合直接暴露到不可信网络。

## 5. 数据与可覆盖配置

| 宿主变量 | 默认值/职责 |
| --- | --- |
| `PROJECT_COPILOT_ENV_FILE` | `./.env`，只读凭据文件 |
| `PROJECT_COPILOT_MANIFEST_HOST_DIR` | `./data/manifests`，读写成功清单、锁与BM25快照 |
| `PROJECT_COPILOT_MODEL_CACHE_HOST_DIR` | `./models/huggingface`，只读固定模型缓存 |
| `PROJECT_COPILOT_API_PORT` | `8002`，仅绑定127.0.0.1 |
| `PROJECT_COPILOT_PHOENIX_PORT` | `6007`，避免与其他项目Phoenix混用 |
| `PROJECT_COPILOT_QDRANT_COLLECTION_NAME` | `pc_knowledge_dense_v0`，必须与该清单匹配 |
| `PROJECT_COPILOT_PHOENIX_PROJECT` | `project-copilot`，追踪项目标识 |
| `PROJECT_COPILOT_CPU_THREADS` | `2`，限制OMP/MKL线程，避免本机多任务争用 |

路径挂载必须已经存在；Compose不会自动把缺失凭据文件创建成目录。新项目继续复用原`project-copilot_postgres_data`和`project-copilot_qdrant_data`命名卷，不将已有数据库、模型或向量烘焙进镜像。自动验收可指定新的清单目录与新Qdrant集合，与旧索引分开，但仍须保留旧数据。

## 6. 停止、重建与持久化

正常停止只关闭容器：

```bash
docker compose --env-file .env.example \
  -f docker-compose.yml -f docker-compose.deploy.yml -f docker-compose.gpu.yml \
  stop api postgres qdrant phoenix
```

之后使用相同部署命令`up -d --wait api`可恢复。源码变化后先`build api`，再`up -d --wait --force-recreate api`重建API；工单和向量仍来自原服务卷，索引快照和模型来自原挂载。数据库迁移通过独立migrate服务显式执行；再次执行upgrade head是幂等的。禁止使用带卷删除选项的清理命令。保留手测工单及提案，不自动删除演示数据。

## 7. 故障定位与边界

- API无法启动：查看`logs api migrate`、确认UID/GID、路径存在、数据库URL为容器服务名；健康状态不证明模型缓存已可用。
- Qdrant或数据库连接失败：检查对应健康状态、容器网络和已有数据库实际口令，不重建或删除数据卷。
- Rerank失败：确认固定revision完整缓存挂载、本机GPU运行时和镜像依赖组；GPU不可用时显式选择CPU镜像及CPU配置，不能把降级当成功GPU验收。
- Trace缺失：核对应用开关、Phoenix健康、OTLP endpoint和导出诊断；日志run_id不是Phoenix已接收Trace的证明。
- 模型上游失败：按run_id定位，保持零自动重试和原失败记录；不要在未分析错误前重复付费场景。

本次CPU镜像首次在官方PyTorch下载站遇到TLS握手失败；只读确认官方包可直接访问后，清空构建代理参数的一次重试成功，未修改锁文件包地址或关闭TLS校验。遇到相同代理问题时可执行：

```bash
docker compose --env-file .env.example \
  -f docker-compose.yml -f docker-compose.deploy.yml \
  build --build-arg HTTP_PROXY= --build-arg HTTPS_PROXY= --build-arg ALL_PROXY= api
```

若你的网络必须使用代理，应提供容器可以访问的受信任代理地址；不要把密钥写进Dockerfile或将代理认证信息提交Git。构建下载重试与业务模型请求不同，业务SDK仍为max_retries=0。

当前仍为本地单用户工程演示：原`POST /tickets`直建接口没有完整身份鉴权，D29上下文凭据也不是用户登录或租户隔离。对外部署前需为全部写接口及Phoenix补充真实认证和网络边界。

部署参考：[Phoenix官方自托管说明](https://arize.com/docs/phoenix/self-hosting/deploying-phoenix)、[官方环境配置源码](https://github.com/Arize-ai/phoenix/blob/main/src/phoenix/config.py)。本仓库Phoenix镜像固定为20.16.0对应digest，Python基础镜像也固定digest；应用依赖以`uv.lock`为准。
