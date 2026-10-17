"""D7离线标注检查；仅核对结构与原文定位，不判定语义或自动确认人工标签。"""

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from uuid import UUID

from project_copilot.rag.loaders import DocumentLoadError, load_document

CATEGORIES = (
    "knowledge", "terminology", "exact_match", "paraphrase", "chinese_long",
    "cross_passage", "unanswerable", "follow_up",
)
SOURCE_FIELDS = {
    "source_id", "source_type", "title", "path", "document_id", "document_version",
    "language", "provenance", "prepared_by",
}
QUESTION_FIELDS = {
    "question_id", "category", "family_id", "question", "standalone_question",
    "history", "source_ids", "prepared_by",
}
LABEL_FIELDS = {
    "question_id", "answerable", "reference_answer", "evidence", "unanswerable_reason",
    "review_status", "reviewed_by", "reviewed_at",
}
EVIDENCE_FIELDS = {
    "evidence_id", "source_id", "document_id", "document_version", "quote", "page",
    "start_char", "end_char", "section",
}


class DatasetValidationError(ValueError):
    """安全简明的本地数据校验错误，不包含原文、问题或其他输入值。"""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise DatasetValidationError(message)


def _text(value: object, where: str) -> str:
    _check(isinstance(value, str) and bool(value.strip()), f"{where}必须为非空字符串")
    return value


def _object(value: object, fields: set[str], where: str) -> dict:
    _check(isinstance(value, dict) and set(value) == fields, f"{where}字段不完整或含未知字段")
    return value


def _list(value: object, where: str) -> list:
    _check(isinstance(value, list), f"{where}必须为列表")
    return value


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        _check(key not in result, "JSON对象含重复字段")
        result[key] = value
    return result


def _rows(root: Path, filename: str, fields: set[str]) -> list[dict]:
    try:
        lines = (root / filename).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise DatasetValidationError(f"无法读取UTF-8文件{filename}") from None
    _check(bool(lines), f"{filename}不能为空")
    rows = []
    for number, line in enumerate(lines, 1):
        where = f"{filename}第{number}行"
        try:
            row = json.loads(line, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            raise DatasetValidationError(f"{where}不是有效JSON对象或含重复字段") from None
        rows.append(_object(row, fields, where))
    return rows


def _identity(record: dict, where: str) -> None:
    try:
        UUID(_text(record["document_id"], where + ".document_id"))
    except ValueError:
        raise DatasetValidationError(f"{where}.document_id必须为UUID") from None
    version = _text(record["document_version"], where + ".document_version")
    _check(bool(re.fullmatch(r"[0-9a-f]{64}", version)), f"{where}.document_version必须为SHA-256")


def _review(label: dict, where: str) -> None:
    status = label["review_status"]
    _check(status in ("pending", "confirmed", "needs_changes"), f"{where}.review_status无效")
    reviewer, reviewed_at = label["reviewed_by"], label["reviewed_at"]
    if status == "pending":
        _check(reviewer is None and reviewed_at is None, f"{where}待复核标签不能声称已人工复核")
        return
    if status == "needs_changes" and reviewer is None and reviewed_at is None:
        return
    name = _text(reviewer, where + ".reviewed_by").casefold()
    _check(not re.search(r"codex|chatgpt|assistant|copilot|claude|gemini|\bgpt\b|\bai\b|\bllm\b|\bbot\b", name),
           f"{where}复核者必须是人工身份")
    try:
        timestamp = _text(reviewed_at, where + ".reviewed_at")
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        _check("T" in timestamp and parsed.utcoffset() is not None, f"{where}复核时间必须含时区")
    except ValueError:
        raise DatasetValidationError(f"{where}复核时间必须为含时区的ISO8601时间") from None


def validate_dataset(dataset_dir: Path, *, require_reviewed: bool = False) -> dict:
    """检查字节版本、原文字符切片与关联；ready标志仍依赖人工复核真实性。"""
    try:
        root = Path(dataset_dir).resolve()
    except (OSError, ValueError, RuntimeError):
        raise DatasetValidationError("数据集根目录无效或无法解析") from None
    sources, documents = {}, {}
    source_paths, document_ids = set(), set()
    for number, source in enumerate(_rows(root, "sources.jsonl", SOURCE_FIELDS), 1):
        where = f"来源第{number}条"
        source_id = _text(source["source_id"], where + ".source_id")
        _check(source_id not in sources, "来源ID重复")
        _check(source["source_type"] in ("project_experience", "official_documentation_note"), f"{where}来源类型无效")
        _text(source["title"], where + ".title")
        _check(source["language"] == "zh-CN" and source["prepared_by"] == "codex", f"{where}语言或准备者无效")
        _identity(source, where)
        relative = _text(source["path"], where + ".path")
        path = PurePosixPath(relative)
        _check(not path.is_absolute() and len(path.parts) == 2 and path.parts[0] == "corpus"
               and path.suffix == ".md" and "\\" not in relative and "\x00" not in relative,
               f"{where}路径必须为corpus下的Markdown文件")
        try:
            resolved = (root / relative).resolve()
        except (OSError, ValueError, RuntimeError):
            raise DatasetValidationError(f"{where}路径无效或无法解析") from None
        _check(resolved.is_relative_to(root) and resolved.is_file(), f"{where}文件不存在或越过数据集根目录")
        _check(resolved not in source_paths and source["document_id"] not in document_ids,
               f"{where}实际路径或文档ID重复")
        provenance = _list(source["provenance"], where + ".provenance")
        _check(bool(provenance), f"{where}必须保留至少一条来源记录")
        for item in provenance:
            _object(item, {"uri", "revision", "retrieved_on", "note"}, where + ".provenance")
            for field in ("uri", "revision", "note"):
                _text(item[field], where + ".provenance." + field)
            try:
                date.fromisoformat(item["retrieved_on"])
                _check(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["retrieved_on"])), f"{where}来源日期无效")
            except (ValueError, TypeError):
                raise DatasetValidationError(f"{where}来源日期必须为YYYY-MM-DD") from None
        try:
            document = load_document(resolved)
        except (DocumentLoadError, OSError):
            raise DatasetValidationError(f"{where}原文无法读取") from None
        _check(str(document.document_id) == source["document_id"]
               and document.content_hash == source["document_version"], f"{where}文档ID或字节版本不匹配")
        sources[source_id], documents[source_id] = source, document
        source_paths.add(resolved)
        document_ids.add(source["document_id"])

    questions, category_counts = {}, dict.fromkeys(CATEGORIES, 0)
    for number, question in enumerate(_rows(root, "questions.jsonl", QUESTION_FIELDS), 1):
        where = f"问题第{number}条"
        question_id = _text(question["question_id"], where + ".question_id")
        _check(question_id not in questions, "问题ID重复")
        category = question["category"]
        _check(isinstance(category, str) and category in CATEGORIES, f"{where}类别无效")
        for field in ("family_id", "question"):
            _text(question[field], where + "." + field)
        _check(question["prepared_by"] == "codex", f"{where}准备者无效")
        source_ids = _list(question["source_ids"], where + ".source_ids")
        _check(all(isinstance(item, str) and item in sources for item in source_ids), f"{where}来源不存在")
        _check(len(source_ids) == len(set(source_ids)), f"{where}来源ID重复")
        history = _list(question["history"], where + ".history")
        for message in history:
            _object(message, {"role", "content"}, where + ".history")
            _check(message["role"] in ("user", "assistant"), f"{where}历史角色无效")
            _text(message["content"], where + ".history.content")
        if category == "follow_up":
            _check(bool(history), f"{where}追问必须保存历史")
            _text(question["standalone_question"], where + ".standalone_question")
        else:
            _check(not history and question["standalone_question"] is None, f"{where}非追问不能有历史或独立改写")
        questions[question_id] = question
        category_counts[category] += 1

    seen, evidence_ids = set(), set()
    counts = dict.fromkeys(("answerable", "unanswerable", "confirmed", "pending", "needs_changes"), 0)
    for number, label in enumerate(_rows(root, "labels.draft.jsonl", LABEL_FIELDS), 1):
        where = f"标签第{number}条"
        question_id = _text(label["question_id"], where + ".question_id")
        _check(question_id in questions and question_id not in seen, f"{where}问题不存在或标签重复")
        seen.add(question_id)
        _check(type(label["answerable"]) is bool, f"{where}.answerable必须为布尔值")
        _check(label["answerable"] == (questions[question_id]["category"] != "unanswerable"),
               f"{where}题型与可回答标记不一致")
        evidence = _list(label["evidence"], where + ".evidence")
        if label["answerable"]:
            _check(bool(evidence) and label["unanswerable_reason"] is None, f"{where}可回答标签必须有证据且无拒答原因")
            _text(label["reference_answer"], where + ".reference_answer")
        else:
            _check(not evidence and label["reference_answer"] is None, f"{where}不可回答标签不能含证据或参考答案")
            _text(label["unanswerable_reason"], where + ".unanswerable_reason")
        positions = set()
        for item in evidence:
            _object(item, EVIDENCE_FIELDS, where + ".evidence")
            evidence_id = _text(item["evidence_id"], where + ".evidence_id")
            _check(evidence_id not in evidence_ids, "证据ID重复")
            evidence_ids.add(evidence_id)
            source_id = _text(item["source_id"], where + ".evidence.source_id")
            _check(source_id in questions[question_id]["source_ids"], f"{where}证据来源不在问题来源范围")
            _identity(item, where + ".evidence")
            source, document = sources[source_id], documents[source_id]
            _check(all(item[key] == source[key] for key in ("document_id", "document_version")), f"{where}证据版本不匹配")
            _check(item["page"] is None or (type(item["page"]) is int and item["page"] >= 0), f"{where}证据页码无效")
            pages = {page.page: page.text for page in document.pages}
            _check(item["page"] in pages, f"{where}证据页不存在")
            start, end = item["start_char"], item["end_char"]
            _check(type(start) is int and type(end) is int and 0 <= start < end <= len(pages[item["page"]]), f"{where}字符范围无效")
            position = (source_id, item["page"], start, end)
            _check(position not in positions, f"{where}不能将同一原文位置重复计为多个证据单元")
            positions.add(position)
            quote = _text(item["quote"], where + ".evidence.quote")
            _check(pages[item["page"]][start:end] == quote, f"{where}证据与原文字符切片不匹配")
            _text(item["section"], where + ".evidence.section")
        _review(label, where)
        counts["answerable" if label["answerable"] else "unanswerable"] += 1
        counts[label["review_status"]] += 1
    _check(seen == set(questions), "每个问题必须恰有一条标签")
    ready = bool(questions) and counts["confirmed"] == len(questions)
    _check(not require_reviewed or ready, "标签尚未全部人工确认，不能作为已复核基线输入")
    return {"status": "ok", "sources": len(sources), "questions": len(questions),
            **counts, "category_counts": category_counts, "ready_for_baseline": ready}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--require-reviewed", action="store_true")
    args = parser.parse_args()
    try:
        result = validate_dataset(args.dataset, require_reviewed=args.require_reviewed)
    except DatasetValidationError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
