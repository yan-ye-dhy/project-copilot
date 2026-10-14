"""D5单条固定RAG问答；--live同时允许查询Embedding及DeepSeek生成。"""

import argparse
import json
import sys

from pydantic import ValidationError

from project_copilot.config import load_settings
from project_copilot.infrastructure.models import (
    ChatConfigError, ChatRequestError, ChatResponseError,
    EmbeddingConfigError, EmbeddingRequestError, EmbeddingResponseError,
)
from project_copilot.infrastructure.qdrant import QdrantConfigError, QdrantRequestError, QdrantResponseError
from project_copilot.rag.answering import AnswerResponseError, create_answer_service
from project_copilot.rag.ingestion import ManifestError


SMOKE_QUESTION = "样本文档第一页描述了什么功能？"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="允许本次查询Embedding和DeepSeek问答")
    args = parser.parse_args()
    if not args.live:
        parser.error("真实问答需显式添加--live；本脚本仅执行一条固定问题，不入库或自动重试")
    try:
        with create_answer_service(load_settings()) as service:
            result = service.answer(SMOKE_QUESTION)
    except (
        ValidationError, ChatConfigError, ChatRequestError, ChatResponseError,
        EmbeddingConfigError, EmbeddingRequestError, EmbeddingResponseError,
        QdrantConfigError, QdrantRequestError, QdrantResponseError, ManifestError, AnswerResponseError,
    ):
        print(json.dumps({"status": "error", "message": "问答未完成，请检查配置、索引或上游结果"},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    print(result.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
