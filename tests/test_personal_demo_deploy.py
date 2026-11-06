"""The optional image override must not alter isolation or start extra services."""
from pathlib import Path

import yaml


def test_personal_image_override_changes_images_and_bounded_worker_drain_only():
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "deploy/personal-demo.image.yml").read_text())
    assert config == {"services": {
        "api": {"image": "project-copilot-api:personal-demo"},
        "worker": {"image": "project-copilot-api:personal-demo", "stop_grace_period": "1260s"},
        "migrate": {"image": "project-copilot-api:personal-demo"},
    }}
    dockerfile = (root / "Dockerfile.personal-demo").read_text()
    lines = [line for line in dockerfile.splitlines() if line and not line.startswith("#")]
    assert lines == ["FROM project-copilot-api:public-demo", "WORKDIR /app",
                     "COPY --chown=1000:1000 src/ src/", "COPY --chown=1000:1000 scripts/ scripts/",
                     "COPY --chown=1000:1000 migrations/ migrations/"]
