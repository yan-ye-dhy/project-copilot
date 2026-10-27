"""API和验收脚本共用的Agent装配；模型/权限/预算由后端决定。"""

from contextlib import asynccontextmanager

from project_copilot.agent.backend import ReadOnlyBackend
from project_copilot.agent.graph import AgentRunner, GraphLimits
from project_copilot.agent.tools import build_readonly_registry, build_proposal_registry
from project_copilot.agent.proposals import ProposalBackend
from project_copilot.config import Settings
from project_copilot.db.session import Database
from project_copilot.infrastructure.agent_model import create_agent_model


def graph_limits(settings: Settings) -> GraphLimits:
    return GraphLimits(max_model_calls=settings.agent_max_model_calls,
                       max_tool_calls=settings.agent_max_tool_calls,
                       tool_timeout_seconds=settings.agent_tool_timeout_seconds,
                       model_timeout_seconds=settings.agent_model_timeout_seconds,
                       total_timeout_seconds=settings.agent_total_timeout_seconds)


@asynccontextmanager
async def create_agent_service(settings: Settings, database: Database | None, *, context=None):
    backend = ProposalBackend(settings, database, *context) if context else ReadOnlyBackend(settings, database)
    if context:
        backend.authenticate()
    async with create_agent_model(settings) as model:
        yield AgentRunner(model, build_proposal_registry(backend) if context else build_readonly_registry(backend), graph_limits(settings))
