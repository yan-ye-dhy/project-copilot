"""The optional image override must not alter isolation or start extra services."""
from pathlib import Path, PurePosixPath
import shlex

import pytest
import yaml

from project_copilot.services.entry_protection import ENTRY_SOURCES


ROOT = Path(__file__).resolve().parents[1]


def _context_includes(relative):
    """Evaluate this repository's ordered glob/negation rules, including parents."""
    path = PurePosixPath(relative)
    candidates = [path, *(parent for parent in path.parents if str(parent) != ".")]
    included = True
    for line in (ROOT / ".dockerignore").read_text().splitlines():
        rule = line.strip()
        if not rule or rule.startswith("#"):
            continue
        negated = rule.startswith("!")
        pattern = (rule[1:] if negated else rule).strip("/")
        if any(candidate.full_match(pattern) for candidate in candidates):
            included = negated
    return included


def _local_copy_destinations(relative):
    """Resolve shell-form local COPY inputs to their destinations under /app."""
    required = PurePosixPath(relative)
    for line in (ROOT / "Dockerfile.personal-demo").read_text().splitlines():
        parts = shlex.split(line, comments=True)
        if not parts or parts[0].upper() != "COPY":
            continue
        assert not any(part.startswith("--from=") for part in parts), "entry sources must come from this checkout"
        inputs = [part for part in parts[1:] if not part.startswith("--")]
        sources, destination = inputs[:-1], inputs[-1]
        for source in sources:
            local = PurePosixPath(source)
            if (ROOT / local).is_dir() and required.is_relative_to(local):
                yield PurePosixPath("/app") / destination / required.relative_to(local)
            elif required == local:
                target = PurePosixPath(destination)
                if destination.endswith("/"):
                    target /= required.name
                yield PurePosixPath("/app") / target


def test_personal_image_override_changes_images_and_bounded_worker_drain_only():
    config = yaml.safe_load((ROOT / "deploy/personal-demo.image.yml").read_text())
    assert config == {"services": {
        "api": {"image": "project-copilot-api:personal-demo"},
        "worker": {"image": "project-copilot-api:personal-demo", "stop_grace_period": "1260s"},
        "migrate": {"image": "project-copilot-api:personal-demo"},
    }}
    dockerfile = (ROOT / "Dockerfile.personal-demo").read_text()
    lines = [line for line in dockerfile.splitlines() if line and not line.startswith("#")]
    assert lines == ["FROM project-copilot-api:public-demo", "WORKDIR /app",
                     "COPY --chown=1000:1000 src/ src/", "COPY --chown=1000:1000 scripts/ scripts/",
                     "COPY --chown=1000:1000 migrations/ migrations/",
                     "COPY --chown=1000:1000 cloud-functions/ cloud-functions/",
                     "COPY --chown=1000:1000 edgeone.json docker-compose.public-demo.yml ./",
                     "COPY --chown=1000:1000 deploy/nginx/ deploy/nginx/"]


@pytest.mark.parametrize("relative", ENTRY_SOURCES)
def test_every_runtime_entry_fingerprint_input_is_refreshed_from_build_context(relative):
    # Follow the runtime security contract, so a newly fingerprinted file cannot
    # silently remain inherited from an older dependency image.
    assert (ROOT / relative).is_file()
    assert _context_includes(relative), f"entry source excluded from Docker context: {relative}"
    assert PurePosixPath("/app") / relative in set(_local_copy_destinations(relative))


@pytest.mark.parametrize("relative", [
    ".env", ".env.public-demo", "src/.env", "cloud-functions/.env.private", "deploy/nginx/.env",
])
def test_entry_source_copy_does_not_admit_environment_files(relative):
    # Only virtual paths are checked; never open an actual credential file.
    assert not _context_includes(relative)
