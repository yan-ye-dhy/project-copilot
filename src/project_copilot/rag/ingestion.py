"""单文件顺序入库：加载→切分→Embedding→Qdrant→成功清单。"""

import os
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from project_copilot.config import Settings
from project_copilot.infrastructure.models import create_embedding_client
from project_copilot.infrastructure.qdrant import DenseQdrantStore, create_qdrant_store
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


class EmptyDocumentError(IngestionError):
    """文档解析成功，但没有可索引的非空文本。"""


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

    def ingest_file(self, path: str | Path, *, source_name: str | None = None) -> CompletedDocument:
        if source_name is not None and (not isinstance(source_name, str) or not source_name.strip()):
            raise IngestionError("文档展示名称必须是非空字符串")
        document = load_document(path)
        if source_name is not None:
            # 上传使用服务端临时路径；展示名称不参与文件或片段的稳定身份。
            document = document.model_copy(update={"source_name": source_name})
        chunks = split_document(document, self.split_config)
        if not chunks:
            raise EmptyDocumentError("文档没有可索引的非空文本，未发Embedding请求")
        previous = self.manifest.load()
        # 先验证存储契约，避免集合不兼容时仍产生Embedding费用。
        self.store.ensure_collection()
        completed = next((item for item in previous.documents if item.document_id == document.document_id), None)
        if completed is not None:
            if completed.content_hash != document.content_hash or completed.chunk_ids != tuple(
                chunk.metadata.chunk_id for chunk in chunks
            ):
                raise ManifestError("已完成文档的内容或片段身份与当前切分不一致")
            # 同内容改名不创建别名或改写已有证据；复用第一次完成时的来源名。
            canonical = [chunk.model_copy(update={"metadata": chunk.metadata.model_copy(
                update={"source_name": completed.source_name},
            )}) for chunk in chunks]
            self.store.verify_chunks(canonical, dense_index_version=self.manifest.spec.index_version)
            return completed
        vectors = self.embeddings.embed_documents([chunk.text for chunk in chunks])
        self.store.upsert(chunks, vectors, dense_index_version=self.manifest.spec.index_version)
        # completed响应后仍回读精确片段；失败点可残留，但未登记文档不能进入检索清单。
        self.store.verify_chunks(chunks, dense_index_version=self.manifest.spec.index_version)
        completed = CompletedDocument(
            document_id=document.document_id, content_hash=document.content_hash,
            source_name=document.source_name,
            chunk_ids=tuple(chunk.metadata.chunk_id for chunk in chunks),
        )
        self.manifest.record(previous, completed)
        return completed


@contextmanager
def create_ingestion_service(settings: Settings) -> Iterator[IngestionService]:
    """复用D4单文件链路；请求结束或失败时释放Embedding和Qdrant客户端。"""
    split_config = SplitConfig()
    spec = DenseIndexSpec(
        collection_name=settings.qdrant_collection_name,
        embedding_model=settings.embedding_model,
        dimensions=settings.embedding_expected_dimensions,
        splitter_version=split_config.index_version,
    )
    manifest = ManifestStore(settings.manifest_dir, spec)
    with create_embedding_client(settings) as embeddings, create_qdrant_store(settings) as store:
        yield IngestionService(embeddings, store, manifest, split_config=split_config)
