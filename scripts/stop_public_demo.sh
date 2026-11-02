#!/usr/bin/env bash
# Stop only containers bearing this isolated Compose project label.
# No `down`, no volume removal, and no dependency on secrets/env-file.
set -euo pipefail
project='project-copilot-public-demo'
found=0
for service in cloudflared api worker phoenix qdrant postgres; do
  if ! ids_output=$(docker ps -q --filter "label=com.docker.compose.project=${project}" \
      --filter "label=com.docker.compose.service=${service}"); then
    printf '%s\n' '无法读取 Docker 状态，停止操作已中断。' >&2
    exit 1
  fi
  if [[ -n "$ids_output" ]]; then
    found=1
    mapfile -t container_ids <<< "$ids_output"
    # Close ingress first; let the worker finish before stopping its storage.
    timeout=120
    if [[ "$service" == worker ]]; then timeout=360; fi
    docker stop --time "$timeout" "${container_ids[@]}" >/dev/null
  fi
done
if [[ "$found" == 0 ]]; then
  printf '%s\n' '未发现运行中的 public-demo 容器；未操作其他服务或数据卷。'
else
  printf '%s\n' '已停止 public-demo 容器；未删除任何容器或数据卷，也未停止本地开发服务。'
fi
