"""Diff-aware static analysis with ruff, bandit and the Python compiler.

Each changed Python file is analysed at the PR head and at the base. A
finding is reported only if the PR introduced it: findings are fingerprinted by
(file, tool, rule, source line text) and the base multiset is subtracted from
the head multiset, so pre-existing problems and pure line shifts do not count.
Findings on lines the PR added are marked ``on_changed_line``.

The analysers only parse code; nothing from the repository is executed here.
"""

from __future__ import annotations

import collections
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .diff import FileDiff, is_python_path, is_test_path
from .models import RUFF_SELECT, Finding, bandit_meta, ruff_rule_meta

# Bandit checks skipped everywhere / in test files (noise rather than defects).
BANDIT_SKIP = {"B101"}           # assert used: only matters under python -O
BANDIT_SKIP_IN_TESTS = {"B105", "B106", "B108", "B311", "B404", "B603", "B607"}


@dataclass
class RawIssue:
    tool: str
    rule: str
    message: str
    line: int | None
    end_line: int | None
    cwe: int | None = None
    severity: str | None = None
    confidence: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _tool(name: str) -> list[str]:
    return [sys.executable, "-m", name]


def tool_versions() -> dict[str, str]:
    out = {}
    for name in ("ruff", "bandit"):
        try:
            r = subprocess.run(_tool(name) + ["--version"], capture_output=True, text=True, timeout=60)
            out[name] = (r.stdout or r.stderr).strip().splitlines()[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            out[name] = "unavailable"
    out["python"] = sys.version.split()[0]
    return out


def run_ruff(root: Path, files: list[str]) -> dict[str, list[RawIssue]]:
    if not files:
        return {}
    cmd = _tool("ruff") + ["check", "--isolated", "--no-cache", "--exit-zero", "--output-format", "json",
                          "--select", ",".join(RUFF_SELECT), "--target-version", "py39", *files]
    r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=600)
    out: dict[str, list[RawIssue]] = collections.defaultdict(list)
    for item in json.loads(r.stdout or "[]"):
        rel = str(Path(item["filename"]).resolve().relative_to(root.resolve()))
        out[rel].append(RawIssue(
            tool="ruff", rule=item.get("code") or "syntax-error", message=item["message"],
            line=item["location"]["row"], end_line=item["end_location"]["row"],
            extra={"url": item.get("url")}))
    return out


def run_bandit(root: Path, files: list[str]) -> dict[str, list[RawIssue]]:
    if not files:
        return {}
    cmd = _tool("bandit") + ["-q", "-f", "json", "--skip", ",".join(sorted(BANDIT_SKIP)), *files]
    r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=600)
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    out: dict[str, list[RawIssue]] = collections.defaultdict(list)
    for item in data.get("results", []):
        rel = str(Path(root, item["filename"]).resolve().relative_to(root.resolve()))
        if is_test_path(rel) and item["test_id"] in BANDIT_SKIP_IN_TESTS:
            continue
        lines = item.get("line_range") or [item["line_number"]]
        out[rel].append(RawIssue(
            tool="bandit", rule=item["test_id"], message=f"{item['test_name']}: {item['issue_text']}",
            line=item["line_number"], end_line=max(lines), cwe=(item.get("issue_cwe") or {}).get("id"),
            severity=item["issue_severity"], confidence=item["issue_confidence"],
            extra={"more_info": item.get("more_info")}))
    return out


def run_compile(root: Path, files: list[str]) -> dict[str, list[RawIssue]]:
    """Compile each file with this interpreter (catches syntax errors ruff's parser may accept)."""
    out: dict[str, list[RawIssue]] = collections.defaultdict(list)
    for rel in files:
        try:
            src = (root / rel).read_bytes()
            compile(src, rel, "exec", dont_inherit=True)
        except SyntaxError as e:
            out[rel].append(RawIssue(tool="py_compile", rule=type(e).__name__,
                                     message=f"{type(e).__name__}: {e.msg}", line=e.lineno,
                                     end_line=e.end_lineno or e.lineno))
        except (OSError, ValueError):
            continue
    return out


def _source_line(root: Path, rel: str, line: int | None, cache: dict) -> str:
    if rel not in cache:
        try:
            cache[rel] = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            cache[rel] = []
    lines = cache[rel]
    return lines[line - 1].strip() if line and 0 < line <= len(lines) else ""


def _norm(src: str) -> str:
    return "".join(src.split())


def _merge(*results: dict[str, list[RawIssue]]) -> dict[str, list[RawIssue]]:
    out: dict[str, list[RawIssue]] = collections.defaultdict(list)
    for res in results:
        for k, v in res.items():
            out[k].extend(v)
    return out


def analyse(root: Path, files: list[str]) -> dict[str, list[RawIssue]]:
    files = [f for f in files if (root / f).is_file()]
    return _merge(run_ruff(root, files), run_bandit(root, files), run_compile(root, files))


def _to_finding(issue: RawIssue, rel: str, changed: bool, src: str) -> Finding:
    if issue.tool == "bandit":
        cat, sev, conf = bandit_meta(issue.severity or "LOW", issue.confidence or "LOW")
    elif issue.tool == "py_compile":
        cat, sev, conf = ("correctness", "high", "high")
    else:
        cat, sev, conf = ruff_rule_meta(None if issue.rule == "syntax-error" else issue.rule)
    if is_test_path(rel) and cat == "security":
        sev = "low"
    return Finding(tool=issue.tool, rule=issue.rule, category=cat, severity=sev, confidence=conf,
                   message=issue.message, file=rel, line=issue.line, end_line=issue.end_line,
                   cwe=issue.cwe, on_changed_line=changed,
                   evidence={"type": "static", "source_line": src, **issue.extra}, sources=[issue.tool])


@dataclass
class StaticResult:
    findings: list[Finding]
    files_analysed: int
    head_issue_count: int
    base_issue_count: int
    per_tool_head: dict[str, int]
    per_tool_base: dict[str, int]


def introduced_findings(head: Path, base: Path, file_diffs: list[FileDiff]) -> StaticResult:
    targets = [fd for fd in file_diffs
               if fd.new_path and is_python_path(fd.new_path) and not fd.binary]
    head_issues = analyse(head, [fd.new_path for fd in targets])
    base_paths = [fd.old_path for fd in targets if fd.old_path]
    base_issues = analyse(base, base_paths)
    hcache: dict = {}
    bcache: dict = {}
    findings: list[Finding] = []
    for fd in targets:
        rel = fd.new_path
        base_list = [(b, _norm(_source_line(base, fd.old_path, b.line, bcache)))
                     for b in base_issues.get(fd.old_path, [])] if fd.old_path else []
        by_line = collections.Counter((b.tool, b.rule, src) for b, src in base_list)
        by_msg = collections.Counter((b.tool, b.rule, b.message) for b, _ in base_list)
        # Consume base matches with off-diff head issues first, so matches are not
        # wasted on issues sitting on lines the PR actually added.
        hs = sorted(head_issues.get(rel, []), key=lambda i: (i.line in fd.added_lines, i.line or 0))
        unmatched = []
        for h in hs:  # stage 1: same rule on the same (whitespace-normalised) source line
            src = _source_line(head, rel, h.line, hcache)
            key = (h.tool, h.rule, _norm(src))
            if by_line[key] > 0:
                by_line[key] -= 1
                by_msg[(h.tool, h.rule, h.message)] -= 1
            else:
                unmatched.append((h, src))
        for h, src in unmatched:  # stage 2: same rule and message (survives reformatting/moves)
            key = (h.tool, h.rule, h.message)
            if by_msg[key] > 0:
                by_msg[key] -= 1
                continue
            changed = bool(h.line and any(ln in fd.added_lines
                                          for ln in range(h.line, (h.end_line or h.line) + 1)))
            findings.append(_to_finding(h, rel, changed, src))
    findings.sort(key=lambda f: (f.file or "", f.line or 0))
    return StaticResult(
        findings=dedupe(findings),
        files_analysed=len(targets),
        head_issue_count=sum(map(len, head_issues.values())),
        base_issue_count=sum(map(len, base_issues.values())),
        per_tool_head=dict(collections.Counter(i.tool for v in head_issues.values() for i in v)),
        per_tool_base=dict(collections.Counter(i.tool for v in base_issues.values() for i in v)),
    )


def dedupe(findings: list[Finding]) -> list[Finding]:
    """Merge reports of the same problem by different tools (e.g. a syntax error)."""
    out: list[Finding] = []
    by_loc: dict[tuple, Finding] = {}
    for f in findings:
        syntax = f.tool == "py_compile" or f.rule in ("syntax-error", "E999")
        key = (f.file, f.line, "syntax") if syntax else None
        if key and key in by_loc:
            by_loc[key].sources = sorted(set(by_loc[key].sources) | {f.tool})
            continue
        if key:
            by_loc[key] = f
        out.append(f)
    return out
