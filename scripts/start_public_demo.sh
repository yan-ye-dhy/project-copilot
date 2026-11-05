#!/usr/bin/env bash
# Prepare isolated storage/API while CLOSED. Never authorize a release or tunnel.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
case "${1:-}" in
  --prepare|--serve) operation="$1" ;;
  *) printf '%s\n' '公开演示尚不可启动：请先审核发布清单，再显式选择 --prepare（CLOSED基础服务）或 --serve（已授权worker）。' >&2; exit 1 ;;
esac
if [[ $# != 1 ]]; then printf '%s\n' '不接受额外 Compose 参数或项目名覆盖。' >&2; exit 2; fi
python='.venv/bin/python'
if [[ ! -x "$python" ]]; then printf '%s\n' '请先安装项目锁定依赖，准备 .venv。' >&2; exit 1; fi
mkdir -p data
exec 9>data/public-demo-operations.lock
flock -n 9 || { printf '%s\n' '另一个部署/备份操作正在运行。' >&2; exit 1; }
"$python" -m scripts.public_resources check
compose=(docker compose --env-file .env.public-demo -f docker-compose.public-demo.yml)
# Preflight rejects conflicting values; remove all app/Compose interpolation
# selectors as a second defence before executing the reviewed env-file.
while IFS='=' read -r key _; do
  case "$key" in PROJECT_COPILOT_*|COMPOSE_*) unset "$key" ;; esac
done < <(env)
if [[ "$operation" == --prepare ]]; then
  "${compose[@]}" up -d --wait postgres qdrant phoenix
  "${compose[@]}" up -d --build migrate api ingress
  printf '%s\n' '基础服务已准备；默认CLOSED，API unhealthy/503是预期。未启动worker或隧道；授权变更请用交互式release_control。'
else
  "${compose[@]}" exec -T api python -m scripts.public_probe worker-ready
  "${compose[@]}" up -d worker
  printf '%s\n' 'worker已按当前发布授权启动；未启动任何公网隧道。'
fi
