#!/usr/bin/env bash
# Local GPU deployment shortcut; preserve the existing demo index and volumes.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

compose=(docker compose --env-file .env.example)
if [[ -f data/auto-final/deploy.env ]]; then
  compose+=(--env-file data/auto-final/deploy.env)
else
  export PROJECT_COPILOT_CONTAINER_UID="${PROJECT_COPILOT_CONTAINER_UID:-$(id -u)}"
  export PROJECT_COPILOT_CONTAINER_GID="${PROJECT_COPILOT_CONTAINER_GID:-$(id -g)}"
fi

# Recreate application mounts after a Docker Desktop/WSL restart.
"${compose[@]}" \
  -f docker-compose.yml -f docker-compose.deploy.yml -f docker-compose.gpu.yml \
  up -d --wait --no-build --pull never --force-recreate migrate api

printf '\n工作台：http://127.0.0.1:8002/\n追踪：http://127.0.0.1:6007/\n'
