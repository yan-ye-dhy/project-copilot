"""有预算的只读Agent入口；运行状态与工具错误保留在结构化结果中。"""

import logging

from fastapi import APIRouter, HTTPException, Request

from project_copilot.agent.service import create_agent_service
from project_copilot.api.actions import credentials
from project_copilot.services.approvals import ContextRejected
from sqlalchemy.exc import SQLAlchemyError
from project_copilot.infrastructure.models import ChatConfigError
from project_copilot.schemas.agent import AgentQuestion, AgentRun


router = APIRouter(prefix="/agent", tags=["agent"])
logger = logging.getLogger(__name__)


@router.post("/ask", response_model=AgentRun)
async def ask_agent(payload: AgentQuestion, request: Request) -> AgentRun:
    context = credentials(request, optional=True)
    try:
        options = {"context": context} if context else {}
        async with create_agent_service(request.app.state.settings, request.app.state.database, **options) as service:
            result = await service.run(payload, run_id=request.state.run_id)
    except ContextRejected:
        raise HTTPException(status_code=403, detail="上下文凭据无效或已过期") from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="数据库操作失败，请根据run_id排查") from None
    except ChatConfigError:
        raise HTTPException(status_code=503, detail="Agent模型配置不可用，请检查本项目配置") from None
    # No messages, tool arguments, evidence text or provider diagnostics enter logs.
    logger.info("agent run_id=%s status=%s model_calls=%d tool_calls=%d elapsed_ms=%.2f failure_type=%s trace_exported=false",
                result.run_id, result.status, result.model_calls, result.tool_calls,
                result.elapsed_ms, result.failure_type)
    for event in result.events:
        logger.info("agent_tool run_id=%s call_id=%s name=%s status=%s failure_type=%s elapsed_ms=%.2f",
                    result.run_id, event.tool_call_id, event.name, event.status, event.failure_type, event.elapsed_ms)
    return result
