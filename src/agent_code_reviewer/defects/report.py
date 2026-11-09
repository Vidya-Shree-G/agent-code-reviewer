"""Aggregate per-PR defect detection results into findings.jsonl, summary.json and SUMMARY.md."""

from __future__ import annotations

import collections
import json
import re
from pathlib import Path
from typing import Any

from ..dataset.pipeline import read_jsonl, write_jsonl

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def comment_line(diff_hunk: str | None) -> int | None:
    """New-file line a GitHub review comment points at (the last line of its diff_hunk)."""
    if not diff_hunk:
        return None
    lines = diff_hunk.splitlines()
    m = _HUNK.match(lines[0]) if lines else None
    if not m:
        return None
    ln = int(m.group(1)) - 1
    for line in lines[1:]:
        if not line.startswith("-"):
            ln += 1
    return ln


def _human_comment_locations(evidence: list[dict]) -> dict[int, list[tuple[str, int | None]]]:
    out: dict[int, list[tuple[str, int | None]]] = collections.defaultdict(list)
    for ev in evidence:
        for c in ev["inline_comments"]:
            if not c["is_bot"] and c["path"]:
                out[ev["pr_id"]].append((c["path"], comment_line(c["diff_hunk"])))
    return out


def summarize(data_dir: Path, results_dir: Path) -> str:
    data_dir, results_dir = Path(data_dir), Path(results_dir)
    prs = {p["pr_id"]: p for p in read_jsonl(data_dir / "prs.jsonl")}
    ev_path = data_dir / "review_evidence.jsonl"
    comments = _human_comment_locations(read_jsonl(ev_path)) if ev_path.is_file() else {}
    results = [json.loads(p.read_text()) for p in sorted((results_dir / "prs").glob("*.json"))]
    all_findings: list[dict[str, Any]] = []
    rows = []
    for r in results:
        pr = prs.get(r["pr_id"], {})
        locs = comments.get(r["pr_id"], [])
        for f in r["findings"]:
            near = [ln for path, ln in locs if path == f["file"]]
            f2 = {"pr_id": r["pr_id"], "repository": r["repository"], "pr_number": r.get("pr_number"),
                  "split": pr.get("split"), "agent": pr.get("agent"), **f,
                  "human_comment_same_file": bool(near),
                  "human_comment_within_3_lines": any(ln is not None and f["line"] is not None
                                                      and abs(ln - f["line"]) <= 3 for ln in near)}
            all_findings.append(f2)
        t = r.get("tests") or {}
        env = (t.get("env") or {})
        rows.append({
            "pr_id": r["pr_id"], "repository": r["repository"], "pr_number": r.get("pr_number"),
            "split": pr.get("split"), "agent": pr.get("agent"), "status": r["status"],
            "base_mode": r.get("base_mode"),
            "static_introduced": (r.get("static") or {}).get("introduced"),
            "findings": len(r["findings"]),
            "high": sum(f["severity"] == "high" for f in r["findings"]),
            "env_status": env.get("status"), "test_status": t.get("status"),
            "selected_tests": len(t.get("selected_tests") or []),
            "head_counts": (t.get("head_run") or {}).get("counts"),
            "base_counts": (t.get("base_run") or {}).get("counts"),
            "regressions": len((t.get("comparison") or {}).get("regressions", [])),
            "new_test_failures": len((t.get("comparison") or {}).get("new_test_failures", [])),
            "fixed": len((t.get("comparison") or {}).get("fixed", [])),
            "flaky": len((t.get("comparison") or {}).get("flaky_regressions", [])),
            "modules_import_checked": len(t.get("imports_head") or {}),
            "seconds": r.get("seconds"),
        })
    write_jsonl(results_dir / "findings.jsonl", all_findings)
    write_jsonl(results_dir / "pr_results.jsonl", rows)
    summary = _summary(rows, all_findings, prs, results)
    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    md = _markdown(summary, rows, all_findings)
    (results_dir / "SUMMARY.md").write_text(md, encoding="utf-8")
    return md


def _summary(rows, findings, prs, results) -> dict[str, Any]:
    C = collections.Counter
    ok = [r for r in rows if r["status"] == "ok"]
    tested = [r for r in ok if r["test_status"]]
    return {
        "prs_in_dataset": len(prs),
        "prs_processed": len(rows),
        "status": dict(C(r["status"] for r in rows)),
        "base_mode": dict(C(r["base_mode"] for r in ok)),
        "findings_total": len(findings),
        "prs_with_any_finding": sum(r["findings"] > 0 for r in ok),
        "prs_with_high_severity_finding": sum(r["high"] > 0 for r in ok),
        "by_severity": dict(C(f["severity"] for f in findings)),
        "by_category": dict(C(f["category"] for f in findings).most_common()),
        "by_tool": dict(C(f["tool"] for f in findings).most_common()),
        "by_rule": dict(C(f"{f['tool']}:{f['rule']}" for f in findings).most_common(25)),
        "cwe": dict(C(f"CWE-{f['cwe']}" for f in findings if f.get("cwe")).most_common()),
        "on_changed_line": sum(f["on_changed_line"] for f in findings),
        "by_agent_findings_per_pr": {
            a: round(sum(r["findings"] for r in ok if r["agent"] == a)
                     / max(1, sum(r["agent"] == a for r in ok)), 2)
            for a in sorted({r["agent"] for r in ok if r["agent"]})},
        "tests": {
            "env_status": dict(C(r["env_status"] for r in tested)),
            "test_status": dict(C(r["test_status"] for r in tested)),
            "prs_with_selected_tests": sum(r["selected_tests"] > 0 for r in tested),
            "prs_with_completed_head_run": sum(r["test_status"] == "completed" for r in tested),
            "prs_with_regression": sum(r["regressions"] > 0 for r in tested),
            "prs_with_new_test_failure": sum(r["new_test_failures"] > 0 for r in tested),
            "prs_that_fix_a_failing_test": sum(r["fixed"] > 0 for r in tested),
            "flaky_regressions_discarded": sum(r["flaky"] for r in tested),
            "modules_import_checked": sum(r["modules_import_checked"] for r in tested),
        },
        "human_review_overlap": {
            "findings_in_file_with_human_comment": sum(f["human_comment_same_file"] for f in findings),
            "findings_within_3_lines_of_human_comment": sum(f["human_comment_within_3_lines"] for f in findings),
        },
    }


def _table(d: dict, head: tuple[str, str]) -> list[str]:
    return [f"| {head[0]} | {head[1]} |", "|---|---|"] + [f"| {k} | {v} |" for k, v in d.items()]


def _markdown(s: dict, rows: list[dict], findings: list[dict]) -> str:
    t = s["tests"]
    out = [
        "# Defect detection results",
        "",
        "Generated by `agent-code-reviewer defects summarize`. Do not edit by hand.",
        "Findings are suspected defects the PR *introduced* (absent at the base); each has tool or test evidence.",
        "They are not yet validated against reference labels.",
        "",
        "## Overview",
        "",
        "| Item | Value |",
        "|---|---|",
        f"| PRs processed | {s['prs_processed']} of {s['prs_in_dataset']} |",
        f"| Status | {', '.join(f'{k}: {v}' for k, v in s['status'].items())} |",
        f"| Base reconstruction | {', '.join(f'{k}: {v}' for k, v in s['base_mode'].items())} |",
        f"| Findings | {s['findings_total']} ({s['on_changed_line']} on lines the PR added) |",
        f"| PRs with ≥1 finding | {s['prs_with_any_finding']} |",
        f"| PRs with ≥1 high-severity finding | {s['prs_with_high_severity_finding']} |",
        f"| Findings in a file a human reviewer commented on | "
        f"{s['human_review_overlap']['findings_in_file_with_human_comment']} |",
        f"| Findings within 3 lines of a human comment | "
        f"{s['human_review_overlap']['findings_within_3_lines_of_human_comment']} |",
        "",
        "## Findings by severity, category and tool",
        "",
        *_table(s["by_severity"], ("Severity", "Findings")),
        "",
        *_table(s["by_category"], ("Category", "Findings")),
        "",
        *_table(s["by_tool"], ("Tool", "Findings")),
        "",
        "## Most frequent rules",
        "",
        *_table(s["by_rule"], ("Tool:rule", "Findings")),
        "",
    ]
    if s["cwe"]:
        out += ["## Security findings by CWE", "", *_table(s["cwe"], ("CWE", "Findings")), ""]
    out += ["## Findings per PR by agent", "", *_table(s["by_agent_findings_per_pr"], ("Agent", "Mean findings/PR")),
            ""]
    if t["env_status"]:
        out += [
            "## Executable testing", "",
            "| Item | PRs |", "|---|---|",
            f"| Environment ok / partial / failed | {t['env_status'].get('ok', 0)} / "
            f"{t['env_status'].get('partial', 0)} / {t['env_status'].get('failed', 0)} |",
            f"| Related tests found | {t['prs_with_selected_tests']} |",
            f"| Head test run completed | {t['prs_with_completed_head_run']} |",
            f"| ≥1 confirmed regression (pass at base → fail at head) | {t['prs_with_regression']} |",
            f"| ≥1 failing test added/changed by the PR | {t['prs_with_new_test_failure']} |",
            f"| PR makes a failing test pass | {t['prs_that_fix_a_failing_test']} |",
            f"| Flaky regressions discarded on rerun | {t['flaky_regressions_discarded']} |",
            f"| Changed modules import-checked | {t['modules_import_checked']} |",
            "", "Test status per PR: " + ", ".join(f"{k}: {v}" for k, v in t["test_status"].items()), "",
        ]
    out += ["## Per-PR results", "",
            "| PR | Split | Agent | Findings (high) | Env | Tests | Head pass/fail | Regr. | New fail |",
            "|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r["split"] or "", r["repository"].lower())):
        hc = r["head_counts"] or {}
        hp = f"{hc.get('passed', 0)}/{hc.get('failed', 0) + hc.get('error', 0)}" if hc else "–"
        out.append(f"| [{r['repository']}#{r['pr_number']}](https://github.com/{r['repository']}/pull/"
                   f"{r['pr_number']}) | {r['split']} | {r['agent']} | {r['findings']} ({r['high']}) | "
                   f"{r['env_status'] or '–'} | {r['test_status'] or r['status']} | {hp} | "
                   f"{r['regressions']} | {r['new_test_failures']} |")
    top = sorted((f for f in findings if f["severity"] == "high"), key=lambda f: -f["score"])[:25]
    if top:
        out += ["", "## High-severity findings", "", "| PR | Location | Tool:rule | Message |", "|---|---|---|---|"]
        for f in top:
            msg = f["message"].replace("|", "\\|").replace("\n", " ")[:140]
            out.append(f"| {f['repository']}#{f['pr_number']} | `{f['file']}:{f['line']}` | "
                       f"{f['tool']}:{f['rule']} | {msg} |")
    return "\n".join(out) + "\n"
