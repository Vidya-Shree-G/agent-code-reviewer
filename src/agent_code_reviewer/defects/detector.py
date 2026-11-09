"""Defect detection for one PR: static analysis + executable tests -> ranked findings.

This is the "defect analysis" step of the reviewer agent and, with tests
disabled, the static-analysis-only baseline (B1). It can also run extra
test files (for example targeted tests written by the LLM agent) at head and
base through ``generated_tests``.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .diff import parse_diff
from .models import Finding
from .sandbox import Sandbox
from .static_analysis import StaticResult, introduced_findings, tool_versions
from .test_runner import (FAIL, TestResult, compare_runs, import_check, import_findings,
                          missing_modules, module_name, pypi_names, run_pytest, select_tests,
                          setup_environment, test_findings, uv_binary)
from .workspace import Workspace, prepare_workspace


@dataclass
class DetectorConfig:
    run_tests: bool = True
    sandbox: bool = True
    install_timeout: int = 900
    test_timeout: int = 600
    shared_cache: Path | None = None  # uv package cache shared across PRs


def rank(findings: list[Finding]) -> list[Finding]:
    """Drop exact duplicates, then sort by severity x confidence (changed lines first on ties)."""
    seen, out = set(), []
    for f in findings:
        key = (f.tool, f.rule, f.file, f.line, f.message)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return sorted(out, key=lambda f: (-f.score, f.file or "", f.line or 0))


def dependency_cutoff(pr: dict[str, Any]) -> str | None:
    """Resolve dependencies as of when the PR was last worked on (closed, else created).

    Installing today's releases breaks older code (removed APIs), which would show up as
    failures at both versions and hide real regressions.
    """
    return pr.get("closed_at") or pr.get("created_at")


def _run_tests(ws: Workspace, fds, cfg: DetectorConfig, work: Path,
               generated_tests: Path | None, cutoff: str | None = None) -> TestResult:
    venv, home = work / "venv", work / "home"
    home.mkdir(parents=True, exist_ok=True)
    writable = [work] + ([cfg.shared_cache] if cfg.shared_cache else [])
    env_vars = {"HOME": str(home), "UV_TORCH_BACKEND": "cpu", "UV_NO_PROGRESS": "1"}
    if cutoff:
        env_vars["UV_EXCLUDE_NEWER"] = cutoff
    if cfg.shared_cache:
        env_vars.update({"UV_CACHE_DIR": str(cfg.shared_cache / "uv"),
                         "UV_PYTHON_INSTALL_DIR": str(cfg.shared_cache / "python")})
    sb = Sandbox(writable, enabled=cfg.sandbox, env=env_vars, readonly=[Path(uv_binary()).parent])
    selected = select_tests(ws.head, ws.base, fds)
    if generated_tests and Path(generated_tests).is_dir():
        for src in sorted(Path(generated_tests).glob("test_*.py")):
            rel = f"_acr_generated_tests/{src.name}"
            for root in (ws.head, ws.base):
                (root / rel).parent.mkdir(exist_ok=True)
                shutil.copy(src, root / rel)
            selected.append({"path": rel, "reason": "generated", "exists_at_base": True})
    mod_files = {}
    for fd in fds:
        if fd.new_path:
            m = module_name(ws.head, fd.new_path)
            if m:
                mod_files[m] = fd.new_path
    env = setup_environment(ws.head, venv, sb, cfg.install_timeout)
    empty = {"status": "not_run", "tests": {}}
    if not env.usable:
        return TestResult(env, selected, empty, empty, {}, {}, {}, [], "env_failed")
    head_tests = [s["path"] for s in selected]
    base_tests = [s["path"] for s in selected if s["exists_at_base"]]
    base_mods = [m for m, f in mod_files.items() if module_name(ws.base, f)]

    def run_all():
        head_run = run_pytest(ws.head, venv, head_tests, sb, home, cfg.test_timeout, "head")
        base_run = run_pytest(ws.base, venv, base_tests, sb, home, cfg.test_timeout, "base")
        imports_head = import_check(ws.head, venv, sorted(mod_files), sb, home)
        imports_base = import_check(ws.base, venv, base_mods, sb, home)
        return head_run, base_run, imports_head, imports_base

    head_run, base_run, imports_head, imports_base = run_all()
    # Repair round: install third-party modules that are missing at the *base* too (an
    # incomplete environment). Modules missing only at head are left alone, because a
    # dependency the PR forgot to declare is a defect we want to report.
    base_missing = missing_modules(
        base_run.get("log_tail", ""), *(v["detail"] for v in base_run.get("tests", {}).values()),
        *(v.get("message", "") for v in imports_base.values() if v.get("type") == "ModuleNotFoundError"))
    own = {m.split(".")[0] for m in mod_files} | {p.name for p in ws.head.iterdir() if (p / "__init__.py").is_file()}
    to_install = pypi_names(base_missing, own)
    if to_install:
        r = sb.run([uv_binary(), "pip", "install", "-q", "--python", str(venv / "bin" / "python"), *to_install],
                   cwd=ws.head, timeout=cfg.install_timeout, network=True)
        env.steps.append({"step": f"repair: install {' '.join(to_install)}", "ok": r.returncode == 0,
                          "seconds": round(r.seconds, 1), "timed_out": r.timed_out,
                          "log_tail": "" if r.returncode == 0 else r.stderr[-1500:]})
        if r.returncode == 0:
            head_run, base_run, imports_head, imports_base = run_all()
    cmp = compare_runs(head_run, base_run, selected)
    if cmp["regressions"]:
        # Confirm regressions with a second run of both versions; keep only consistent ones.
        h2 = run_pytest(ws.head, venv, head_tests, sb, home, cfg.test_timeout, "head2")
        b2 = run_pytest(ws.base, venv, base_tests, sb, home, cfg.test_timeout, "base2")
        stable = set(compare_runs(h2, b2, selected)["regressions"])
        cmp["flaky_regressions"] = [t for t in cmp["regressions"] if t not in stable]
        cmp["regressions"] = [t for t in cmp["regressions"] if t in stable]
    findings = test_findings(cmp, head_run, selected) + import_findings(imports_head, imports_base, mod_files)
    if not selected and not mod_files:
        status = "no_executable_checks"
    elif not selected:
        status = "imports_only"
    elif head_run["status"] != "completed":
        status = f"head_{head_run['status']}"
    else:
        status = "completed"
    return TestResult(env, selected, head_run, base_run, cmp, imports_head, imports_base, findings, status)


def detect(pr: dict[str, Any], diff_text: str, work: Path, cfg: DetectorConfig,
           generated_tests: Path | None = None) -> dict[str, Any]:
    t0 = time.monotonic()
    fds = parse_diff(diff_text)
    result: dict[str, Any] = {
        "pr_id": pr["pr_id"], "repository": pr["repository"], "pr_number": pr["pr_number"],
        "split": pr.get("split"), "agent": pr.get("agent"), "head_sha": pr["head_sha"],
        "tool_versions": tool_versions(), "config": {"run_tests": cfg.run_tests, "sandbox": cfg.sandbox},
    }
    try:
        ws = prepare_workspace(pr, diff_text, work / "src")
    except Exception as e:  # noqa: BLE001 - report and move on to the next PR
        result.update({"status": "checkout_failed", "error": str(e)[-500:], "findings": []})
        return result
    result["base_mode"] = ws.base_mode
    t1 = time.monotonic()
    static: StaticResult = introduced_findings(ws.head, ws.base, fds)
    result["static"] = {"files_analysed": static.files_analysed, "head_issues": static.head_issue_count,
                        "base_issues": static.base_issue_count, "per_tool_head": static.per_tool_head,
                        "per_tool_base": static.per_tool_base, "introduced": len(static.findings),
                        "seconds": round(time.monotonic() - t1, 1)}
    findings = list(static.findings)
    if cfg.run_tests:
        t2 = time.monotonic()
        cutoff = dependency_cutoff(pr)
        tests = _run_tests(ws, fds, cfg, work, generated_tests, cutoff)
        result["tests"] = tests.to_dict()
        result["tests"]["dependency_cutoff"] = cutoff
        result["tests"]["seconds"] = round(time.monotonic() - t2, 1)
        result["tests"]["head_failures"] = sum(
            v["outcome"] in FAIL for v in tests.head_run.get("tests", {}).values())
        findings += tests.findings
    ranked = rank(findings)
    result["findings"] = [f.to_dict() for f in ranked]
    result["status"] = "ok"
    result["seconds"] = round(time.monotonic() - t0, 1)
    return result
