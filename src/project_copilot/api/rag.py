"""D5同步问答入口；请求体通过校验后才创建外部客户端。"""

import logging

from fastapi import APIRouter, HTTPException, Request

from project_copilot.infrastructure.models import (
    ChatConfigError, ChatRequestError, ChatResponseError,
    EmbeddingConfigError, EmbeddingRequestError, EmbeddingResponseError,
)
from project_copilot.infrastructure.qdrant import QdrantConfigError, QdrantRequestError, QdrantResponseError
from project_copilot.rag.answering import AnswerResponseError, create_answer_service
from project_copilot.rag.ingestion import ManifestError
from project_copilot.schemas.rag import RagAnswer, RagQuestion


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rag", tags=["rag"])


@router.post("/ask", response_model=RagAnswer)
def ask(payload: RagQuestion, request: Request) -> RagAnswer:
    # 同步SDK在FastAPI线程池中执行，不在async路由里阻塞事件循环。
    try:
        with create_answer_service(request.app.state.settings) as service:
            return service.answer(payload.question)
    except (ChatConfigError, EmbeddingConfigError, QdrantConfigError, ManifestError) as exc:
        logger.warning("RAG configuration failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="问答配置或索引清单不可用，请检查本项目配置") from None
    except (ChatRequestError, EmbeddingRequestError, QdrantRequestError) as exc:
        logger.warning("RAG upstream request failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="问答上游服务请求失败，请检查服务状态") from None
    except (ChatResponseError, EmbeddingResponseError, QdrantResponseError, AnswerResponseError) as exc:
        logger.warning("RAG upstream validation failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="问答上游结果未通过校验，请检查生成或检索结果") from None
