"""D8离线划分与冻结；默认只检查，正式冻结必须先完成全量人工复核。"""

import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from evals.validate_dataset import CATEGORIES, DatasetValidationError, validate_dataset


SPLIT_FILE = "split_manifest.json"
FREEZE_FILE = "frozen_manifest.json"
TEXT_POLICY = "d3-utf8-lf-markdown-preserved-unicode-codepoints-half-open-v1"
SPLIT_FIELDS = {"schema_version", "split_id", "dev", "holdout"}
FREEZE_FIELDS = {"schema_version", "split_id", "text_policy", "frozen_at", "files", "fingerprint"}
DATA_FILES = ("sources.jsonl", "questions.jsonl", "labels.draft.jsonl", SPLIT_FILE)


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise DatasetValidationError(message)


def _root(dataset_dir: Path) -> Path:
    try:
        root = Path(dataset_dir).resolve()
        _check(root.is_dir(), "数据集目录不存在")
        return root
    except (OSError, ValueError, RuntimeError):
        raise DatasetValidationError("数据集目录无法解析") from None


def _input_file(root: Path, relative: str) -> Path:
    """只读取本工具确定的输入路径，不按冻结文件提供的路径读取任意文件。"""
    path = root / relative
    try:
        _check(not path.is_symlink() and path.resolve().is_relative_to(root)
               and path.is_file(), "数据输入必须是数据集内的普通文件，不能使用符号链接")
    except (OSError, ValueError, RuntimeError):
        raise DatasetValidationError("数据输入文件无法解析") from None
    return path


def _bytes(root: Path, relative: str) -> bytes:
    try:
        return _input_file(root, relative).read_bytes()
    except OSError:
        raise DatasetValidationError("无法读取数据输入文件") from None


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        _check(key not in result, "JSON对象含重复字段")
        result[key] = value
    return result


def _json(root: Path, filename: str) -> dict:
    try:
        value = json.loads(_bytes(root, filename).decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise DatasetValidationError("清单不是有效UTF-8 JSON对象或含重复字段") from None
    _check(isinstance(value, dict), "清单必须为JSON对象")
    return value


def _rows(root: Path, filename: str) -> list[dict]:
    # 字段、重复key与JSONL语法由validate_dataset统一检查后才走到这里。
    return [json.loads(line) for line in _bytes(root, filename).decode("utf-8").splitlines()]


def validate_split(dataset_dir: Path) -> dict:
    """检查3:1划分及同族约束；pending可以预检，不能据此生成正式冻结。"""
    root = _root(dataset_dir)
    for filename in DATA_FILES:
        _input_file(root, filename)
    summary = validate_dataset(root)
    sources = _rows(root, "sources.jsonl")
    for source in sources:
        raw = _bytes(root, source["path"])
        _check(b"\r" not in raw, "冻结语料必须采用LF换行，改变换行需重新核对版本与位置")
    questions = _rows(root, "questions.jsonl")
    by_id = {q["question_id"]: q for q in questions}
    total = len(questions)
    _check(30 <= total <= 50, "D8数据集须包含30至50题")
    _check(all(summary["category_counts"][category] > 0 for category in CATEGORIES), "数据集必须覆盖八类题型")

    split = _json(root, SPLIT_FILE)
    _check(set(split) == SPLIT_FIELDS and type(split["schema_version"]) is int
           and split["schema_version"] == 1, "划分清单字段或版本无效")
    _check(isinstance(split["split_id"], str) and bool(split["split_id"].strip()), "划分版本必须为非空字符串")
    owners, families, category_counts = {}, {}, {}
    for side in ("dev", "holdout"):
        ids = split[side]
        _check(isinstance(ids, list) and bool(ids)
               and all(isinstance(qid, str) and qid in by_id for qid in ids), "划分包含未知问题或不是非空ID列表")
        categories = Counter()
        for qid in ids:
            _check(qid not in owners, "问题在划分中重复或跨集重叠")
            owners[qid] = side
            question = by_id[qid]
            family = question["family_id"]
            _check(family not in families or families[family] == side, "同一问题族不能跨开发集与保留集")
            families[family] = side
            categories[question["category"]] += 1
        _check(0 < categories["unanswerable"] < len(ids), "每侧均须包含可回答题和无答案题")
        category_counts[side] = {category: categories[category] for category in CATEGORIES}
    _check(set(owners) == set(by_id), "划分必须完整覆盖全部问题")
    _check(len(split["holdout"]) == round(total / 4), "划分须约3:1；40题应为30开发/10保留")
    return {**summary, "split_id": split["split_id"], "dev": len(split["dev"]),
            "holdout": len(split["holdout"]), "families": len(families),
            "split_category_counts": category_counts,
            "ready_for_freeze": summary["ready_for_baseline"]}


def _snapshot(root: Path) -> dict[str, str]:
    paths = [*DATA_FILES, *(s["path"] for s in _rows(root, "sources.jsonl"))]
    return {path: sha256(_bytes(root, path)).hexdigest() for path in sorted(paths)}


def _fingerprint(split_id: str, files: dict[str, str]) -> str:
    # 时间不参与指纹：相同输入可在临时目录独立复算出同一版本身份。
    value = {"schema_version": 1, "split_id": split_id, "text_policy": TEXT_POLICY, "files": files}
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "dataset-v1-" + sha256(canonical.encode("utf-8")).hexdigest()


def freeze_dataset(dataset_dir: Path) -> dict:
    """生成一次性冻结清单；不修改标签、不自动确认，也不覆盖已有冻结。"""
    root = _root(dataset_dir)
    target = root / FREEZE_FILE
    _check(not target.exists() and not target.is_symlink(), "冻结清单已存在，拒绝覆盖；请验证原版本")
    summary = validate_split(root)
    _check(summary["ready_for_freeze"], "标签尚未全部人工确认，不能正式冻结")
    files = _snapshot(root)
    fingerprint = _fingerprint(summary["split_id"], files)
    manifest = {"schema_version": 1, "split_id": summary["split_id"], "text_policy": TEXT_POLICY,
                "frozen_at": datetime.now(timezone.utc).isoformat(), "files": files,
                "fingerprint": fingerprint}
    payload = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    # 单进程本地数据维护；独占创建防止误覆盖，失败时仅清理本次新建的文件。
    created = False
    try:
        with target.open("xb") as stream:
            created = True
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise DatasetValidationError("冻结清单已存在，拒绝覆盖") from None
    except OSError:
        if created:
            target.unlink(missing_ok=True)
        raise DatasetValidationError("冻结清单写入失败") from None
    return {"status": "ok", "state": "frozen", "fingerprint": fingerprint,
            "dev": summary["dev"], "holdout": summary["holdout"]}


def verify_freeze(dataset_dir: Path) -> dict:
    """复算所有登记输入；更改问题、证据、审核元数据或划分均使原冻结失效。"""
    root = _root(dataset_dir)
    manifest = _json(root, FREEZE_FILE)
    _check(set(manifest) == FREEZE_FIELDS and type(manifest["schema_version"]) is int
           and manifest["schema_version"] == 1, "冻结清单字段或版本无效")
    _check(manifest["text_policy"] == TEXT_POLICY, "冻结文本定位规则不匹配")
    try:
        timestamp = manifest["frozen_at"]
        _check(isinstance(timestamp, str) and "T" in timestamp, "冻结时间必须为含时区ISO8601")
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        _check(parsed.utcoffset() is not None, "冻结时间必须含时区")
    except ValueError:
        raise DatasetValidationError("冻结时间无效") from None
    summary = validate_split(root)
    _check(summary["ready_for_freeze"], "冻结数据的人工复核状态已失效")
    files = _snapshot(root)
    _check(manifest["files"] == files and manifest["split_id"] == summary["split_id"],
           "冻结文件指纹或划分版本不匹配；不得沿用旧实验身份")
    fingerprint = manifest["fingerprint"]
    _check(isinstance(fingerprint, str) and bool(re.fullmatch(r"dataset-v1-[0-9a-f]{64}", fingerprint))
           and fingerprint == _fingerprint(summary["split_id"], files), "冻结总指纹不匹配")
    return {"status": "ok", "state": "verified", "fingerprint": fingerprint,
            "dev": summary["dev"], "holdout": summary["holdout"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--freeze", action="store_true", help="全量人工确认后创建冻结清单，拒绝覆盖")
    action.add_argument("--verify", action="store_true", help="重新校验已有冻结，不修改数据")
    args = parser.parse_args()
    try:
        operation = freeze_dataset if args.freeze else verify_freeze if args.verify else validate_split
        result = operation(args.dataset)
    except DatasetValidationError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
