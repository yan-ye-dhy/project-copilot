#!/usr/bin/env bash
# Fail closed until the user authorizes publication and the code is audited.
set -euo pipefail
printf '%s\n' \
  '公开演示尚不可启动：安全验收未完成，公网入口仍返回 503。' \
  '本脚本没有启动容器、隧道或云服务，也没有修改现有数据。' \
  '请先完成 docs/SECURITY_MODEL.md 中的发布前检查，再按后续经过审查的流程操作。' >&2
exit 1
