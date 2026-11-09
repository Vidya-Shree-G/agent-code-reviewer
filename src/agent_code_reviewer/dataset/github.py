"""GitHub enrichment: commits, changed files and the diff for each candidate PR.

Responses are cached on disk so a rebuild does not depend on GitHub returning
the same answer twice. Raw diffs stay in the cache directory; they are not
committed to the repository.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..defects.diff import is_python_path, is_test_path

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, cache_dir: Path, token: str | None = None):
        self.cache = Path(cache_dir)
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    def _request(self, url: str, accept: str) -> bytes:
        headers = {"Accept": accept, "User-Agent": "agent-code-reviewer-dataset",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(4):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
                    wait = max(1, int(e.headers.get("X-RateLimit-Reset", time.time() + 60)) - int(time.time()))
                    time.sleep(min(wait + 1, 900))
                    continue
                if e.code >= 500 and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise GitHubError(f"{e.code} for {url}") from e
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError) as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise GitHubError(f"{getattr(e, 'reason', e)} for {url}") from e
        raise GitHubError(f"giving up on {url}")

    def _cached(self, key: str, url: str, accept: str) -> bytes:
        path = self.cache / key
        if path.is_file():
            return path.read_bytes()
        data = self._request(url, accept)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data

    def pull(self, repo: str, number: int, pr_id: int) -> dict[str, Any]:
        return json.loads(self._cached(f"api/{pr_id}.pull.json", f"{API}/repos/{repo}/pulls/{number}",
                                       "application/vnd.github+json"))

    def files(self, repo: str, number: int, pr_id: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, 31):  # GitHub caps this endpoint at 3000 files
            batch = json.loads(self._cached(
                f"api/{pr_id}.files.{page}.json",
                f"{API}/repos/{repo}/pulls/{number}/files?per_page=100&page={page}",
                "application/vnd.github+json"))
            out.extend(batch)
            if len(batch) < 100:
                break
        return out

    def diff_path(self, pr_id: int) -> Path:
        return self.cache / "diffs" / f"{pr_id}.diff"

    def diff(self, repo: str, number: int, pr_id: int) -> str:
        data = self._cached(f"diffs/{pr_id}.diff", f"{API}/repos/{repo}/pulls/{number}",
                            "application/vnd.github.v3.diff")
        return data.decode("utf-8", errors="replace")


def enrich(row: dict[str, Any], gh: GitHub, *, max_changed_lines: int, max_files: int
           ) -> tuple[dict[str, Any] | None, str | None]:
    """Return (enriched row, None) or (None, exclusion reason)."""
    repo, number, pid = row["repository"], row["pr_number"], row["pr_id"]
    try:
        pull = gh.pull(repo, number, pid)
        files = gh.files(repo, number, pid)
    except GitHubError:
        return None, "pr_unavailable"
    changed = [{"path": f["filename"], "status": f["status"], "additions": f["additions"],
                "deletions": f["deletions"], "previous_path": f.get("previous_filename")} for f in files]
    py = [f["path"] for f in changed if is_python_path(f["path"]) and f["status"] != "removed"]
    if not py:
        return None, "no_python_changes"
    additions, deletions = pull.get("additions", 0), pull.get("deletions", 0)
    if additions + deletions > max_changed_lines or len(changed) > max_files:
        return None, "too_large"
    try:
        diff = gh.diff(repo, number, pid)
    except GitHubError:
        return None, "diff_unavailable"
    if not diff.strip():
        return None, "diff_unavailable"
    out = dict(row)
    out.update({
        "base_ref": pull["base"]["ref"],
        "base_sha": pull["base"]["sha"],
        "head_sha": pull["head"]["sha"],
        "merge_commit_sha": pull.get("merge_commit_sha"),
        "n_commits": pull.get("commits"),
        "additions": additions,
        "deletions": deletions,
        "changed_lines": additions + deletions,
        "n_changed_files": len(changed),
        "changed_files": changed,
        "changed_python_files": py,
        "changed_test_files": [p for p in py if is_test_path(p)],
        "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
    })
    return out, None
