# 独立公开演示的资源、冷备和恢复

所有命令都只适用于专用公开演示资源，不用于已有本机数据库。当前没有发布网站或启动隧道；STAGING/PUBLIC 仍须用户显式授权和证据准入。

## 固定资源与预检

`docker-compose.public-demo.yml` 使用固定外部卷 `pc-public-v2-postgres`、`pc-public-v2-qdrant`、`pc-public-v2-phoenix`、`pc-public-v2-manifests`，以及固定外部网络 `pc-public-v2-backend` / `pc-public-v2-egress`。它们不随 `-p` 或 `COMPOSE_PROJECT_NAME` 改名，因此不会因为误用旧项目名就选择旧卷或旧数据库网络。

首次初始化前，先在本机保存权限为 600 的 `.env.public-demo`，数据库 URL 仅允许内部 `postgres:5432` 的 `project_copilot_public` 用户及数据库。密码采用 URL 安全随机字符并进行 URL 编码；环境文件不允许 `$` 插值表达式。脚本拒绝与文件冲突的已导出变量，并在调用 Compose 时清除应用/Compose 环境覆盖；再比较实际渲染的数据库 URL、数据库密码与 env_file 路径，不向终端输出秘密。

```bash
.venv/bin/python -m scripts.public_resources initialize
.venv/bin/python -m scripts.public_resources check
```

`initialize` 只创建缺失的固定名称资源，并附加 `io.project-copilot.scope=public-demo-v2` 与资源角色标签；遇到未标记、异常 driver/options 的同名卷或非专用网络便拒绝，不采用、不迁移、不删除。backend 网络由初始化脚本以 `--internal` 创建；Compose 对 external 网络本身不能配置 `internal`，预检会检查真实 Docker 网络的属性与消费者。预检还核对实际容器挂载、模型只读 bind 及网络，拒绝另一项目占用资源或额外挂载。旧公开演示前缀卷也不自动迁移。

基础准备使用 `./scripts/start_public_demo.sh --prepare`，仅准备数据库、Qdrant、Phoenix、迁移和 API，新库默认 CLOSED，API 返回 503/unhealthy 是预期。已有堆栈的授权不会被这个脚本主动修改，维护前须先检查并由用户显式CLOSED。它不会开启 worker 或隧道。用户通过交互式发布 CLI 完成另行授权后，`--serve` 先检查 worker 准入，再启动 worker，仍不启动隧道。无参数调用继续失败关闭。

## 一致备份边界

这是一套**停机冷备**，不声称多个运行中目录复制是事务一致的在线备份。

1. 管理员先通过交互式 `scripts.release_control CLOSED` 明确授权关闭；该操作同时禁用 worker。
2. `backup` 停止隧道、API 和 worker，检查没有剩余写入/维护容器；在数据库中获取发布变更排他锁，验证 CLOSED、worker disabled 和没有执行租约。
3. 在共享索引锁下只读验证：ORM/数据库结构、所有 ORM 业务表摘要、完成清单、Qdrant 原文/元数据/向量、BM25 快照，并用已有向量执行一次真实 Qdrant 查询、用原文执行 BM25 查询；不调用 Embedding、LLM 或 BGE 模型。
4. 停止 PostgreSQL 和 Qdrant。PostgreSQL 必须 exit 0；当前固定 Qdrant 镜像接受其正常 SIGTERM 退出 143，强杀/OOM 不接受。数据服务已离线后才归档三个卷（Manifest 与 BM25 在同一卷）。
5. 归档保留数字 UID/GID，记录 SHA-256、压缩大小与实际 datastore image ID；输出目录 700、文件 600。来源不会被自动重新启动。失败保留诊断和文件，不删除源卷。

不要在备份期间运行手工 SQL、`docker compose run --no-deps` 写操作、直接操作 Docker 或绕过脚本启动服务。宿主 `data/public-demo-operations.lock` 串行化官方启动、备份、恢复及备份集清理入口；任务维护和发布CLI还使用数据库锁，但操作人员仍不得并发执行这些运维写入。它不能约束具有 Docker/root 权限的人员绕过脚本，这是运维权限边界。

```bash
.venv/bin/python -m scripts.public_backup backup --path data/public-backups/backup-YYYYMMDD-01
.venv/bin/python -m scripts.public_backup restore --path data/public-backups/backup-YYYYMMDD-01
```

`restore` **只**创建全新 `pc-restore-<UUID>-*` 卷，不覆盖任何固定或现有卷，也不启动应用。它拒绝 checksum/大小错误、路径穿越、设备文件、软硬链接和超过 2 GiB 解压上限的归档。含 PostgreSQL tablespace 软链接的特殊部署需要单独审查，不自动恢复。

随后使用 restore 返回的本地 plan 路径执行：

```bash
.venv/bin/python -m scripts.public_backup verify-restored \
  --path data/public-backups/backup-YYYYMMDD-01 \
  --restore-plan data/public-backups/backup-YYYYMMDD-01/pc-restore-UUID.json
```

`verify-restored` 再核对归档大小/hash与 plan 的不可变身份绑定；使用相同 PostgreSQL/Qdrant image ID、全新隔离网络和恢复卷启动数据服务，只运行只读验证。恢复库必须 CLOSED，所有业务表、索引摘要和真实 Dense/BM25 查询排名必须与备份前一致，才标记 `verified=true`。数据服务与验证容器随后停止，新卷保留。任何失败都设置诊断保留标记，不自动重新执行不确定业务或切换原环境。Phoenix 数据不在此业务备份集合内；公开追踪如需要保留须另定经过验证的备份策略。

## 存储与保留

当前演示默认备份集合总预算为 2 GiB，单次压缩数据与解压大小也受限。读取归档流时到限即停止，保留部分文件，不自动删除未验证备份。清理是显式操作：

```bash
.venv/bin/python -m scripts.public_backup prune --path data/public-backups
```

仅删除超过最新 7 份或为满足总预算而可清理的**已验证**集合。`diagnostic_hold=true`、未验证、含额外调查文件的集合都不会自动删除；这些证据超预算时失败关闭，要求人工核查，而非腾空证据。备份也包含数据库历史 WAL 和认证/任务数据，应保存在受限本机目录或经过批准的加密备份介质，不提交 Git，不放到前端。

## 本轮真实演练范围

采用 public Compose 派生的全新随机资源，无宿主公开端口；在专用 PostgreSQL 写入一张合成工单，在真实 HTTP Qdrant 写入一段合成知识并同步 Manifest/BM25。实际执行了停机、三个卷冷备、恢复到另一组全新 UUID 卷、真实数据服务启动及摘要比对，全部通过。源码中的固定资源标识在合成演练驱动中替换为随机标识，未访问固定公开演示卷、旧项目卷、真实密钥或模型 API。源与恢复容器已停止，新卷保留；这不是公网预发布，也不是用户亲自手测。
