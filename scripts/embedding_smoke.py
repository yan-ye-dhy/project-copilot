"""仅在显式--live时，用一条固定中文文本请求智谱Embedding。"""

import argparse
import json
import sys

from pydantic import ValidationError

from project_copilot.config import load_settings
from project_copilot.infrastructure.models import (
    EmbeddingConfigError,
    EmbeddingRequestError,
    EmbeddingResponseError,
    create_embedding_client,
)


SMOKE_TEXT = "Project Copilot 用研发知识帮助排查问题。"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="允许一次真实Embedding请求")
    args = parser.parse_args()
    if not args.live:
        parser.error("真实请求需显式添加--live；本脚本不执行批量索引或自动重试")

    try:
        with create_embedding_client(load_settings()) as embeddings:
            vector = embeddings.embed_query(SMOKE_TEXT)
            result = {
                "status": "ok",
                "model": embeddings.model,
                "texts": 1,
                "dimensions": len(vector),
                "encoding_format": "float",
            }
    except (ValidationError, EmbeddingConfigError, EmbeddingRequestError, EmbeddingResponseError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
