"""D4单文件入库或Dense检索；收费Embedding须显式添加--live。"""

import argparse
import json
import sys
import time

from pydantic import ValidationError

from project_copilot.config import load_settings
from project_copilot.infrastructure.models import (
    EmbeddingConfigError, EmbeddingRequestError, EmbeddingResponseError, create_embedding_client,
)
from project_copilot.infrastructure.qdrant import (
    QdrantConfigError, QdrantRequestError, QdrantResponseError, create_qdrant_store,
)
from project_copilot.rag.ingestion import IngestionError, IngestionService, ManifestError, ManifestStore
from project_copilot.rag.loaders import DocumentLoadError
from project_copilot.rag.retrieval import DenseRetriever
from project_copilot.rag.splitters import DocumentSplitError, SplitConfig
from project_copilot.schemas.retrieval import DenseIndexSpec


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="顺序索引一个本地文件")
    ingest.add_argument("path")
    search = commands.add_parser("search", help="查询完成清单内的证据")
    search.add_argument("query")
    for command in (ingest, search):
        command.add_argument("--live", action="store_true", help="允许本次真实Embedding调用")
    args = parser.parse_args()
    if not args.live:
        parser.error("真实Embedding需显式添加--live；本脚本只处理单文件或单条问题")

    started = time.monotonic()
    try:
        settings = load_settings()
        split_config = SplitConfig()
        spec = DenseIndexSpec(
            collection_name=settings.qdrant_collection_name,
            embedding_model=settings.embedding_model,
            dimensions=settings.embedding_expected_dimensions,
            splitter_version=split_config.index_version,
        )
        manifest = ManifestStore(settings.manifest_dir, spec)
        with create_embedding_client(settings) as embeddings, create_qdrant_store(settings) as store:
            if args.command == "ingest":
                completed = IngestionService(
                    embeddings, store, manifest, split_config=split_config,
                ).ingest_file(args.path)
                result = {"document": completed.model_dump(mode="json"),
                          "chunk_count": len(completed.chunk_ids)}
            else:
                hits = DenseRetriever(
                    embeddings, store, manifest, top_k=settings.rag_top_k,
                ).search(args.query)
                result = {"query": args.query, "top_k": settings.rag_top_k,
                          "hit_count": len(hits), "hits": [h.model_dump(mode="json") for h in hits]}
    except (
        ValidationError, EmbeddingConfigError, EmbeddingRequestError, EmbeddingResponseError,
        QdrantConfigError, QdrantRequestError, QdrantResponseError, DocumentLoadError,
        DocumentSplitError, IngestionError, ManifestError, ValueError,
    ) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", "mode": "DENSE", "collection": spec.collection_name,
                      "index_version": spec.index_version, "manifest": str(manifest.path),
                      "elapsed_seconds": round(time.monotonic() - started, 4), **result},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
