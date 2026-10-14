"""单文件顺序入库：加载→切分→Embedding→Qdrant→成功清单。"""

import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from project_copilot.infrastructure.qdrant import DenseQdrantStore
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import SplitConfig, split_document
from project_copilot.schemas.retrieval import CompletedDocument, DenseIndexSpec, IndexManifest


class Embeddings(Protocol):
    model: str
    expected_dimensions: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class IngestionError(RuntimeError):
    """文档不能进入当前索引。"""


class ManifestError(RuntimeError):
    """完成清单读取、契约或保存失败。"""


class ManifestStore:
    """一个集合一份本地完成清单；D4只支持顺序单进程入库。"""

    def __init__(self, directory: Path, spec: DenseIndexSpec) -> None:
        self.spec = spec
        self.path = directory / f"{spec.collection_name}.json"

    def load(self) -> IndexManifest:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return IndexManifest(spec=self.spec)
        except (OSError, UnicodeError):
            raise ManifestError("无法读取索引完成清单") from None
        try:
            manifest = IndexManifest.model_validate_json(raw)
        except ValidationError:
            raise ManifestError("索引完成清单格式不合法") from None
        if manifest.spec != self.spec:
            raise ManifestError("索引配置与完成清单不一致，请使用新集合重新索引")
        return manifest

    def record(self, manifest: IndexManifest, document: CompletedDocument) -> None:
        if manifest.spec != self.spec:
            raise ManifestError("不能向不同配置的完成清单写入")
        documents = tuple(d for d in manifest.documents if d.document_id != document.document_id)
        updated = IndexManifest(spec=self.spec, documents=documents + (document,))
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=".manifest-", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(updated.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            # 同一文件系统原子替换，避免读到半份JSON；不是跨存储事务。
            temporary.replace(self.path)
        except OSError:
            raise ManifestError("无法保存索引完成清单；本次不登记入库成功") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # 临时文件清理失败不覆盖原始保存错误或已成功的替换。
                    pass


def validate_index_binding(
    embeddings: Embeddings, store: DenseQdrantStore, spec: DenseIndexSpec
) -> None:
    if (embeddings.model, embeddings.expected_dimensions) != (spec.embedding_model, spec.dimensions):
        raise IngestionError("Embedding模型或维度与索引配置不一致")
    if (store.collection_name, store.dimensions, store.vector_name) != (
        spec.collection_name, spec.dimensions, spec.vector_name,
    ):
        raise IngestionError("Qdrant适配与索引配置不一致")


class IngestionService:
    def __init__(
        self, embeddings: Embeddings, store: DenseQdrantStore,
        manifest: ManifestStore, *, split_config: SplitConfig | None = None,
    ) -> None:
        self.embeddings = embeddings
        self.store = store
        self.manifest = manifest
        self.split_config = split_config or SplitConfig()
        validate_index_binding(embeddings, store, manifest.spec)
        if self.split_config.index_version != manifest.spec.splitter_version:
            raise IngestionError("切分配置与当前索引不一致，请使用新集合")

    def ingest_file(self, path: str | Path) -> CompletedDocument:
        document = load_document(path)
        chunks = split_document(document, self.split_config)
        if not chunks:
            raise IngestionError("文档没有可索引的非空文本，未发Embedding请求")
        previous = self.manifest.load()
        # 先验证存储契约，避免集合不兼容时仍产生Embedding费用。
        self.store.ensure_collection()
        vectors = self.embeddings.embed_documents([chunk.text for chunk in chunks])
        self.store.upsert(chunks, vectors, dense_index_version=self.manifest.spec.index_version)
        completed = CompletedDocument(
            document_id=document.document_id, content_hash=document.content_hash,
            source_name=document.source_name,
            chunk_ids=tuple(chunk.metadata.chunk_id for chunk in chunks),
        )
        self.manifest.record(previous, completed)
        return completed
