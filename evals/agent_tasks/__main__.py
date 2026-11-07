"""Offline only: contract, score imported results, or compare two saved summaries."""

import argparse
import json
from pathlib import Path

from .dataset import DEFAULT_DATASET, load_dataset
from .scoring import compare, score


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_new(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Archived evidence is immutable: fail rather than overwrite a previous run.
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("contract", "score"):
        child = commands.add_parser(name)
        child.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
        child.add_argument("--split", choices=("dev", "holdout", "all"), default="dev")
        child.add_argument("--output", type=Path, required=True)
        if name == "score":
            child.add_argument("--records", type=Path, required=True)
            child.add_argument("--reviews", type=Path)
    child = commands.add_parser("compare")
    child.add_argument("--before", type=Path, required=True)
    child.add_argument("--after", type=Path, required=True)
    child.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "compare":
        write_new(args.output, compare(read(args.before), read(args.after)))
        return
    cases, dataset_hash = load_dataset(args.dataset)
    if args.command == "contract":
        from .runner import run_contract
        if args.output.exists():
            parser.error("output directory already exists; use a new run path")
        bundle = run_contract(cases, dataset_hash, args.split)
        summary = score(cases, dataset_hash, bundle, split=args.split)
        write_new(args.output / "records.json", bundle)
        write_new(args.output / "summary.json", summary)
    else:
        bundle = read(args.records)
        summary = score(cases, dataset_hash, bundle, split=args.split,
                        reviews=read(args.reviews) if args.reviews else None)
        write_new(args.output, summary)
    print(json.dumps({key: summary[key] for key in ("contract_success", "terminal_failures", "usage")},
                     ensure_ascii=False))
    if summary["contract_failure"]["numerator"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
