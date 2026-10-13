"""参考原Text/PyPDFium2 loader，自主提供单文件、无模型调用的读取接口。"""

from contextlib import closing
from hashlib import sha256
from pathlib import Path
from threading import Lock
from uuid import NAMESPACE_URL, uuid5

from project_copilot.schemas.documents import DocumentPage, LoadedDocument


class DocumentLoadError(ValueError):
    """类型、编码或解析失败；不把文件内容写入错误消息。"""


# PDFium不允许不同线程同时调用；未来API的并行请求也需遵守这一约束。
_PDF_LOCK = Lock()


def _load_pdf_pages(data: bytes) -> tuple[DocumentPage, ...]:
    import pypdfium2 as pdfium

    try:
        with _PDF_LOCK, closing(pdfium.PdfDocument(data)) as pdf:
            pages = []
            for page_index in range(len(pdf)):
                with closing(pdf[page_index]) as page:
                    with closing(page.get_textpage()) as text_page:
                        text = text_page.get_text_bounded()
                # 统一PDF解析器的换行；页码不因空白页而重新编号。
                text = text.replace("\r\n", "\n").replace("\r", "\n")
                pages.append(DocumentPage(text=text, page=page_index))
            return tuple(pages)
    except pdfium.PdfiumError:
        raise DocumentLoadError("无法解析PDF；请确认文件完整且没有需要密码的加密。") from None


def load_document(path: str | Path) -> LoadedDocument:
    """读取受控本地TXT/MD/PDF；对同一次读取的字节做身份哈希和解析。"""
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix not in {".txt", ".md", ".pdf"}:
        raise DocumentLoadError("仅支持TXT、Markdown（.md）和PDF文件。")
    if not source.is_file():
        raise DocumentLoadError("文档路径必须指向存在的本地普通文件。")
    try:
        data = source.read_bytes()
    except OSError:
        raise DocumentLoadError("无法读取文档文件，请检查路径与权限。") from None

    content_hash = sha256(data).hexdigest()
    document_id = uuid5(NAMESPACE_URL, f"project-copilot:document:sha256:{content_hash}")
    if suffix == ".pdf":
        pages = _load_pdf_pages(data)
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise DocumentLoadError("TXT/Markdown必须使用UTF-8编码。") from None
        pages = (DocumentPage(text=text),)

    return LoadedDocument(
        document_id=document_id,
        content_hash=content_hash,
        source_name=source.name,
        pages=pages,
    )
