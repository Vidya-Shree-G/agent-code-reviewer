"""Load the archived AIDev-pop tables and turn them into cleaned PR candidates."""

from __future__ import annotations

import collections
import random
import re
from pathlib import Path
from typing import Any, Iterable

from .clean import clean_issue_body, clean_pr_body, is_documentation_title, normalize, word_count

ARCHIVE = "https://zenodo.org/records/16919051"
TABLES = (
    "pull_request",
    "repository",
    "related_issue",
    "issue",
    "pr_reviews",
    "pr_review_comments",
)
_REPO_NAME = re.compile(r"[\w.-]+/[\w.-]+", re.ASCII)
_PR_API_URL = re.compile(r"/repos/([\w.-]+/[\w.-]+)/pulls/(\d+)$", re.ASCII)
_BOT = re.compile(r"(\[bot\]$|-bot$|^bot-|^(?:copilot|devin-ai-integration|ellipsis-dev|coderabbitai|"
                  r"github-actions|codecov|sourcery-ai|gemini-code-assist|cursor|chatgpt-codex-connector)"
                  r"(?:\[bot\])?$)", re.I)


def read_tables(raw_dir: Path) -> dict[str, list[dict[str, Any]]]:
    import pyarrow.parquet as pq

    return {name: pq.read_table(Path(raw_dir) / f"{name}.parquet").to_pylist() for name in TABLES}


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _none(value: Any) -> Any:
    return None if value in (None, "", "None", "nan") else value


def is_bot(login: str | None) -> bool:
    return bool(login and _BOT.search(login))


def build_candidates(
    tables: dict[str, list[dict[str, Any]]],
    *,
    language: str = "python",
    min_stars: int = 100,
    min_requirement_words: int = 10,
    seed: int = 572,
    pinned_pr_ids: Iterable[int] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply metadata filters and return candidates ordered for screening.

    One PR is chosen per repository (pinned PRs first, then repositories in a
    seeded random order), so the order is reproducible and the list can be
    screened until a target size is reached.
    """
    repos = {_int(r["id"]): r for r in tables["repository"]}
    issues = {_int(r["id"]): r for r in tables["issue"]}
    links: dict[int, set[int]] = collections.defaultdict(set)
    for r in tables["related_issue"]:
        if _int(r["issue_id"]) in issues:
            links[_int(r["pr_id"])].add(_int(r["issue_id"]))

    funnel = collections.Counter()
    by_repo: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    seen: set[tuple[str, int]] = set()
    for pr in sorted(tables["pull_request"], key=lambda x: _int(x["id"]) or 0):
        funnel["input_prs"] += 1
        repo = repos.get(_int(pr["repo_id"]))
        if not repo:
            funnel["excluded_missing_repository"] += 1
            continue
        if (repo.get("language") or "").lower() != language or (repo.get("stars") or 0) <= min_stars:
            funnel["excluded_language_or_stars"] += 1
            continue
        name, number = repo.get("full_name") or "", _int(pr.get("number"))
        if not _REPO_NAME.fullmatch(name) or not number:
            funnel["excluded_missing_repository_name_or_pr_number"] += 1
            continue
        if (name.lower(), number) in seen:
            funnel["excluded_duplicate"] += 1
            continue
        title = normalize(pr.get("title"))
        if is_documentation_title(title):
            funnel["excluded_documentation_title"] += 1
            continue
        body = clean_pr_body(pr.get("body"))
        linked = []
        for iid in sorted(links[_int(pr["id"])]):
            issue = issues[iid]
            linked.append({
                "number": _int(issue.get("number")),
                "title": normalize(issue.get("title")),
                "body": clean_issue_body(issue.get("body")),
                "state": issue.get("state"),
                "html_url": issue.get("html_url"),
            })
        requirement_words = (word_count(title) + word_count(body.description)
                             + sum(word_count(i["title"]) + word_count(i["body"]) for i in linked))
        if requirement_words < min_requirement_words:
            funnel["excluded_insufficient_requirement_text"] += 1
            continue
        seen.add((name.lower(), number))
        merged_at = _none(pr.get("merged_at"))
        parts = [f"# {title}", body.description]
        for i in linked:
            parts.append(f"## Linked issue #{i['number']}: {i['title']}\n\n{i['body']}")
        by_repo[name.lower()].append({
            "pr_id": _int(pr["id"]),
            "repository": name,
            "repository_stars": repo.get("stars"),
            "repository_license": _none(repo.get("license")),
            "pr_number": number,
            "agent": pr.get("agent"),
            "pr_state": "merged" if merged_at else pr.get("state"),
            "created_at": _none(pr.get("created_at")),
            "closed_at": _none(pr.get("closed_at")),
            "merged_at": merged_at,
            "title": title,
            "pr_description": body.description,
            "linked_issues": linked,
            "requirement_text": normalize("\n\n".join(p for p in parts if p)),
            "requirement_words": requirement_words,
            "agent_reported_testing": body.agent_reported_testing,
            "agent_reported_test_failure": body.agent_reported_test_failure,
            "cleaning_rules_applied": body.removed,
            "pr_url": f"https://github.com/{name}/pull/{number}",
            "patch_url": f"https://github.com/{name}/pull/{number}.patch",
        })
        funnel["eligible_prs"] += 1
    funnel["eligible_repositories"] = len(by_repo)

    pinned = set(int(p) for p in pinned_pr_ids)
    ordered: list[dict[str, Any]] = []
    pinned_repos = set()
    for key in sorted(by_repo):
        for row in by_repo[key]:
            if row["pr_id"] in pinned:
                ordered.append(row)
                pinned_repos.add(key)
    missing = pinned - {r["pr_id"] for r in ordered}
    if missing:
        funnel["pinned_prs_not_eligible"] = len(missing)
    rng = random.Random(seed)
    keys = sorted(k for k in by_repo if k not in pinned_repos)
    rng.shuffle(keys)
    ordered.extend(rng.choice(by_repo[k]) for k in keys)
    return ordered, dict(funnel)


def review_evidence(tables: dict[str, list[dict[str, Any]]], rows: list[dict[str, Any]]) -> list[dict]:
    """Historical human reviews and inline comments for the selected PRs.

    This is held-out evaluation evidence: it must not be given to the review
    agent as input.
    """
    wanted = {r["pr_id"]: r for r in rows}
    by_key = {(r["repository"].lower(), r["pr_number"]): r["pr_id"] for r in rows}
    out = {pid: {"pr_id": pid, "repository": r["repository"], "pr_number": r["pr_number"],
                 "reviews": [], "inline_comments": []} for pid, r in wanted.items()}
    for rv in tables["pr_reviews"]:
        pid = _int(rv["pr_id"])
        if pid in out:
            out[pid]["reviews"].append({
                "user": rv.get("user"), "is_bot": is_bot(rv.get("user")), "state": rv.get("state"),
                "submitted_at": _none(rv.get("submitted_at")), "body": normalize(rv.get("body")),
            })
    for c in tables["pr_review_comments"]:
        m = _PR_API_URL.search(c.get("pull_request_url") or "")
        pid = by_key.get((m.group(1).lower(), int(m.group(2)))) if m else None
        if pid is None:
            continue
        out[pid]["inline_comments"].append({
            "user": c.get("user"), "is_bot": is_bot(c.get("user")), "path": c.get("path"),
            "diff_hunk": c.get("diff_hunk"), "position": _int(c.get("position")),
            "original_position": _int(c.get("original_position")), "commit_id": c.get("commit_id"),
            "in_reply_to_id": _int(c.get("in_reply_to_id")), "created_at": _none(c.get("created_at")),
            "body": normalize(c.get("body")),
        })
    for ev in out.values():
        ev["reviews"].sort(key=lambda r: r["submitted_at"] or "")
        ev["inline_comments"].sort(key=lambda r: r["created_at"] or "")
        ev["n_human_reviews"] = sum(not r["is_bot"] for r in ev["reviews"])
        ev["n_human_inline_comments"] = sum(not c["is_bot"] for c in ev["inline_comments"])
    return [out[r["pr_id"]] for r in rows]
