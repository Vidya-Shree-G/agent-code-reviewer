"""Run defect detection over the dataset, one result file per PR."""

from __future__ import annotations

import argparse
import json
import shutil
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..dataset.pipeline import read_jsonl
from .detector import DetectorConfig, detect
from .report import summarize


def run_dataset(args: argparse.Namespace) -> None:
    prs = read_jsonl(args.data / "prs.jsonl")
    if args.split != "all":
        prs = [p for p in prs if p["split"] == args.split]
    if args.pr:
        prs = [p for p in prs if p["pr_id"] in set(args.pr)]
    out_dir = args.out / "prs"
    out_dir.mkdir(parents=True, exist_ok=True)
    work = args.work.resolve()
    cache = work / "_shared_cache"
    cache.mkdir(parents=True, exist_ok=True)
    cfg = DetectorConfig(run_tests=not args.no_tests, sandbox=not args.no_sandbox,
                         install_timeout=args.install_timeout, test_timeout=args.test_timeout,
                         shared_cache=cache)

    def one(pr: dict) -> str:
        diff_path = args.cache / "diffs" / f"{pr['pr_id']}.diff"
        target = out_dir / f"{pr['pr_id']}.json"
        pr_work = work / str(pr["pr_id"])
        try:
            if not diff_path.is_file():
                res = {"pr_id": pr["pr_id"], "repository": pr["repository"], "status": "diff_missing",
                       "findings": []}
            else:
                res = detect(pr, diff_path.read_text(encoding="utf-8", errors="replace"), pr_work, cfg)
        except Exception:  # noqa: BLE001
            res = {"pr_id": pr["pr_id"], "repository": pr["repository"], "status": "crashed",
                   "error": traceback.format_exc()[-1500:], "findings": []}
        finally:
            if not args.keep_work:
                shutil.rmtree(pr_work, ignore_errors=True)
        target.write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        t = res.get("tests", {})
        return (f"{pr['repository']}#{pr['pr_number']}: {res['status']}, {len(res['findings'])} findings"
                + (f", tests={t.get('status')}" if t else ""))

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futs = [pool.submit(one, pr) for pr in prs]
        for i, fut in enumerate(as_completed(futs), 1):
            print(f"[{i}/{len(prs)}] {fut.result()}", flush=True)
    print(summarize(args.data, args.out))
