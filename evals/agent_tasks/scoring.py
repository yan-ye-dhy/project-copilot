"""Offline scoring separates graph contracts, terminal failures and human factual review."""

from collections import Counter

from pydantic import BaseModel, ConfigDict, Field

from project_copilot.schemas.agent import AgentRun

from .dataset import Case, digest


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message_chars: int = Field(ge=0)
    tool_schema_chars: int = Field(ge=0)
    tool_observation_chars: int = Field(ge=0)
    distinct_tool_observation_chars: int = Field(ge=0)
    tool_message_count: int = Field(ge=0)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    case_id: str
    result: AgentRun
    prompt_observations: list[Observation] | None = None
    backend_executions: list[dict] | None = None
    unmatched_fixtures: list[dict] | None = None
    unused_script_steps: int | None = Field(default=None, ge=0)
    external_model_requests: int | None = Field(default=None, ge=0)
    real_service_executions: int | None = Field(default=None, ge=0)


def fraction(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else None}


def validate_record(row: dict) -> Record:
    record = Record.model_validate(row)
    run = record.result
    if (run.tool_executions != sum(event.executed for event in run.events)
            or run.tool_calls < len(run.events)
            or run.tool_failures < sum(event.status == "error" for event in run.events)
            or run.tool_failures > run.tool_calls or run.tool_executions > run.tool_calls
            or run.model_calls != len(run.model_events)):
        raise ValueError("inconsistent recorded event counters")
    if record.prompt_observations is not None and len(record.prompt_observations) != run.model_calls:
        raise ValueError("prompt observations must cover every model attempt or be null")
    if record.backend_executions is not None and len(record.backend_executions) != run.tool_executions:
        raise ValueError("backend execution ledger mismatch")
    if run.usage is not None:
        usage = run.usage
        if (any(type(value) is not int or value < 0 for value in usage.values())
                or not {"prompt_tokens", "completion_tokens", "total_tokens"} <= usage.keys()
                or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]):
            raise ValueError("invalid recorded usage")
    return record


def check_contract(case: Case, record: Record | None) -> list[str]:
    if record is None:
        return ["missing_record"]
    expected, run = case.expected, record.result
    checks = {
        "status": run.status == expected.status,
        "failure_type": run.failure_type == expected.failure_type,
        "source_ids": {source.id for source in run.sources} == set(expected.source_ids),
        "required_tools": set(expected.required_tools) <= {
            event.name for event in run.events if event.executed and event.status == "ok"},
        "event_failures": set(expected.event_failures) <= {event.failure_type for event in run.events},
        "tool_execution_budget": run.tool_executions <= expected.max_tool_executions,
        "model_call_budget": run.model_calls <= expected.max_model_calls,
        "proposal_count": sum(event.name == "propose_ticket" and event.executed and event.status == "ok"
                              for event in run.events) == expected.proposal_count,
        "no_direct_write_execution": not any(event.executed and event.name in {
            "create_ticket", "confirm", "confirm_action", "execute_sql", "http_request"} for event in run.events),
        "fixtures_matched": not record.unmatched_fixtures,
        "script_consumed": record.unused_script_steps in (None, 0),
    }
    return [name for name, passed in checks.items() if not passed]


def _measured(records, field, expected_records):
    values = [getattr(record, field) for record in records]
    known = [value for value in values if value is not None]
    return {"total": sum(known) if len(known) == expected_records and values else None,
            "known_sum": sum(known), "measured_records": len(known), "record_denominator": expected_records}


def score(cases: list[Case], dataset_sha256: str, bundle: dict, *, split: str = "all",
          reviews: list[dict] | None = None) -> dict:
    if split not in {"dev", "holdout", "all"}:
        raise ValueError("unknown score split")
    if bundle.get("schema_version") != 1:
        raise ValueError("unsupported records schema")
    provenance = bundle["provenance"]
    if provenance["dataset_sha256"] != dataset_sha256:
        raise ValueError("recorded results belong to a different dataset")
    if provenance.get("execution_mode") not in {"scripted_contract", "recorded_external"}:
        raise ValueError("execution mode must be explicit")
    for field in ("code_sha256", "config_sha256", "model_decisions"):
        if not isinstance(provenance.get(field), str) or not provenance[field]:
            raise ValueError("missing recorded provenance: " + field)
    if "config" in provenance and digest(provenance["config"]) != provenance["config_sha256"]:
        raise ValueError("configuration fingerprint mismatch")
    all_ids = {case.id for case in cases}
    records = {}
    for row in bundle["records"]:
        record = validate_record(row)
        if record.case_id not in all_ids or record.case_id in records:
            raise ValueError("unknown or duplicate recorded case id")
        if provenance["execution_mode"] == "scripted_contract" and (
                record.external_model_requests != 0 or record.real_service_executions != 0):
            raise ValueError("scripted contracts must declare zero external requests and services")
        records[record.case_id] = record
    selected = [case for case in cases if split == "all" or case.split == split]
    selected_ids = {case.id for case in selected}
    review_map = {}
    for review in reviews or []:
        cid = review["case_id"]
        if cid not in all_ids or cid in review_map or cid not in records:
            raise ValueError("unknown, duplicate or missing-record human review")
        if (review.get("dataset_sha256") != dataset_sha256
                or review.get("answer_sha256") != digest(records[cid].result.answer)
                or review.get("evidence_sha256") != digest([source.model_dump(mode="json") for source in records[cid].result.sources])
                or review.get("verdict") not in {"supported", "unsupported", "unassessable"}
                or review.get("reviewer_kind") != "human"
                or not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip()):
            raise ValueError("human review must bind the dataset, exact answer and evidence")
        review_map[cid] = review
    details = []
    for case in selected:
        record = records.get(case.id)
        failures = check_contract(case, record)
        details.append({"case_id": case.id, "family": case.family, "split": case.split,
                        "category": case.category, "contract_pass": not failures,
                        "failed_checks": failures,
                        "terminal_status": record.result.status if record else None,
                        "failure_type": record.result.failure_type if record else None,
                        "answer_sha256": digest(record.result.answer) if record else None,
                        "evidence_sha256": digest([source.model_dump(mode="json") for source in record.result.sources]) if record else None,
                        "human_factual_support": review_map.get(case.id, {}).get("verdict"),
                        "review_rubric": case.review_rubric})
    present = [records[cid] for cid in sorted(selected_ids & records.keys())]
    runs = [record.result for record in present]
    attempts = sum(run.model_calls for run in runs)
    observations = [observation for record in present for observation in record.prompt_observations or []]
    usage_known = [run.usage for run in runs if run.usage is not None]
    complete_usage = len(usage_known) == len(selected) and bool(runs)
    usage = ({key: sum(item[key] for item in usage_known)
              for key in set.intersection(*(set(item) for item in usage_known))} if complete_usage else None)
    eligible = [detail for detail in details if detail["terminal_status"] in {"answered", "pending_confirmation"}]
    reviewed = [item for item in eligible if item["human_factual_support"] is not None]
    assessable = [item for item in reviewed if item["human_factual_support"] != "unassessable"]
    groups = {}
    for dimension in ("split", "category", "family"):
        groups[dimension] = {}
        for value in sorted({detail[dimension] for detail in details}):
            group = [detail for detail in details if detail[dimension] == value]
            groups[dimension][value] = fraction(sum(item["contract_pass"] for item in group), len(group))
    n = len(details)
    return {"schema_version": 1, "provenance": provenance, "score_split": split,
            "evidence_boundary": "Scripted contracts test the compiled graph, not autonomous model decisions. "
                                 "Imported records are externally supplied evidence, not independently verified live runs.",
            "selected_cases": n, "record_coverage": fraction(len(present), n),
            "contract_success": fraction(sum(item["contract_pass"] for item in details), n),
            "contract_failure": fraction(sum(not item["contract_pass"] for item in details), n),
            "terminal_failures": fraction(sum(run.status in {"failed", "budget_exhausted"} for run in runs), n),
            "terminal_status_counts": dict(Counter(run.status for run in runs)),
            "failure_type_counts": dict(Counter(run.failure_type for run in runs if run.failure_type)),
            "model_decision_success": None,
            "counts": {"model_calls": attempts, "tool_attempts": sum(run.tool_calls for run in runs),
                       "tool_handler_executions": sum(run.tool_executions for run in runs),
                       "tool_failures": sum(run.tool_failures for run in runs),
                       "external_model_requests": _measured(present, "external_model_requests", n),
                       "real_service_executions": _measured(present, "real_service_executions", n)},
            "prompt_characters": {"unit": "Unicode characters; not provider tokens or full wire bytes",
                "observation_coverage": fraction(len(observations), attempts),
                "known_message_chars": sum(item.message_chars for item in observations),
                "known_tool_schema_chars": sum(item.tool_schema_chars for item in observations),
                "known_tool_observation_chars": sum(item.tool_observation_chars for item in observations),
                "known_distinct_tool_observation_chars": sum(item.distinct_tool_observation_chars for item in observations),
                "max_tool_observation_chars": max((item.tool_observation_chars for item in observations), default=None)},
            "usage": usage, "usage_coverage": fraction(len(usage_known), n),
            "human_factual_support": {"reviewed": fraction(len(reviewed), len(eligible)),
                "supported": fraction(sum(item["human_factual_support"] == "supported" for item in assessable), len(assessable)),
                "pending": len(eligible) - len(reviewed), "unassessable": len(reviewed) - len(assessable),
                "boundary": "Source identity and contract checks do not establish factual support. Only explicit human labels count."},
            "groups": groups, "cases": details}


def compare(before: dict, after: dict) -> dict:
    """Show controlled deltas without calling character reduction model improvement."""
    for field in ("dataset_sha256", "config_sha256", "execution_mode"):
        if before["provenance"][field] != after["provenance"][field]:
            raise ValueError("comparison requires matching " + field)
    if (before["score_split"] != after["score_split"]
            or [item["case_id"] for item in before["cases"]] != [item["case_id"] for item in after["cases"]]
            or before["record_coverage"]["rate"] != 1 or after["record_coverage"]["rate"] != 1):
        raise ValueError("comparison requires the same complete case denominator")
    if any(item["prompt_characters"]["observation_coverage"]["rate"] != 1 for item in (before, after)):
        raise ValueError("character comparison requires observations for every model attempt")
    if before["provenance"].get("dependencies") != after["provenance"].get("dependencies"):
        raise ValueError("comparison requires matching dependency versions")
    fields = ("known_message_chars", "known_tool_observation_chars", "known_distinct_tool_observation_chars")
    return {"before_code_sha256": before["provenance"]["code_sha256"],
            "after_code_sha256": after["provenance"]["code_sha256"],
            "denominator": before["selected_cases"],
            "contract_success_delta": after["contract_success"]["numerator"] - before["contract_success"]["numerator"],
            "tool_execution_delta": after["counts"]["tool_handler_executions"] - before["counts"]["tool_handler_executions"],
            "prompt_character_deltas": {field: after["prompt_characters"][field] - before["prompt_characters"][field]
                                        for field in fields},
            "claim_boundary": "A scripted replay delta is not a measured model-quality improvement."}
