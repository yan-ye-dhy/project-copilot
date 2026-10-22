"""D13中文BM25：分词、本地索引与词项检索，不执行模型调用。"""

import argparse
from collections.abc import Sequence
from contextlib import closing
import json
from pathlib import Path
import sys

from pydantic import ValidationError
from qdrant_client import QdrantClient

from project_copilot.rag.bm25 import (
    BM25Error, BM25Index, ChineseTokenizer, read_completed_chunks,
)
from project_copilot.schemas.retrieval import IndexManifest


def _read_manifest(path: Path) -> IndexManifest:
    # 不沿用ManifestStore的“文件不存在即空清单”，避免错误地建出空索引。
    try:
        return IndexManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError):
        raise BM25Error("无法读取有效的索引完成清单") from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    tokenize = commands.add_parser("tokenize", help="观察中文与技术标识符的实际分词")
    tokenize.add_argument("text")
    build = commands.add_parser("build", help="只读现有Qdrant成功片段，保存独立BM25快照")
    build.add_argument("--manifest", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path)
    build.add_argument("--qdrant-url", default="http://127.0.0.1:6433")
    search = commands.add_parser("search", help="从本地快照检索，不连接Qdrant或模型")
    search.add_argument("query")
    search.add_argument("--manifest", required=True, type=Path)
    search.add_argument("--index", required=True, type=Path)
    search.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args(argv)

    try:
        tokenizer = ChineseTokenizer()
        if args.command == "tokenize":
            result = {"tokens": tokenizer.tokenize(args.text), "tokenizer": tokenizer.spec}
        else:
            manifest = _read_manifest(args.manifest)
            if args.command == "build":
                if args.output.exists():
                    raise BM25Error("BM25快照已存在，不能覆盖")
                with closing(QdrantClient(
                    url=args.qdrant_url, check_compatibility=False, trust_env=False,
                    timeout=10, prefer_grpc=False,
                )) as client:
                    chunks = read_completed_chunks(client, manifest)
                index = BM25Index.build(manifest, chunks)
                index.save(args.output)
                snapshot = index.snapshot.model_dump(mode="json")
                result = {
                    "documents": len(manifest.documents), "chunks": len(chunks),
                    "collection": manifest.spec.collection_name,
                    "dense_index_version": manifest.spec.index_version,
                    "tokenizer": snapshot["tokenizer"], "config": snapshot["config"],
                    "fingerprint": index.fingerprint,
                }
            else:
                index = BM25Index.load(args.index, manifest)
                hits = index.search(args.query, top_k=args.top_k)
                result = {
                    "query": args.query, "query_tokens": tokenizer.tokenize(args.query),
                    "top_k": args.top_k, "hit_count": len(hits),
                    "hits": [hit.model_dump(mode="json") for hit in hits],
                    "fingerprint": index.fingerprint,
                    "score_note": "BM25分数是词项相关度，不是Dense分数或答案置信度",
                }
    except Exception:
        # 不回显SDK响应、URL凭据或损坏文件中的原始内容。
        print(json.dumps({
            "status": "error", "message": "BM25处理失败；请检查完成清单、快照文件及Qdrant连接",
            "mode": "local_bm25", "model_calls": 0,
        }, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", "mode": "local_bm25", "model_calls": 0, **result},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
