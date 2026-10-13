"""D3 离线验收：实际小文件、原文、PDF 页码、边界与稳定片段身份。"""

import hashlib
from pathlib import Path
from uuid import UUID

import pytest

from project_copilot.rag.loaders import DocumentLoadError, load_document
from project_copilot.rag.splitters import SplitConfig, split_document


FIXTURES = Path(__file__).parent / "fixtures" / "documents"


def _write_pdf(path: Path, pages: list[str]) -> None:
    """以 PDF 基础语法生成合成样本；只使用 ASCII 与内置 Helvetica。"""
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # Pages 字典在得到所有页面对象编号后填充。
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    page_numbers: list[int] = []
    for text in pages:
        page_number = len(objects) + 1
        content_number = page_number + 1
        page_numbers.append(page_number)
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_number} 0 R >>"
            ).encode("ascii")
        )
        stream = b""
        if text:
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET\n".encode("ascii")
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream"
        )
    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    objects[1] = f"<< /Type /Pages /Count {len(pages)} /Kids [{kids}] >>".encode("ascii")

    result = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n")
    xref_offset = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    result.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    path.write_bytes(result)


@pytest.mark.parametrize("name", ["sample.txt", "sample.md"])
def test_utf8_text_files_preserve_original_text_and_source(name: str) -> None:
    path = FIXTURES / name
    document = load_document(path)

    assert isinstance(document.document_id, UUID)
    assert document.content_hash == hashlib.sha256(path.read_bytes()).hexdigest()
    assert document.source_name == name
    assert document.has_text
    assert isinstance(document.pages, tuple)
    assert len(document.pages) == 1
    assert document.pages[0].page is None
    assert document.pages[0].text == path.read_text(encoding="utf-8")

    chunks = split_document(document)
    assert chunks
    assert all(0 < len(chunk.text) <= 750 for chunk in chunks)
    for index, chunk in enumerate(chunks):
        assert chunk.text in document.pages[0].text
        assert chunk.metadata.document_id == document.document_id
        assert chunk.metadata.content_hash == document.content_hash
        assert chunk.metadata.source_name == name
        assert chunk.metadata.page is None
        assert chunk.metadata.chunk_index == index
        assert isinstance(chunk.metadata.chunk_id, UUID)


def test_pdf_keeps_real_page_indices_including_blank_middle_page() -> None:
    path = FIXTURES / "sample.pdf"
    document = load_document(path)

    assert document.source_name == "sample.pdf"
    assert document.content_hash == hashlib.sha256(path.read_bytes()).hexdigest()
    assert document.has_text
    assert [page.page for page in document.pages] == [0, 1, 2]
    assert document.pages[0].text.strip() == "Project Copilot page one: local document loading."
    assert not document.pages[1].text.strip()
    assert document.pages[2].text.strip() == "Project Copilot page three: preserve the original page number."

    chunks = split_document(document, SplitConfig(chunk_size=32, chunk_overlap=8))
    assert {chunk.metadata.page for chunk in chunks} == {0, 2}
    assert [chunk.metadata.chunk_index for chunk in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert chunk.metadata.source_name == "sample.pdf"
        assert chunk.text in document.pages[chunk.metadata.page].text
        assert 0 < len(chunk.text) <= 32


def test_character_fallback_preserves_long_chinese_and_overlap(tmp_path: Path) -> None:
    # 互不相同的连续中文字符让丢字、错序和重叠偏差无法被重复语句掩盖。
    text = "".join(chr(0x4E00 + index) for index in range(1853))
    path = tmp_path / "long.txt"
    path.write_text(text, encoding="utf-8")
    chunks = split_document(load_document(path))

    assert len(chunks) > 1
    assert all(0 < len(chunk.text) <= 750 for chunk in chunks)
    for previous, current in zip(chunks, chunks[1:]):
        assert current.text.startswith(previous.text[-150:])
    reconstructed = chunks[0].text + "".join(chunk.text[150:] for chunk in chunks[1:])
    assert reconstructed == text


def test_renaming_identical_bytes_preserves_document_and_chunk_identity(tmp_path: Path) -> None:
    original = tmp_path / "original.md"
    renamed = tmp_path / "renamed.md"
    contents = ("# 合成资料\n\n这是用于核对稳定身份的中文原文。" * 90).encode("utf-8")
    original.write_bytes(contents)
    renamed.write_bytes(contents)

    first = load_document(original)
    again = load_document(str(original))
    moved = load_document(renamed)
    assert first.document_id == again.document_id == moved.document_id
    assert first.content_hash == moved.content_hash
    assert first.source_name != moved.source_name
    first_chunks = split_document(first)
    assert [chunk.metadata.chunk_id for chunk in first_chunks] == [
        chunk.metadata.chunk_id for chunk in split_document(again)
    ]
    assert [chunk.metadata.chunk_id for chunk in first_chunks] == [
        chunk.metadata.chunk_id for chunk in split_document(moved)
    ]


def test_same_filename_with_different_bytes_has_different_identity(tmp_path: Path) -> None:
    first_directory = tmp_path / "first"
    second_directory = tmp_path / "second"
    first_directory.mkdir()
    second_directory.mkdir()
    first_path = first_directory / "same.txt"
    second_path = second_directory / "same.txt"
    first_path.write_text("第一份原文。", encoding="utf-8")
    second_path.write_text("第二份原文。", encoding="utf-8")
    first = load_document(first_path)
    second = load_document(second_path)

    assert first.source_name == second.source_name
    assert first.content_hash != second.content_hash
    assert first.document_id != second.document_id
    assert split_document(first)[0].metadata.chunk_id != split_document(second)[0].metadata.chunk_id


def test_repeated_chunk_text_has_unique_position_identity(tmp_path: Path) -> None:
    path = tmp_path / "repeated.txt"
    path.write_text("知" * 2100, encoding="utf-8")
    chunks = split_document(load_document(path))

    assert len({chunk.text for chunk in chunks}) < len(chunks)
    assert len({chunk.metadata.chunk_id for chunk in chunks}) == len(chunks)
    assert [chunk.metadata.chunk_index for chunk in chunks] == list(range(len(chunks)))


def test_changed_split_config_changes_version_and_chunk_ids(tmp_path: Path) -> None:
    path = tmp_path / "short.txt"
    path.write_text("短文本在两种配置下都只产生一个片段。", encoding="utf-8")
    document = load_document(path)
    baseline = split_document(document)
    changed = split_document(document, SplitConfig(chunk_size=700, chunk_overlap=100))

    assert len(baseline) == len(changed) == 1
    assert baseline[0].text == changed[0].text
    assert baseline[0].metadata.document_id == changed[0].metadata.document_id
    assert baseline[0].metadata.index_version != changed[0].metadata.index_version
    assert baseline[0].metadata.chunk_id != changed[0].metadata.chunk_id


@pytest.mark.parametrize("suffix, contents", [(".txt", ""), (".md", " \t\n\n ")])
def test_empty_or_whitespace_text_has_explicit_empty_result(
    tmp_path: Path, suffix: str, contents: str
) -> None:
    path = tmp_path / f"empty{suffix}"
    path.write_text(contents, encoding="utf-8")
    document = load_document(path)

    assert not document.has_text
    assert document.pages[0].text == contents
    assert split_document(document) == []


def test_blank_pdf_has_explicit_empty_result(tmp_path: Path) -> None:
    path = tmp_path / "blank.pdf"
    _write_pdf(path, [""])
    document = load_document(path)

    assert [page.page for page in document.pages] == [0]
    assert not document.has_text
    assert split_document(document) == []


def test_missing_file_is_a_document_load_error(tmp_path: Path) -> None:
    with pytest.raises(DocumentLoadError):
        load_document(tmp_path / "missing.txt")


def test_directory_is_not_a_document(tmp_path: Path) -> None:
    directory = tmp_path / "directory.txt"
    directory.mkdir()
    with pytest.raises(DocumentLoadError):
        load_document(directory)


@pytest.mark.parametrize("name, contents", [("unsupported.csv", b"column\nvalue"), ("invalid.txt", b"\xff\xfe"), ("broken.pdf", b"not a PDF")])
def test_unsupported_encoding_and_broken_documents_fail_clearly(
    tmp_path: Path, name: str, contents: bytes
) -> None:
    path = tmp_path / name
    path.write_bytes(contents)
    with pytest.raises(DocumentLoadError):
        load_document(path)


@pytest.mark.parametrize("path", [None, 123, True, ["file.txt"]])
def test_invalid_path_type_is_rejected(path: object) -> None:
    with pytest.raises((DocumentLoadError, TypeError, ValueError)):
        load_document(path)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "values",
    [
        {"chunk_size": 0},
        {"chunk_size": -1},
        {"chunk_overlap": -1},
        {"chunk_size": 100, "chunk_overlap": 100},
        {"chunk_size": 100, "chunk_overlap": 101},
        {"separators": ("\n\n", "\n", " ")},
        {"separators": ()},
    ],
)
def test_invalid_split_config_is_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SplitConfig(**values)
