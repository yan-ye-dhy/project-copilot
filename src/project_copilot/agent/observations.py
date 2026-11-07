"""Lossless model-only projection; full sources remain in the graph's trusted state."""
import json

from project_copilot.agent.tools import ToolResult


def _identity(value) -> str:
    # Canonical JSON avoids both dictionary ordering differences and Python's
    # True == 1 equivalence. Non-finite values fail just as in the wire encoder.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":"))


def model_observation(result: ToolResult) -> dict:
    """Replace a source body only when its exact JSON is already in data.

    data_ref is a local JSON pointer, never a URL or a request to execute a tool.
    Unmatched source bodies are retained, and no summarization/truncation occurs.
    This does not mutate the validated tool result or user-visible sources.
    """
    payload = result.model_dump(mode="json")
    data = payload["data"]
    references = {_identity(data): "/data"}
    if isinstance(data, list):
        for index, item in enumerate(data):
            references.setdefault(_identity(item), f"/data/{index}")
    sources = []
    for source in payload["sources"]:
        reference = references.get(_identity(source["data"]))
        if reference is None:
            sources.append(source)
        else:
            sources.append({"id": source["id"], "kind": source["kind"], "data_ref": reference})
    return {"status": "ok", "data": data, "sources": sources}
