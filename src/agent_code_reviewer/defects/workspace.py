"""Check out the head and base versions of a PR.

The head is the PR's head commit. The base is derived by reverse-applying the
PR diff to the head, which reproduces the merge base (the code the PR was
written against) exactly, without fetching the base branch's history. If the
diff does not reverse-apply, the base branch commit recorded by GitHub is used
instead and ``base_mode`` says so.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .diff import parse_diff, text_only_diff

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1", "GIT_CONFIG_NOSYSTEM": "1"}


class WorkspaceError(RuntimeError):
    pass


@dataclass
class Workspace:
    root: Path
    head: Path
    base: Path
    base_mode: str  # reverse_applied_diff | base_branch_commit


def _git(args: list[str], cwd: Path, timeout: int = 900) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GH_", "GITHUB_"))}
    env.update(GIT_ENV)
    return subprocess.run(["git", "-c", "advice.detachedHead=false", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=timeout)


def _fetch_checkout(repo: str, sha: str, dest: Path, fallback_ref: str | None = None) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], dest)
    _git(["remote", "add", "origin", f"https://github.com/{repo}.git"], dest)
    r = _git(["fetch", "-q", "--depth=1", "origin", sha], dest)
    if r.returncode != 0 and fallback_ref:
        r = _git(["fetch", "-q", "--depth=1", "origin", fallback_ref], dest)
    if r.returncode != 0:
        raise WorkspaceError(f"fetch {repo}@{sha[:10]} failed: {r.stderr.strip()[-300:]}")
    r = _git(["checkout", "-q", "FETCH_HEAD"], dest)
    if r.returncode != 0:
        raise WorkspaceError(f"checkout failed: {r.stderr.strip()[-300:]}")


def prepare_workspace(pr: dict, diff_text: str, root: Path) -> Workspace:
    root = Path(root)
    if root.exists():
        shutil.rmtree(root)
    head, base = root / "head", root / "base"
    _fetch_checkout(pr["repository"], pr["head_sha"], head, f"pull/{pr['pr_number']}/head")
    shutil.copytree(head, base, symlinks=True)
    patch = root / "pr.diff"
    patch.write_text(text_only_diff(parse_diff(diff_text)), encoding="utf-8")
    r = _git(["apply", "-R", "--whitespace=nowarn", str(patch)], base)
    if r.returncode == 0:
        return Workspace(root, head, base, "reverse_applied_diff")
    shutil.rmtree(base)
    _fetch_checkout(pr["repository"], pr["base_sha"], base)
    return Workspace(root, head, base, "base_branch_commit")
