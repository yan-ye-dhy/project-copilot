"""Fixed synthetic cases, split by scenario family rather than random paraphrases."""

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from project_copilot.agent.state import GraphLimits
from project_copilot.schemas.agent import AgentQuestion

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = ROOT / "evals/datasets/agent_tasks_v1/cases.json"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    failure_type: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    event_failures: list[str] = Field(default_factory=list)
    max_tool_executions: int = Field(ge=0)
    max_model_calls: int = Field(ge=0)
    proposal_count: int = Field(default=0, ge=0, le=1)


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    family: str
    split: Literal["dev", "holdout"]
    category: Literal["knowledge", "ticket", "joint", "conditional_proposal", "insufficient", "exception"]
    question: str
    history: list[dict[str, str]] = Field(default_factory=list)
    allow_proposal: bool = False
    limits: dict[str, int | float] = Field(default_factory=dict)
    script: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    expected: Contract
    review_rubric: str

    @model_validator(mode="after")
    def validate_contract(self):
        AgentQuestion(question=self.question, history=self.history)
        GraphLimits(**self.limits)
        if not self.script or not self.review_rubric.strip():
            raise ValueError("script and human review rubric are required")
        return self


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def load_dataset(path: Path = DEFAULT_DATASET) -> tuple[list[Case], str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != "agent_tasks_v1" or payload.get("synthetic") is not True:
        raise ValueError("expected the synthetic agent_tasks_v1 dataset")
    cases = [Case.model_validate(item) for item in payload["cases"]]
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("duplicate case id")
    families: dict[str, str] = {}
    for case in cases:
        if families.setdefault(case.family, case.split) != case.split:
            raise ValueError("scenario family leaks across splits")
    dataset_hash = digest(payload)
    manifest_path = path.with_name("frozen_manifest.json")
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["dataset_sha256"] != dataset_hash:
            raise ValueError("frozen dataset fingerprint mismatch")
    return cases, dataset_hash
