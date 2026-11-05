# STAGING 证据盘点与人工审核工作单

本页是审核说明，**不是发布许可**。基准 `80302f9`，2026-11-05。用户本轮明确报告真实 EdgeOne `health=503`、`auth/me=503`、`PATCH jobs=404`；Codex没有访问该站点、控制台或私有凭据。截图原文件及相应摘要尚未交付，不能填成机器可用的已审核工件。

## 证据状态

| 标识 | 含义 |
| --- | --- |
| HISTORICAL_REAL | 已提交、可核对的历史真实演练；须另审当前版本/资源适用性 |
| LOCAL_ONLY | 本地/合成/Mock结果，只证明相应测试合同 |
| NEEDS_USER_EVIDENCE | 用户已提供描述/输出，仍需本人整理脱敏原始证据及审核 |
| NEEDS_PLATFORM_TEST | 必须真实平台验证，不能用本地通过替代 |
| NEEDS_STAGING_E2E | 只有短期授权并连接上游后才能验证 |

| 必需 check | 可核实起点 | 当前缺口及最小补齐方式 |
| --- | --- | --- |
| identity_isolation | `tests/test_auth_postgres.py`、`test_public_agent_approvals.py` 等历史隔离测试；LOCAL_ONLY（相对于公网） | 审查相同版本的会话/owner/context合同、两名受限测试身份；真实浏览器跨账号Cookie/资源隔离留 NEEDS_STAGING_E2E，不提前写通过 |
| quotas_limits | 历史真实PG并发配额、Context/提案触发器，见 [阶段1.5报告](STAGE15_REPORT.md) | 当前安全配置指纹、独立数据库迁移/约束与预算适用性需审；真实Nginx/轮询和云函数用量留平台/窗口验收 |
| storage_restore | HISTORICAL_REAL：[冷备恢复收据](evidence/STAGE_1_5_BACKUP_RESTORE.json)，合成PG/Qdrant/Manifest/BM25恢复一致 | 收据的 `public_backup.py`、`public_resources.py`、Compose三个源码摘要均与当前基准不同。不得直接当本版本、本资源恢复证明；人工审查差异并决定是否另授权新隔离恢复演练。禁止覆盖现有卷 |
| retention_recovery | 历史PG保留/恢复与 [Docker停机探针](evidence/STAGE15_DOCKER_STOP_PROBE.json)；合成任务/停机inventory | 核对当前策略及恢复流程；没有真实供应商对账证据，不宣称解决真实unknown任务。不以年龄释放槽位 |
| proxy_limits | [来源最终整改](EDGEONE_ORIGIN_FINAL_FIX.md) 本地102 Node/40 Python；用户真实503/503/404 | NEEDS_USER_EVIDENCE：保存真实部署提交、时间、响应来源；NEEDS_PLATFORM_TEST：错误Origin写403、头/体积拒绝、额度监控。Cookie/超时/断线完整路径留STAGING |
| resource_isolation | 用户报告四个卷已归属验证；代码固定external卷/网络及消费者预检 | NEEDS_USER_EVIDENCE：当前 `public_resources check` 和只读预检脱敏结果、镜像/挂载身份；不重新初始化、不重建卷 |
| edge_protection | 默认域名受限STAGING保护代码、本地Nginx/代理合同；用户确认默认域名无精准限流/自定义规则 | 缺完整云端前置负例/用量/关闭演练、受审本地入口记录、六文件摘要及用户显式剩余风险接受。维护版移除旧函数路由尚需真实云端证明 |
| restricted_access | 邀请注册、持久STAGING业务白名单 | 指定1–20个**已有active**账号UUID，人工核对owner/角色；不得在本轮新建管理员/账号。业务白名单不等于所有非名单用户不能登录或读取自身me |

默认域名PUBLIC仍禁止。`public_e2e`、`cookies_csrf`、`offline_shutdown` 是其他域名PUBLIC的额外checks；其真实端到端语义也适用于后续STAGING验收，但**不要求先伪造它们才能开STAGING**。先审离线与前置证据，再在短窗口补全真实链路。

## 机器合同与人工合同

实际代码：`services/release_control.py::validate_evidence/change_state`、`services/entry_protection.py::validate_default_domain`。

- 授权文档 `format=1`、`state=STAGING`、resource_id、固定HTTPS origin、code/config fingerprint必须匹配实际容器。`verified_at`带时区且在当前前7天至当前；`expires_at`带时区且为将来，通用≤24h，默认edgeone.cool另限≤2h。
- checks **恰好上述8项**；每项恰 `path` / `sha256`，path是容器内绝对普通非符号链接文件，≤8MiB，摘要为64位小写hex且与实际字节相同。普通check的机器规则未拒空文件，人工审核必须拒绝空白/只有成功布尔值的“证明”。
- allowed_user_ids为1–20个去重UUID，授权时都存在且active；操作者另须active/admin、TTY密码、`AUTHORIZE STAGING`及8–1000字无秘密原因。程序不能代替本人确认。
- 默认域名edge_protection receipt恰有 `format/profile/origin/source_sha256/cloud_runtime_artifact/local_ingress_artifact/risk_acknowledgement`；profile是 `default-domain-bounded-v1`。两个嵌套artifact各恰path/sha256，必须非空、≤8MiB、非symlink、摘要正确。风险字符串须与源码常量逐字一致；**本轮不替用户填写或接受**。
- 8项摘要、时效和字段匹配不证明证据语义。不能把测试临时库的合成收据复制进真实授权目录。

指纹分开绑定：

| 指纹 | 实际覆盖 | 注意 |
| --- | --- | --- |
| code_fingerprint | `src/project_copilot/**/*.py`、`migrations/**/*.py`、`uv.lock`，相对路径+NUL+字节 | docs/tests/独立scripts改动不改变它；Git SHA不能替代此摘要 |
| configuration_fingerprint | `_SECURITY_FIELDS` 中预算、资源、origin、内部地址、超时、模型/缓存/trace设置 | 不含秘密；配置模型Key不会被摘要记录，仍须就绪检查。不能贴DSN/Key进工件 |
| source_sha256 | `cloud-functions/proxy.mjs`、`cloud-functions/api/[[default]].js`、`edgeone.json`、`deploy/nginx/public-demo.conf`、`deploy/nginx/Dockerfile`、`docker-compose.public-demo.yml` | 精确六文件；与实际部署/镜像逐项对应，不使用本地新文件冒充旧镜像 |

运行时重核代码/配置/资源/origin/期限/check集合及六入口摘要；不每次重读全部工件或刷新verified_at。工件变动须重新审核、重新授权，不借此隐式延长窗口。容器/数据库/卷真实身份仍单独检查，指纹不是完整存储归属证明。

## 私有收集流程（不生成授权）

所有记录只放已有 `data/release-evidence/`，目录700、文件600，由挂载只读映射至 `/app/release`。只读预检不创建/修权限；权限不符先停止，由用户另行修正。不要把私有文件提交GitHub。

1. 用户在本机整理现有截图/输出，记录操作者、UTC时间、站点origin、真实部署commit、环境（真实平台/本机/合成）、请求方法及**去除query的路径**、HTTP状态/脱敏detail、预期与实际、局限、原件的本地摘要。
2. 只允许必要的状态和用量数字。移除密码、API/proxy密钥、Cookie/CSRF、eo_token/eo_time、完整URL、真实Tunnel地址、账户标识/数据库内容和整份日志。截图也须检查地址栏、开发者工具和网络面板。
3. 时间/代码/资源无法核实或截图未取得，标 `NEEDS_USER_EVIDENCE`；不得填PASS。免费额度6/1,000,000、0.1/500,000GB-s、5/500构建只是用户报告的历史快照，不是实时余额/套餐承诺。
4. 对审核后的每个实际文件运行 `sha256sum data/release-evidence/<文件名>`，记录容器内对应 `/app/release/<文件名>`；复核容器可读且摘要相同。不要对秘密或数据库内容制作公开摘要清单。
5. 本人逐项审阅后，才人工编写机器授权文档与edge receipt；再次使用容器 `release_control inspect` 取得实际指纹。**本轮不生成staging.json，也不自动填风险接受。**

非授权工作记录模板（只可保存为独立 `*-worksheet.json`，不能提交给release_control）：

```json
{
  "kind": "staging-review-worksheet-v1",
  "authorization_permitted": false,
  "review_state": "NEEDS_USER_EVIDENCE",
  "observed_at_utc": null,
  "deployed_commit": null,
  "evidence_type": "user_report_not_independently_verified",
  "checks": {
    "health": {"reported_status": 503, "reviewed_artifact_sha256": null},
    "auth_me": {"reported_status": 503, "reviewed_artifact_sha256": null},
    "patch_jobs": {"reported_status": 404, "reviewed_artifact_sha256": null},
    "wrong_origin_post": {"reported_status": null, "status": "NEEDS_PLATFORM_TEST"}
  },
  "limitations": ["No release authorization", "No risk acceptance"]
}
```

缺 `format/state/resource_id/origin/fingerprints/8 checks/expiry/allowlist`，按现有校验必拒绝。不要用脚本自动升级工作记录为授权证据。
