"""Command-line interface for the project.

    agent-code-reviewer dataset build   --raw RAW --out data/clean --cache CACHE
    agent-code-reviewer dataset stats   --data data/clean
    agent-code-reviewer defects run     --data data/clean --cache CACHE --work WORK --out results/defects
    agent-code-reviewer defects summarize --data data/clean --results results/defects
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-code-reviewer", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="group")

    ds = sub.add_parser("dataset", help="build and describe the cleaned dataset")
    dsub = ds.add_subparsers(dest="cmd", required=True)
    b = dsub.add_parser("build", help="filter, clean, screen and split AIDev-pop PRs")
    b.add_argument("--raw", type=Path, required=True, help="folder with the AIDev Parquet tables")
    b.add_argument("--out", type=Path, default=Path("data/clean"))
    b.add_argument("--cache", type=Path, required=True, help="GitHub response/diff cache (kept local)")
    b.add_argument("--pilot", type=Path, default=Path("data/pilot/pilot_python_prs.jsonl"))
    b.add_argument("--target", type=int, default=100)
    b.add_argument("--min-stars", type=int, default=100)
    b.add_argument("--min-requirement-words", type=int, default=10)
    b.add_argument("--max-changed-lines", type=int, default=1000)
    b.add_argument("--max-files", type=int, default=30)
    b.add_argument("--seed", type=int, default=572)
    s = dsub.add_parser("stats", help="write stats.json and STATS.md")
    s.add_argument("--data", type=Path, default=Path("data/clean"))

    df = sub.add_parser("defects", help="defect detection and executable testing")
    fsub = df.add_subparsers(dest="cmd", required=True)
    r = fsub.add_parser("run", help="run static analysis and tests on dataset PRs")
    r.add_argument("--data", type=Path, default=Path("data/clean"))
    r.add_argument("--cache", type=Path, required=True, help="same cache used by dataset build")
    r.add_argument("--work", type=Path, required=True, help="scratch dir for checkouts and venvs")
    r.add_argument("--out", type=Path, default=Path("results/defects"))
    r.add_argument("--split", choices=["development", "evaluation", "all"], default="all")
    r.add_argument("--pr", type=int, action="append", help="only these PR ids (repeatable)")
    r.add_argument("--no-tests", action="store_true", help="static analysis only (baseline B1)")
    r.add_argument("--no-sandbox", action="store_true", help="run tests without bubblewrap")
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--install-timeout", type=int, default=900)
    r.add_argument("--test-timeout", type=int, default=600)
    r.add_argument("--keep-work", action="store_true", help="keep checkouts/venvs after each PR")
    m = fsub.add_parser("summarize", help="aggregate per-PR results into summary files")
    m.add_argument("--data", type=Path, default=Path("data/clean"))
    m.add_argument("--results", type=Path, default=Path("results/defects"))
    return p


def main(argv: list[str] | None = None) -> int:
    """Run the project entry point."""
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.group is None:
        print("Agent Code Reviewer is ready.")
        return 0
    if args.group == "dataset" and args.cmd == "build":
        from .dataset.pipeline import build_dataset
        from .dataset.stats import write_stats

        info = build_dataset(args.raw, args.out, args.cache, target=args.target, pilot=args.pilot,
                             min_stars=args.min_stars, min_requirement_words=args.min_requirement_words,
                             max_changed_lines=args.max_changed_lines, max_files=args.max_files,
                             seed=args.seed)
        write_stats(args.out)
        print(json.dumps(info, indent=2))
    elif args.group == "dataset" and args.cmd == "stats":
        from .dataset.stats import write_stats

        write_stats(args.data)
        print((args.data / "STATS.md").read_text())
    elif args.group == "defects" and args.cmd == "run":
        from .defects.runner import run_dataset

        run_dataset(args)
    elif args.group == "defects" and args.cmd == "summarize":
        from .defects.report import summarize

        print(summarize(args.data, args.results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
