"""Prepare candidate Python PRs from the archived AIDev-pop release."""
import argparse
import collections
import json
from pathlib import Path
import random
import re

ARCHIVE = "https://zenodo.org/records/16919051"

def read_tables(path):
    import pyarrow.parquet as pq
    return [pq.read_table(path / (name + ".parquet")).to_pylist()
            for name in ("pull_request", "repository", "related_issue", "issue")]

def prepare(prs, repos, links, issues, limit=10, seed=572):
    repo_by_id = {r["id"]: r for r in repos}
    issue_by_id = {r["id"]: r for r in issues}
    links_by_pr = collections.defaultdict(set)
    for r in links:
        if r["issue_id"] in issue_by_id:
            links_by_pr[r["pr_id"]].add(r["issue_id"])
    counts = collections.Counter()
    candidates = collections.defaultdict(list)
    seen = set()
    for pr in sorted(prs, key=lambda x: x["id"]):
        repo = repo_by_id.get(pr["repo_id"])
        if not repo:
            counts["missing_repository"] += 1
            continue
        if (repo.get("language") or "").lower() != "python" or (repo.get("stars") or 0) <= 100:
            counts["language_or_stars"] += 1
            continue
        name, number = repo.get("full_name") or "", pr.get("number")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", name, flags=re.ASCII) or not number:
            counts["missing_repository_name_or_pr_number"] += 1
            continue
        key = (name.lower(), number)
        if key in seen:
            counts["duplicate"] += 1
            continue
        title = (pr.get("title") or "").strip()
        body = (pr.get("body") or "").strip()
        related = [{k: issue_by_id[i].get(k) for k in ("title", "body", "html_url")}
                   for i in sorted(links_by_pr[pr["id"]])]
        if not body and not any((i.get("body") or "").strip() for i in related):
            counts["missing_description_and_issue_body"] += 1
            continue
        if re.match(r"^(docs?|documentation)(?:\(|:|\s)", title, re.I):
            counts["obvious_documentation_title"] += 1
            continue
        seen.add(key)
        candidates[key[0]].append({
            "pr_id": pr["id"], "repository": name, "repository_stars": repo["stars"],
            "pr_number": number, "agent": pr.get("agent"), "title": title,
            "pr_description": body, "linked_issues": related,
            "pr_url": f"https://github.com/{name}/pull/{number}",
            "files_url": f"https://github.com/{name}/pull/{number}/files",
            "patch_url": f"https://github.com/{name}/pull/{number}.patch",
            "patch_check": "pending", "requirements_check": "pending", "tests_check": "pending"
        })
    rng = random.Random(seed)
    keys = sorted(candidates)
    rng.shuffle(keys)
    selected = [rng.choice(candidates[k]).copy() for k in keys[:limit]]
    if not selected:
        raise ValueError("No candidate PRs matched the metadata filters")
    eval_count = max(1, round(len(selected) * .2)) if len(selected) > 1 else 0
    for idx, row in enumerate(selected):
        row["split"] = "evaluation" if idx < eval_count else "development"
    stats = {
        "archive": ARCHIVE, "seed": seed, "input_pr_rows": len(prs),
        "input_repository_rows": len(repos), "metadata_exclusions": dict(counts),
        "eligible_prs_after_metadata_filters": sum(map(len, candidates.values())),
        "eligible_repositories_after_metadata_filters": len(keys),
        "candidate_prs": len(selected), "candidate_repositories": len(selected),
        "development_candidates": len(selected) - eval_count,
        "evaluation_candidates": eval_count, "fully_validated_candidates": 0,
    }
    return selected, stats

def screen_patches(rows, patch_dir):
    import shlex
    screened = []
    for row in rows:
        path = patch_dir / f"{row['pr_id']}.patch"
        files = set()
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("diff --git "):
                    parts = shlex.split(line)
                    if len(parts) == 4 and parts[3].startswith("b/"):
                        files.add(parts[3][2:])
        py = sorted(p for p in files if p.lower().endswith(".py"))
        row["patch_check"] = ("contains_python_changes" if py else
                              "no_python_changes" if files else "unverified")
        row["changed_python_files"] = py
        row["changed_test_file_hints"] = [
            p for p in py if any(part in ("test", "tests") for part in p.split("/")[:-1])
            or Path(p).name.startswith("test_") or Path(p).name.endswith("_test.py")]
        if py:
            screened.append(row.copy())
    n_eval = max(1, round(len(screened) * .2)) if len(screened) > 1 else 0
    for idx, row in enumerate(screened):
        row["split"] = "evaluation" if idx < n_eval else "development"
    return screened

def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                    encoding="utf-8")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--patch-dir", type=Path, help="Optional directory of PR-ID.patch files")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    rows, stats = prepare(*read_tables(args.raw), args.limit)
    args.out.mkdir(parents=True, exist_ok=True)
    if args.patch_dir:
        screened = screen_patches(rows, args.patch_dir)
        stats.update({
            "patch_screen_counts": dict(collections.Counter(r["patch_check"] for r in rows)),
            "screened_prs": len(screened),
            "screened_development_prs": sum(r["split"] == "development" for r in screened),
            "screened_evaluation_prs": sum(r["split"] == "evaluation" for r in screened),
        })
        write_jsonl(args.out / "pilot_python_prs.jsonl", screened)
    write_jsonl(args.out / "pilot_candidates.jsonl", rows)
    (args.out / "pilot_stats.json").write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(stats, indent=2))

if __name__ == "__main__":
    main()
