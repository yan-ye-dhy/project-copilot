# 可复现验证

## 1. 目标

同一入口用于本机与GitHub Actions，区分纯离线、受控浏览器、真实隔离存储和未执行的模型/GPU验收。CI不部署、不读取Secrets、不访问演示数据库。

## 2. 文件

根目录`package.json`/`package-lock.json`锁定仅开发用Playwright；`.node-version`固定已验证的Node版本。`scripts/verify.mjs`统一调用静态构建、Node测试及pytest。生产静态产物仍只含构建白名单文件，不包含Node依赖。

## 3. 干净检出后的运行

安装`.node-version`中的Node及uv 0.11.32后：

```bash
npm ci --ignore-scripts
npx --no-install playwright install --with-deps chromium
npm run verify
```

首次依赖安装需要网络；测试本身不用模型密钥。Python通过`uv run --frozen --no-default-groups --group dev`安装锁定依赖，避免自动下载GPU组。已有虚拟环境可显式设`PC_TEST_PYTHON=/绝对路径/.venv/bin/python`。本机原有Playwright缓存可设`PLAYWRIGHT_BROWSERS_PATH`；不再依赖私人`PLAYWRIGHT_MODULE`路径。

## 4. 分层与边界

| 命令 | 证明什么 | 不证明什么 |
| --- | --- | --- |
| `npm run verify -- --frontend` | 真实静态构建、Node代理回环、Chromium受控业务响应、IndexedDB与布局 | 真实公网、真实模型和真实业务写入 |
| `npm run verify -- --python` | Python默认单测/合同测试；输出全部跳过原因 | PostgreSQL、HTTP Qdrant、GPU或供应商可用性 |
| `npm run verify -- --services` | 显式启用真实隔离PG、Redis Lua、HTTP Qdrant、上传/删除、Graph/事务回归；模型仍为替身 | 真实模型质量、GPU和公网链路 |

`--services`只连接已准备的独立测试服务，不自动启动Docker：PG为127.0.0.1:5434的`project_copilot_test`角色/数据库；每个测试仅创建和删除它自己新建的随机数据库。Qdrant需全新临时实例，`PROJECT_COPILOT_QDRANT_URL=http://127.0.0.1:16433`；旧上传测试固定用16433。不要指向演示集合。CI服务随job销毁，不挂载任何业务卷。

第一周升级后，服务回归还要求独立Redis：`redis://127.0.0.1:16379/15`。测试只清理自己新建的随机namespace，不执行FLUSHDB/FLUSHALL；默认离线入口强制关闭真实Redis测试。需要统一准备时可显式运行 `docker compose -p pc-week1-lab -f docker-compose.week1-lab.yml --profile full-regression up -d --wait`；该文件使用tmpfs，无业务env/模型/持久化卷。Redis原生进程重启专项另需`PROJECT_COPILOT_TEST_REDIS_SERVER_BIN`指向显式测试二进制；没有时如实跳过，不重启共享服务。

## 5. 自动测试结果

2026-11-10后端工程升级的最终本机/CI统计、真实服务版本及合成压测见[本批验证记录](WEEK1_BACKEND_VERIFICATION.md)。下文保留既有历史批次原结果，不替新代码背书。

每轮真实结果在PR及GitHub Checks中记录；本文件不把计划写成通过。JUnit和静态构建保存在独立`test-results/verify-*`目录，CI保留7天。使用`-ra`列出跳过项；GPU、真实供应商、Docker ingress与公网E2E不因CI绿色而被标为已验证。

2026-11-07，Codex在按锁文件新建的Python 3.13 dev-only环境执行最终合并代码：`npm run verify -- --services` **1954通过、0失败、10跳过**（364.49秒；真实隔离PostgreSQL与HTTP Qdrant，模型替身）；`npm run verify -- --frontend` **317通过、0失败、0跳过**，包含实际Chromium、代理回环和11文件静态构建。跳过的是1项GPU与9项Docker入口专项；没有真实供应商请求。这些是本机结果；GitHub Actions结果以对应提交的独立运行记录为准。

首次完整执行修复了三处测试环境依赖：Alembic使用当前Python解释器而非固定`.venv`路径；隔离Qdrant客户端不继承宿主代理；历史退役测试在随机测试数据库中按历史时间装载既存提案，避免今日的Context过期门禁阻止测试数据准备。生产迁移、过期门禁和恢复规则均未修改。

## 6. Git与发布

`.github/workflows/checks.yml`在面向main的PR、main推送和手动dispatch时执行三项独立检查：前端/Chromium、离线Python、真实隔离PostgreSQL与Qdrant。工作流只有`contents: read`权限，Actions固定提交SHA，无Secrets、部署或付费模型步骤，测试产物保留7天；同一分支的新运行取消被替代的旧检查。最初推送因缺少`workflow`权限被拒绝，用户完成正常GitHub授权后通过独立PR启用。每次是否通过须核对该提交的Checks，不能将本机通过称为云端CI通过。EdgeOne既有main自动部署独立运行，也不能把CI成功当成EdgeOne发布成功。

## 7. 复习重点

锁文件和运行时版本、服务隔离、模型替身的证明边界、测试跳过与验收通过的区别。参考[GitHub服务容器](https://docs.github.com/en/actions/tutorials/use-containerized-services/create-postgresql-service-containers)及[Playwright CI](https://playwright.dev/docs/ci)。

## 8. 后续

Agent任务评测和本地模拟演示将沿用同一验证入口；真实模型采集仍须独立预算和显式运行，不加入PR自动测试。
