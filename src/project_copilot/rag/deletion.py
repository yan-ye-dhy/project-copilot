"""Crash-recoverable deletion from the completed manifest, Qdrant and BM25."""

from hashlib import sha256
import os
import tempfile
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, ValidationError
from qdrant_client import models

from project_copilot.rag.bm25 import read_completed_chunks
from project_copilot.rag.final_retrieval import SnapshotManager
from project_copilot.rag.index_lock import index_lock
from project_copilot.rag.ingestion import ManifestStore
from project_copilot.schemas.documents import DocumentChunk
from project_copilot.schemas.retrieval import IndexManifest


class DeletionError(RuntimeError):
    """No result may be served while a deletion is in doubt."""


class DocumentNotFound(LookupError):
    pass


class Journal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: int = 1
    document_id: UUID
    before: IndexManifest


def _sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class DocumentDeletion:
    def __init__(self, manifest: ManifestStore, client):
        self.manifest = manifest
        self.client = client

    def _read_journal(self):
        try:
            journal = Journal.model_validate_json(self.manifest.deletion_journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValidationError):
            raise DeletionError("删除日志损坏，必须人工排查") from None
        if journal.format_version != 1 or journal.before.spec != self.manifest.spec:
            raise DeletionError("删除日志与当前索引不匹配")
        documents = [d for d in journal.before.documents if d.document_id == journal.document_id]
        if len(documents) != 1:
            raise DeletionError("删除日志文档身份无效")
        return journal, documents[0]

    def _write_journal(self, journal):
        path = self.manifest.deletion_journal_path
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".delete-journal-", delete=False) as stream:
                temporary = stream.name
                stream.write(journal.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            # Atomic exclusive publish: a process killed while writing cannot
            # leave an incomplete recovery journal at the live path.
            os.link(temporary, path)
            _sync_dir(path.parent)
        except OSError:
            raise DeletionError("无法持久化删除日志，未继续删除") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _delete_exact(self, document, before):
        ids = [str(value) for value in document.chunk_ids]
        for start in range(0, len(ids), 64):
            batch = ids[start:start + 64]
            records = self.client.retrieve(collection_name=before.spec.collection_name, ids=batch,
                                           with_payload=True, with_vectors=False)
            seen = set()
            for record in records:
                payload = record.payload
                if str(record.id) not in batch or str(record.id) in seen or not isinstance(payload, dict):
                    raise DeletionError("Qdrant片段身份异常")
                if set(payload) != {"text", "metadata", "dense_index_version"} or payload["dense_index_version"] != before.spec.index_version:
                    raise DeletionError("Qdrant片段版本异常")
                chunk = DocumentChunk.model_validate({"text": payload["text"], "metadata": payload["metadata"]})
                meta = chunk.metadata
                expected = uuid5(meta.document_id, f"{meta.index_version}:{meta.page}:{meta.chunk_index}:{sha256(chunk.text.encode('utf-8')).hexdigest()}")
                if (str(meta.chunk_id) != str(record.id) or meta.chunk_id != expected or
                    (meta.document_id, meta.content_hash, meta.source_name, meta.index_version) !=
                    (document.document_id, document.content_hash, document.source_name, before.spec.splitter_version)):
                    raise DeletionError("Qdrant片段不属于待删除文档")
                seen.add(str(record.id))
            # A previous interrupted attempt may already have removed some or all IDs.
            result = self.client.delete(collection_name=before.spec.collection_name,
                                        points_selector=models.PointIdsList(points=batch), wait=True)
            if result.status != models.UpdateStatus.COMPLETED:
                raise DeletionError("Qdrant未确认删除完成")
            remaining = self.client.retrieve(collection_name=before.spec.collection_name, ids=batch,
                                             with_payload=False, with_vectors=False)
            if remaining:
                raise DeletionError("Qdrant删除回读未通过")

    def delete(self, document_id: UUID):
        if not isinstance(document_id, UUID):
            raise DeletionError("文档标识无效")
        with index_lock(self.manifest.path):
            try:
                path = self.manifest.deletion_journal_path
                if os.path.lexists(path):
                    journal, document = self._read_journal()
                    if journal.document_id != document_id:
                        raise DeletionError("请先恢复正在删除的文档")
                else:
                    before = self.manifest.load()
                    document = next((d for d in before.documents if d.document_id == document_id), None)
                    if document is None:
                        raise DocumentNotFound("文档不存在")
                    # Validate the complete existing collection before any destructive action.
                    read_completed_chunks(self.client, before)
                    journal = Journal(document_id=document_id, before=before)
                    self._write_journal(journal)
                before = journal.before
                after = before.model_copy(update={"documents": tuple(d for d in before.documents if d.document_id != document_id)})
                current = self.manifest.load(_pending_delete_ok=True)
                if current not in (before, after):
                    raise DeletionError("删除期间完成清单发生意外变化")
                self._delete_exact(document, before)
                if current != after:
                    self.manifest._publish(after)
                # Only this recovery path may read the new manifest while the journal exists.
                SnapshotManager(self.manifest).get_locked(self.client, _pending_delete_ok=True)
                path.unlink()
                _sync_dir(path.parent)
                return document
            except (DocumentNotFound, DeletionError):
                raise
            except Exception:
                raise DeletionError("删除未完成；数据访问已暂停，可重试同一文档恢复") from None
