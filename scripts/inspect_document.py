"""离线查看本地文档、切分结果与来源；不创建模型或向量库客户端。"""

import argparse
import json
import sys

from pydantic import ValidationError

from project_copilot.rag.loaders import DocumentLoadError, load_document
from project_copilot.rag.splitters import DocumentSplitError, SplitConfig, split_document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="本地TXT、UTF-8 Markdown或有文字层的PDF")
    parser.add_argument("--chunk-size", type=int, default=750, help="片段字符上限，默认750")
    parser.add_argument("--chunk-overlap", type=int, default=150, help="目标重叠字符数，必须小于上限")
    args = parser.parse_args()
    try:
        config = SplitConfig(chunk_size=args.chunk_size, chunk_overlap=args.chunk_overlap)
        document = load_document(args.path)
        chunks = split_document(document, config)
    except (DocumentLoadError, DocumentSplitError, ValidationError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    result = {
        "status": "ok" if chunks else "empty",
        "document": document.model_dump(mode="json"),
        "splitter": {**config.model_dump(mode="json"), "index_version": config.index_version},
        "chunk_count": len(chunks),
        "max_chunk_chars": max((len(chunk.text) for chunk in chunks), default=0),
        "chunks": [chunk.model_dump(mode="json") for chunk in chunks],
    }
    if not chunks:
        result["message"] = "没有可切分的非空文本；PDF仅提取文字层，不执行OCR。"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
