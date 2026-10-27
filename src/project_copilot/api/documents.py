"""D6薄上传入口：限制表单、校验文件、临时保存，再调用已有单文件入库服务。"""

import logging
import tempfile
import unicodedata
from pathlib import Path
from threading import Lock

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from project_copilot.config import Settings
from project_copilot.infrastructure.models import (
    EmbeddingConfigError, EmbeddingRequestError, EmbeddingResponseError,
)
from project_copilot.infrastructure.qdrant import (
    QdrantConfigError, QdrantRequestError, QdrantResponseError,
)
from project_copilot.rag.ingestion import (
    EmptyDocumentError, IngestionError, ManifestError, create_ingestion_service,
)
from project_copilot.rag.loaders import DocumentLoadError
from project_copilot.rag.bm25 import BM25Error
from project_copilot.rag.index_lock import IndexLockError
from project_copilot.rag.final_retrieval import FinalRetrievalError
from project_copilot.schemas.uploads import DocumentUploadResult


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"])
_COPY_CHUNK_BYTES = 64 * 1024
# D4完成清单只支持顺序入库；此锁仅保护同一进程内的HTTP入口，不是跨进程或CLI锁/事务。
_INGESTION_LOCK = Lock()
_ALLOWED_TYPES = {
    ".txt": {"text/plain", "application/octet-stream"},
    ".md": {"text/plain", "text/markdown", "application/octet-stream"},
    ".pdf": {"application/pdf", "application/octet-stream"},
}


def _validate_file(upload: UploadFile) -> tuple[str, str]:
    filename = upload.filename
    if (
        not isinstance(filename, str)
        or not 1 <= len(filename) <= 200
        or "/" in filename
        or "\\" in filename
        or any(unicodedata.category(character).startswith("C") for character in filename)
    ):
        raise HTTPException(status_code=422, detail="文件名必须为1至200字符的普通名称，不能包含路径或控制字符")
    suffix = Path(filename).suffix.lower()
    if suffix not in _ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail="仅支持TXT、Markdown（.md）和PDF文件")
    media_type = (upload.content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    if media_type not in _ALLOWED_TYPES[suffix]:
        raise HTTPException(status_code=415, detail="文件Content-Type与扩展名不匹配")
    # 这里只做基本类型筛选；内容仍由D4加载器验证编码或PDF解析结果。
    return filename, suffix


def _ingest_upload(upload: UploadFile, settings: Settings, run_id: str) -> DocumentUploadResult:
    source_name, suffix = _validate_file(upload)
    temporary: Path | None = None
    failed = True
    try:
        # 物理路径只含服务端生成的随机名称，原始名称仅写入展示metadata。
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix="project-copilot-upload-", suffix=suffix, delete=False,
        ) as target:
            temporary = Path(target.name)
            size_bytes = 0
            while data := upload.file.read(_COPY_CHUNK_BYTES):
                size_bytes += len(data)
                if size_bytes > settings.upload_max_bytes:
                    raise HTTPException(status_code=413, detail="上传文件超过允许的大小")
                target.write(data)
        if size_bytes == 0:
            raise HTTPException(status_code=422, detail="上传文件不能为空")
        if not _INGESTION_LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="当前已有文档正在入库，请稍后重试")
        try:
            with create_ingestion_service(settings) as service:
                completed = service.ingest_file(temporary, source_name=source_name)
                result = DocumentUploadResult(
                    document=completed,
                    chunk_count=len(completed.chunk_ids),
                    size_bytes=size_bytes,
                    index_version=service.manifest.spec.index_version,
                    run_id=run_id,
                )
        finally:
            _INGESTION_LOCK.release()
        failed = False
        return result
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                if not failed:
                    raise
                # 清理异常不覆盖原本的解析、模型或存储错误；不记录临时路径。
                logger.warning("Upload cleanup failed: %s", type(exc).__name__)


@router.post(
    "/upload",
    response_model=DocumentUploadResult,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "additionalProperties": False,
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    },
                },
            },
        },
    },
)
async def upload_document(request: Request) -> DocumentUploadResult:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "multipart/form-data":
        raise HTTPException(status_code=415, detail="上传请求必须使用multipart/form-data")
    try:
        # 允许一个普通字段以便明确返回422；额外字段或重复文件都不能进入入库链路。
        async with request.form(max_files=1, max_fields=1, max_part_size=_COPY_CHUNK_BYTES) as form:
            items = list(form.multi_items())
            if len(items) > 1:
                raise HTTPException(status_code=400, detail="上传请求只能包含一个file文件字段")
            if not items or items[0][0] != "file" or not isinstance(items[0][1], UploadFile):
                raise HTTPException(status_code=422, detail="请提供一个名为file的上传文件")
            return await run_in_threadpool(
                _ingest_upload, items[0][1], request.app.state.settings, request.state.run_id,
            )
    except (DocumentLoadError, EmptyDocumentError) as exc:
        logger.warning("Upload document validation failed: %s", type(exc).__name__)
        raise HTTPException(status_code=422, detail="文档解析失败或没有可索引的非空文本") from None
    except (EmbeddingConfigError, QdrantConfigError, ManifestError, IngestionError,
            BM25Error, IndexLockError, FinalRetrievalError) as exc:
        logger.warning("Upload configuration failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="入库配置或索引清单不可用，请检查本项目配置") from None
    except (EmbeddingRequestError, QdrantRequestError) as exc:
        logger.warning("Upload upstream request failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="入库上游服务请求失败，请检查服务状态") from None
    except (EmbeddingResponseError, QdrantResponseError) as exc:
        logger.warning("Upload upstream validation failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="入库上游结果未通过校验，请检查模型或存储结果") from None
    except OSError as exc:
        logger.warning("Upload temporary I/O failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="上传文件临时处理失败，请稍后重试") from None
