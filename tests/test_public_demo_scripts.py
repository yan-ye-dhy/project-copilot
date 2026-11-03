"""Safety properties of public-demo scripts using a fake Docker executable."""
import os
from pathlib import Path
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_public_compose_pins_tunnel_and_keeps_datastores_internal():
    config = yaml.safe_load((ROOT / "docker-compose.public-demo.yml").read_text(encoding="utf-8"))
    services = config["services"]
    assert services["cloudflared"]["image"] == (
        "cloudflare/cloudflared:2026.10.3@sha256:"
        "faa8e0a1475f137c910be6025f43c8e4092fdd35461b31e19fd1390657a1ff39"
    )
    assert services["cloudflared"]["profiles"] == ["tunnel"]
    assert services["cloudflared"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert services["worker"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert services["cloudflared"]["networks"] == ["egress"]
    assert config["networks"]["backend"] == {"name": "pc-public-v2-backend", "external": True}
    assert config["networks"]["egress"] == {"name": "pc-public-v2-egress", "external": True}
    for name in ("postgres", "qdrant", "phoenix"):
        assert services[name]["networks"] == ["backend"]
        assert not services[name].get("ports")
    assert not services["api"].get("ports")
    assert "/health" in services["api"]["healthcheck"]["test"][-1]
    assert "X-Project-Copilot-Proxy-Secret" in services["api"]["healthcheck"]["test"][-1]


def test_start_remains_fail_closed():
    result = subprocess.run(["bash", "scripts/start_public_demo.sh"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 1 and "尚不可启动" in result.stderr


def test_stop_is_scoped_and_stops_ingress_and_worker_before_storage(tmp_path):
    log = tmp_path / "docker.log"
    fake = tmp_path / "docker"
    fake.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$DOCKER_TEST_LOG"
if [[ "$1" == ps ]]; then
  case "$*" in
    *service=cloudflared*) echo tunnel-id ;;
    *service=api*) echo api-id ;;
    *service=worker*) echo worker-id ;;
    *service=phoenix*) echo phoenix-id ;;
    *service=qdrant*) echo qdrant-id ;;
    *service=postgres*) echo postgres-id ;;
  esac
fi
''', encoding="utf-8")
    fake.chmod(0o700)
    result = subprocess.run(["bash", "scripts/stop_public_demo.sh"], cwd=ROOT, capture_output=True, text=True,
                            env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
                                 "DOCKER_TEST_LOG": str(log)})
    assert result.returncode == 0
    lines = log.read_text(encoding="utf-8").splitlines()
    probes = [line for line in lines if line.startswith("ps ")]
    stops = [line for line in lines if line.startswith("stop ")]
    assert len(probes) == len(stops) == 6
    assert all("label=com.docker.compose.project=project-copilot-public-demo" in line for line in probes)
    assert stops == ["stop --time 120 tunnel-id", "stop --time 120 api-id", "stop --time 360 worker-id",
                     "stop --time 120 phoenix-id", "stop --time 120 qdrant-id", "stop --time 120 postgres-id"]
    assert not any("volume" in line or " down" in line for line in lines)


def test_stop_aborts_when_docker_status_cannot_be_read(tmp_path):
    fake = tmp_path / "docker"
    fake.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    fake.chmod(0o700)
    result = subprocess.run(["bash", "scripts/stop_public_demo.sh"], cwd=ROOT, capture_output=True, text=True,
                            env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]})
    assert result.returncode != 0 and "无法读取 Docker 状态" in result.stderr
