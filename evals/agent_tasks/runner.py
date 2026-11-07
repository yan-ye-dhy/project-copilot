"""Execute the real compiled Agent graph with an explicit script and in-memory tools."""

import asyncio
import json
import inspect
import subprocess
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage

from project_copilot.agent.graph import AgentRunner, SYSTEM_PROMPT
from project_copilot.agent.state import GraphLimits
from project_copilot.agent.tools import build_proposal_registry, build_readonly_registry
from project_copilot.schemas.agent import AgentQuestion

from .dataset import Case, ROOT, digest


def fingerprint() -> dict:
    # Resolve the imported production graph, including helper modules, rather
    # than guessing that PYTHONPATH points at this evaluator's checkout.
    agent_directory = Path(inspect.getfile(AgentRunner)).resolve().parent
    production_root = agent_directory.parents[2]
    evaluator_paths = sorted((ROOT / "evals/agent_tasks").glob("*.py"))
    production_paths = sorted(agent_directory.glob("*.py")) + [
        agent_directory.parent / "schemas" / name for name in ("agent.py", "tickets.py")]
    code = {str(path.relative_to(ROOT)): digest(path.read_text(encoding="utf-8")) for path in evaluator_paths}
    code.update({str(path.relative_to(production_root)): digest(path.read_text(encoding="utf-8"))
                 for path in production_paths})

    def git_head(directory):
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=directory,
                              capture_output=True, text=True, check=False)
        return head.stdout.strip() if head.returncode == 0 else None

    return {"code_sha256": digest(code), "files": code,
            "git_head": git_head(ROOT), "production_git_head": git_head(production_root),
            "code_locations": {"evaluator_root": str(ROOT), "production_root": str(production_root)},
            "dependencies": {name: version(name) for name in ("langgraph", "langchain-core", "pydantic")}}


def prompt_observation(messages, tools) -> dict:
    # Strip random LangGraph IDs; count Unicode characters, never call them tokens.
    payloads = [json.dumps(message.model_dump(mode="json", exclude={"id"}), ensure_ascii=False,
                           sort_keys=True, separators=(",", ":")) for message in messages]
    observations = [message.content for message in messages if isinstance(message, ToolMessage)]
    schema = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"message_chars": sum(map(len, payloads)), "tool_schema_chars": len(schema),
            "tool_observation_chars": sum(map(len, observations)),
            "distinct_tool_observation_chars": sum(map(len, set(observations))),
            "tool_message_count": len(observations)}


class ScriptModel:
    def __init__(self, script):
        self.script = list(script)
        self.observations = []

    async def ainvoke(self, messages, tools):
        self.observations.append(prompt_observation(messages, tools))
        if not self.script:
            raise RuntimeError("synthetic script exhausted")
        item = self.script.pop(0)
        if "raise" in item:
            error = TimeoutError if item["raise"] == "timeout" else RuntimeError
            raise error("synthetic model failure")
        return AIMessage(content=item.get("content", ""), tool_calls=item.get("tool_calls", []),
                         response_metadata={"model": "scripted-contract-stub"})


class SyntheticBackend:
    """Exact normalized argument matching; no database, retrieval service or provider."""
    def __init__(self, fixtures):
        self.fixtures = fixtures
        self.executions = []
        self.unmatched = []

    async def invoke(self, name, args):
        args = json.loads(json.dumps(args, default=str))
        self.executions.append({"name": name, "args": args})
        for fixture in self.fixtures:
            if fixture["name"] == name and fixture["args"] == args:
                if "raise" in fixture:
                    error = {"not_found": LookupError, "timeout": TimeoutError}.get(fixture["raise"], RuntimeError)
                    raise error("synthetic tool failure")
                return fixture["result"]
        self.unmatched.append({"name": name, "args": args})
        raise RuntimeError("no matching synthetic tool fixture")

    async def get_ticket(self, ticket_id):
        return await self.invoke("get_ticket", {"ticket_id": str(ticket_id)})

    async def search_tickets(self, status=None, q=None, limit=5):
        return await self.invoke("search_tickets", {"status": status, "q": q, "limit": limit})

    async def search_knowledge(self, query):
        return await self.invoke("search_knowledge", {"query": query})

    async def propose_ticket(self, title, description):
        return await self.invoke("propose_ticket", {"title": title, "description": description})


async def run_case(case: Case) -> dict:
    model = ScriptModel(case.script)
    backend = SyntheticBackend(case.tools)
    registry = (build_proposal_registry if case.allow_proposal else build_readonly_registry)(backend)
    runner = AgentRunner(model, registry, GraphLimits(**case.limits))
    result = await runner.run(AgentQuestion(question=case.question, history=case.history), "contract-" + case.id)
    return {"case_id": case.id, "result": result.model_dump(mode="json"),
            "prompt_observations": model.observations, "backend_executions": backend.executions,
            "unmatched_fixtures": backend.unmatched, "unused_script_steps": len(model.script),
            "external_model_requests": 0, "real_service_executions": 0}


def run_contract(cases: list[Case], dataset_sha256: str, split: str) -> dict:
    selected = [case for case in cases if split == "all" or case.split == split]
    config = {"default_limits": asdict(GraphLimits()), "system_prompt_sha256": digest(SYSTEM_PROMPT),
              "split": split, "case_limits": {case.id: case.limits for case in selected},
              "runner": "actual_compiled_langgraph", "backend": "synthetic_in_memory",
              "model": "scripted-contract-stub"}

    async def execute():
        return [await run_case(case) for case in selected]

    return {"schema_version": 1, "provenance": {"execution_mode": "scripted_contract",
            "model_decisions": "preset_script_not_model_decisions", "dataset_sha256": dataset_sha256,
            "config": config, "config_sha256": digest(config), **fingerprint()},
            "records": asyncio.run(execute())}
