"""End-to-end build of the cleaned AIDev-pop Python dataset."""

from __future__ import annotations

import collections
import hashlib
import json
from pathlib import Path
from typing import Any

from .aidev import ARCHIVE, build_candidates, read_tables, review_evidence
from .github import GitHub, enrich

DATASET_VERSION = "v1"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    Path(path).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _repo_hash(repo: str, seed: int) -> str:
    return hashlib.sha256(f"{seed}:{repo.lower()}".encode()).hexdigest()


def assign_splits(rows: list[dict[str, Any]], pinned_splits: dict[int, str], *, eval_fraction: float,
                  seed: int) -> None:
    """Repository-grouped split: every PR from a repository lands in the same split.

    Pinned (pilot) assignments are kept; remaining evaluation slots are filled
    with the repositories that sort first by a seeded hash.
    """
    repos: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for r in rows:
        repos[r["repository"].lower()].append(r)
    fixed = {}
    for key, rs in repos.items():
        pins = {pinned_splits[r["pr_id"]] for r in rs if r["pr_id"] in pinned_splits}
        if len(pins) > 1:
            raise ValueError(f"pinned PRs from {key} are in different splits")
        if pins:
            fixed[key] = pins.pop()
    n_eval = round(len(repos) * eval_fraction)
    remaining = n_eval - sum(v == "evaluation" for v in fixed.values())
    free = sorted((k for k in repos if k not in fixed), key=lambda k: _repo_hash(k, seed))
    eval_repos = {k for k, v in fixed.items() if v == "evaluation"} | set(free[:max(0, remaining)])
    for key, rs in repos.items():
        for r in rs:
            r["split"] = "evaluation" if key in eval_repos else "development"


def build_dataset(raw: Path, out: Path, cache: Path, *, target: int, pilot: Path | None,
                  min_stars: int = 100, min_requirement_words: int = 10, max_changed_lines: int = 1000,
                  max_files: int = 30, eval_fraction: float = 0.2, seed: int = 572) -> dict[str, Any]:
    tables = read_tables(raw)
    pilot_rows = read_jsonl(pilot) if pilot and Path(pilot).is_file() else []
    pinned_splits = {int(r["pr_id"]): r["split"] for r in pilot_rows}
    candidates, funnel = build_candidates(
        tables, min_stars=min_stars, min_requirement_words=min_requirement_words, seed=seed,
        pinned_pr_ids=pinned_splits)
    gh = GitHub(cache)
    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    screen = collections.Counter()
    for row in candidates:
        if len(selected) >= target:
            break
        enriched, reason = enrich(row, gh, max_changed_lines=max_changed_lines, max_files=max_files)
        screen["screened"] += 1
        if reason:
            screen[f"excluded_{reason}"] += 1
            excluded.append({"pr_id": row["pr_id"], "repository": row["repository"],
                             "pr_number": row["pr_number"], "reason": reason})
            continue
        enriched["in_pilot"] = enriched["pr_id"] in pinned_splits
        selected.append(enriched)
        print(f"[{len(selected)}/{target}] {row['repository']}#{row['pr_number']}", flush=True)
    screen["selected"] = len(selected)
    assign_splits(selected, pinned_splits, eval_fraction=eval_fraction, seed=seed)

    out.mkdir(parents=True, exist_ok=True)
    write_jsonl(out / "prs.jsonl", selected)
    write_jsonl(out / "review_evidence.jsonl", review_evidence(tables, selected))
    write_jsonl(out / "exclusions.jsonl", excluded)
    info = {
        "dataset": f"aidev-pop-python-{DATASET_VERSION}",
        "archive": ARCHIVE,
        "parameters": {"language": "python", "min_stars_exclusive": min_stars,
                       "min_requirement_words": min_requirement_words,
                       "max_changed_lines": max_changed_lines, "max_changed_files": max_files,
                       "prs_per_repository": 1, "target_size": target, "eval_fraction": eval_fraction,
                       "seed": seed, "pinned_pilot_prs": len(pinned_splits)},
        "metadata_funnel": funnel,
        "patch_screening": dict(screen),
    }
    (out / "build_info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return info
