"""Executable testing: build an environment, run related tests at head and base.

A failure is attributed to the PR only when the same test passes at the base
(a regression), when a test added by the PR fails, or when a changed module
imports at the base but not at the head. Failures present at both versions are
environment or pre-existing problems and are reported separately, not as
findings. Regression candidates are re-run once at both versions to filter out
flaky tests.

All repository code runs inside :class:`~.sandbox.Sandbox` with the network
off (dependency installation is the only step with network access).
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .diff import FileDiff, is_python_path, is_test_path
from .models import Finding
from .sandbox import Result, Sandbox

TEST_EXTRAS = ("test", "tests", "testing", "dev")
REQUIREMENT_FILES = ("requirements.txt", "requirements-dev.txt", "requirements_dev.txt",
                     "requirements-test.txt", "requirements_test.txt", "requirements-tests.txt",
                     "test-requirements.txt", "dev-requirements.txt", "requirements/test.txt",
                     "requirements/dev.txt", "requirements/tests.txt", "requirements/base.txt")
SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "site-packages", "build", "dist", ".tox"}
NON_PACKAGE_DIRS = {"scripts", "script", "examples", "example", "docs", "doc", "tests", "test", "benchmarks",
                    "benchmark", "tools", "bin", "notebooks", "dev", "ci", "contrib", "samples", "demo"}
MAX_TESTS_PER_MODULE = 5
FULL_SUITE_MAX_FILES = 30
MAX_TEST_FILES = 25
FAIL = ("failed", "error")


def uv_binary() -> str:
    local = Path(sys.executable).parent / "uv"
    return str(local) if local.is_file() else (shutil.which("uv") or "uv")


@dataclass
class EnvResult:
    status: str                      # ok | partial | failed
    python: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.status in ("ok", "partial")


def _tail(text: str, n: int = 1500) -> str:
    return text[-n:] if text else ""


def _pyproject(head: Path) -> dict:
    try:
        return tomllib.loads((head / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def setup_environment(head: Path, venv: Path, sandbox: Sandbox, timeout: int) -> EnvResult:
    uv = uv_binary()
    env = EnvResult(status="ok")

    def step(name: str, cmd: list[str], *, required: bool = False) -> bool:
        r = sandbox.run(cmd, cwd=head, timeout=timeout, network=True)
        ok = r.returncode == 0
        env.steps.append({"step": name, "ok": ok, "seconds": round(r.seconds, 1),
                          "timed_out": r.timed_out, "log_tail": "" if ok else _tail(r.stderr or r.stdout)})
        if not ok and required:
            env.status = "failed"
        elif not ok and env.status == "ok":
            env.status = "partial"
        return ok

    if not step("venv", [uv, "venv", "-q", str(venv)]):
        env.steps.clear()
        env.status = "ok"
        if not step("venv", [uv, "venv", "-q", "--python", "3.12", str(venv)], required=True):
            return env
    py = str(venv / "bin" / "python")
    pip = [uv, "pip", "install", "-q", "--python", py]
    meta = _pyproject(head)
    is_project = any((head / f).is_file() for f in ("pyproject.toml", "setup.py", "setup.cfg"))
    if is_project:
        extras = [e for e in TEST_EXTRAS
                  if e in (meta.get("project", {}).get("optional-dependencies") or {})]
        installed = bool(extras) and step(f"install -e .[{','.join(extras)}]",
                                          pip + ["-e", f".[{','.join(extras)}]"])
        if not installed:
            if extras:  # the extras attempt failed; let the plain install decide the status
                env.status = "ok"
                env.steps[-1]["superseded"] = True
            step("install -e .", pip + ["-e", "."])
        groups = [g for g in TEST_EXTRAS if g in (meta.get("dependency-groups") or {})]
        for g in groups:
            step(f"install --group {g}", pip + ["--group", g])
        poetry_groups = (meta.get("tool", {}).get("poetry", {}).get("group") or {})
        names = sorted({n for g in TEST_EXTRAS for n in
                        (poetry_groups.get(g, {}).get("dependencies") or {}) if n != "python"})
        if names:
            step("install poetry test/dev group", pip + names)
    for req in REQUIREMENT_FILES:
        if (head / req).is_file():
            step(f"install -r {req}", pip + ["-r", req])
    step("install pytest", pip + ["pytest", "pytest-timeout"], required=True)
    r = sandbox.run([py, "-c", "import sys; print(sys.version.split()[0])"], cwd=head, timeout=60)
    env.python = r.stdout.strip() or None
    return env


# --- test selection ---------------------------------------------------------

def is_pytest_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _test_files(root: Path) -> list[str]:
    out = []
    for p in root.rglob("*.py"):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        if is_pytest_file(rel.as_posix()):
            out.append(rel.as_posix())
    return sorted(out)


def select_tests(head: Path, base: Path, file_diffs: list[FileDiff]) -> list[dict[str, Any]]:
    """Tests changed by the PR plus existing tests named after the changed modules."""
    chosen: dict[str, str] = {}
    for fd in file_diffs:
        if fd.new_path and is_pytest_file(fd.new_path) and (head / fd.new_path).is_file():
            chosen[fd.new_path] = "changed_in_pr"
    stems = sorted({PurePosixPath(fd.new_path).stem for fd in file_diffs
                    if fd.new_path and is_python_path(fd.new_path) and not is_test_path(fd.new_path)}
                   - {"__init__", "__main__", "setup", "conftest", "main", "utils", "config"})
    all_tests = _test_files(head)
    for stem in stems:
        pat = re.compile(rf"^(?:test_{re.escape(stem)}(?:_\w+)?|{re.escape(stem)}_test)\.py$")
        hits = [t for t in all_tests if pat.match(PurePosixPath(t).name) and t not in chosen]
        for t in hits[:MAX_TESTS_PER_MODULE]:
            chosen[t] = f"matches_module:{stem}"
    if not chosen:
        # Fallback 1: tests that import a changed module by its dotted name or stem.
        targets = []
        for fd in file_diffs:
            if fd.new_path and is_python_path(fd.new_path) and not is_test_path(fd.new_path):
                mod = module_name(head, fd.new_path)
                if mod:
                    pkg, _, stem = mod.rpartition(".")
                    targets.append(re.compile(rf"\b{re.escape(mod)}\b" + (
                        rf"|from\s+{re.escape(pkg)}\s+import\s+[^\n]*\b{re.escape(stem)}\b" if pkg else "")))
        for t in all_tests:
            if targets and len(chosen) < MAX_TEST_FILES:
                try:
                    text = (head / t).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if any(p.search(text) for p in targets):
                    chosen[t] = "imports_changed_module"
    if not chosen and 0 < len(all_tests) <= FULL_SUITE_MAX_FILES:
        # Fallback 2: the whole (small) test suite.
        for t in all_tests:
            chosen[t] = "full_suite"
    selected = list(chosen.items())[:MAX_TEST_FILES]
    return [{"path": p, "reason": why, "exists_at_base": (base / p).is_file()} for p, why in selected]


# Import names whose PyPI distribution has a different name.
PYPI_NAMES = {"PIL": "pillow", "cv2": "opencv-python-headless", "sklearn": "scikit-learn", "yaml": "pyyaml",
              "bs4": "beautifulsoup4", "dateutil": "python-dateutil", "dotenv": "python-dotenv",
              "jwt": "pyjwt", "git": "gitpython", "Crypto": "pycryptodome", "attr": "attrs",
              "magic": "python-magic", "serial": "pyserial", "usb": "pyusb", "zmq": "pyzmq",
              "skimage": "scikit-image", "docx": "python-docx", "pptx": "python-pptx", "fitz": "pymupdf",
              "OpenSSL": "pyopenssl", "jose": "python-jose", "multipart": "python-multipart",
              "telegram": "python-telegram-bot", "slugify": "python-slugify", "Levenshtein": "levenshtein",
              "google.protobuf": "protobuf", "sentencepiece": "sentencepiece", "tomli_w": "tomli-w"}
_NO_MODULE = re.compile(r"No module named '([\w.]+)'")


def missing_modules(*texts: str) -> set[str]:
    out = set()
    for t in texts:
        out.update(_NO_MODULE.findall(t or ""))
    return out


def pypi_names(modules: set[str], own_packages: set[str]) -> list[str]:
    names = set()
    for m in modules:
        top = m.split(".")[0]
        if top in own_packages or top.startswith("_"):
            continue
        names.add(PYPI_NAMES.get(m) or PYPI_NAMES.get(top) or top)
    return sorted(names)


# --- running pytest -----------------------------------------------------------

def _run_env(root: Path, home: Path) -> dict[str, str]:
    return {"PYTHONPATH": f"{root / 'src'}:{root}", "HOME": str(home), "XDG_CACHE_HOME": str(home / ".cache"),
            "MPLBACKEND": "Agg", "PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1", "NO_COLOR": "1", "PYTHONHASHSEED": "0"}


def parse_junit(path: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        return out
    for tc in tree.iter("testcase"):
        tid = f"{tc.get('classname', '')}::{tc.get('name', '')}"
        outcome, text = "passed", ""
        for child in tc:
            if child.tag in ("failure", "error"):
                outcome = "failed" if child.tag == "failure" else "error"
                text = (child.get("message") or "") + "\n" + (child.text or "")
                break
            if child.tag == "skipped":
                outcome = "skipped"
        out[tid] = {"outcome": outcome, "detail": text.strip()[-1200:]}
    return out


def run_pytest(root: Path, venv: Path, tests: list[str], sandbox: Sandbox, home: Path, timeout: int,
               tag: str) -> dict[str, Any]:
    if not tests:
        return {"status": "not_run", "tests": {}}
    xml = home.parent / f"junit-{tag}.xml"
    base_cmd = [str(venv / "bin" / "python"), "-m", "pytest", "-p", "no:cacheprovider", "-q",
                "--timeout=120", "-o", "junit_family=xunit1", f"--junitxml={xml}"]
    r: Result = sandbox.run(base_cmd + tests, cwd=root, timeout=timeout, env=_run_env(root, home))
    retried = False
    if r.returncode == 4 or "unrecognized arguments" in (r.stderr or ""):
        retried = True
        r = sandbox.run(base_cmd + ["-o", "addopts="] + tests, cwd=root, timeout=timeout,
                        env=_run_env(root, home))
    results = parse_junit(xml)
    counts = {k: sum(v["outcome"] == k for v in results.values())
              for k in ("passed", "failed", "error", "skipped")}
    status = ("timeout" if r.timed_out else "no_tests_collected" if r.returncode == 5
              else "usage_error" if r.returncode in (3, 4) and not results else "completed")
    return {"status": status, "exit_code": r.returncode, "seconds": round(r.seconds, 1),
            "addopts_overridden": retried, "counts": counts, "tests": results,
            "log_tail": _tail(r.stdout + "\n" + r.stderr, 2500) if status != "completed" else ""}


# --- import check ---------------------------------------------------------------

_IMPORT_SCRIPT = r"""
import importlib, json, os, sys, traceback
root = os.getcwd()
res = {}
for m in sys.argv[1:]:
    try:
        importlib.import_module(m)
        res[m] = {"ok": True}
    except BaseException as e:
        frames = [f for f in traceback.extract_tb(e.__traceback__) if f.filename.startswith(root)]
        last = frames[-1] if frames else None
        line = getattr(e, "lineno", None) if isinstance(e, SyntaxError) else (last.lineno if last else None)
        fname = getattr(e, "filename", None) if isinstance(e, SyntaxError) else (last.filename if last else None)
        res[m] = {"ok": False, "type": type(e).__name__, "message": str(e)[:400],
                  "missing_module": getattr(e, "name", None) if isinstance(e, ModuleNotFoundError) else None,
                  "file": os.path.relpath(fname, root) if fname and fname.startswith(root) else None,
                  "line": line, "traceback": "".join(traceback.format_exception(e))[-1500:]}
print("@@IMPORT@@" + json.dumps(res))
"""


def module_name(root: Path, rel: str) -> str | None:
    """Dotted module name for a file inside a package, else None (scripts are not imported)."""
    p = PurePosixPath(rel)
    if p.suffix != ".py" or p.name in ("__main__.py", "setup.py", "conftest.py") or is_test_path(rel):
        return None
    parts = list(p.parts)
    if parts[0] == "src":
        parts = parts[1:]
    if len(parts) < 2:
        return None
    pkg_root = root / "src" if PurePosixPath(rel).parts[0] == "src" else root
    if parts[0].lower() in NON_PACKAGE_DIRS:
        return None
    # Regular packages need __init__.py at every level; a top-level directory without one
    # is accepted as a PEP 420 namespace package only if nothing below it has __init__.py.
    inits = [(pkg_root.joinpath(*parts[:i]) / "__init__.py").is_file() for i in range(1, len(parts))]
    if not all(inits) and any(inits):
        return None
    mod = parts[:-1] + ([] if p.stem == "__init__" else [p.stem])
    return ".".join(mod) if all(m.isidentifier() for m in mod) else None


def import_check(root: Path, venv: Path, modules: list[str], sandbox: Sandbox, home: Path) -> dict:
    if not modules:
        return {}
    r = sandbox.run([str(venv / "bin" / "python"), "-c", _IMPORT_SCRIPT, *modules], cwd=root,
                    timeout=300, env=_run_env(root, home))
    for line in r.stdout.splitlines():
        if line.startswith("@@IMPORT@@"):
            return json.loads(line[len("@@IMPORT@@"):])
    return {m: {"ok": None, "type": "Timeout" if r.timed_out else "NoResult",
                "message": _tail(r.stderr, 300)} for m in modules}


# --- attribution -------------------------------------------------------------

_LOC = re.compile(r"^([\w./-]+\.py):(\d+):", re.M)


def _location(detail: str, test_file: str | None) -> tuple[str | None, int | None]:
    hits = _LOC.findall(detail or "")
    if hits:
        for f, ln in reversed(hits):
            if not f.startswith(("/", "..")):
                return f, int(ln)
    return test_file, None


def _module_of(tid: str) -> str:
    cls, name = tid.split("::", 1)
    return name if not cls else cls


def _test_file_for(tid: str, files: list[str]) -> str | None:
    mod = _module_of(tid)
    for f in files:
        dotted = f[:-3].replace("/", ".")
        if mod == dotted or mod.startswith(dotted + "."):
            return f
    return None


def compare_runs(head_run: dict, base_run: dict, selected: list[dict]) -> dict[str, list[str]]:
    head, base = head_run.get("tests", {}), base_run.get("tests", {})
    base_modules_ok = {_module_of(t) for t, v in base.items() if v["outcome"] == "passed"}
    out = {"regressions": [], "new_test_failures": [], "fixed": [], "persistent_failures": []}
    for tid, v in head.items():
        b = base.get(tid, {}).get("outcome")
        if v["outcome"] in FAIL:
            if b == "passed":
                out["regressions"].append(tid)
            elif b is None:
                # A collection error on a module that collected fine at base is a regression too.
                mod = _module_of(tid)
                if tid.startswith("::") and any(m == mod or m.startswith(mod + ".") for m in base_modules_ok):
                    out["regressions"].append(tid)
                else:
                    out["new_test_failures"].append(tid)
            else:
                out["persistent_failures"].append(tid)
        elif v["outcome"] == "passed" and b in FAIL:
            out["fixed"].append(tid)
    return out


def test_findings(cmp: dict[str, list[str]], head_run: dict, selected: list[dict]) -> list[Finding]:
    files = [s["path"] for s in selected]
    findings = []
    for kind, rule, sev, conf in (("regressions", "test-regression", "high", "high"),
                                  ("new_test_failures", "new-test-failure", "medium", "medium")):
        for tid in cmp[kind]:
            detail = head_run["tests"][tid]["detail"]
            tfile = _test_file_for(tid, files)
            f, ln = _location(detail, tfile)
            first = next((ln_ for ln_ in detail.splitlines() if ln_.strip()), "")
            msg = (f"Test {tid} passes at base but fails at head" if kind == "regressions"
                   else f"Test {tid} added or changed by the PR fails at head")
            findings.append(Finding(
                tool="pytest", rule=rule, category="test", severity=sev, confidence=conf,
                message=f"{msg}: {first[:200]}", file=f, line=ln,
                evidence={"type": "test", "test_id": tid, "outcome": head_run["tests"][tid]["outcome"],
                          "detail": detail}, sources=["pytest"]))
    return findings


def import_findings(head_res: dict, base_res: dict, mod_files: dict[str, str]) -> list[Finding]:
    out = []
    for mod, h in head_res.items():
        if h.get("ok") is not False:
            continue
        b = base_res.get(mod)
        if b and b.get("ok") is False and b.get("type") == h.get("type"):
            continue  # fails the same way before the PR: environment or pre-existing
        if b and b.get("ok") is None:
            continue
        missing = h.get("type") == "ModuleNotFoundError"
        if missing and not b:
            # New module importing an uninstalled package: likely an undeclared dependency,
            # but our install may simply have missed an optional extra.
            rule, sev, conf = "missing-dependency", "medium", "low"
        elif missing:
            rule, sev, conf = "missing-dependency", "medium", "medium"
        else:
            rule, sev, conf = "import-error", "high", "high"
        out.append(Finding(
            tool="import_check", rule=rule, category="correctness", severity=sev, confidence=conf,
            message=f"Importing {mod} fails at head ({h.get('type')}: {h.get('message', '')[:200]})"
                    + ("" if not b else " but succeeds at base"),
            file=h.get("file") or mod_files.get(mod), line=h.get("line"),
            evidence={"type": "test", "module": mod, "head": h, "base": b}, sources=["import_check"]))
    return out


@dataclass
class TestResult:
    env: EnvResult | None
    selected_tests: list[dict]
    head_run: dict
    base_run: dict
    comparison: dict
    imports_head: dict
    imports_base: dict
    findings: list[Finding]
    status: str

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k != "findings"}
        for run in ("head_run", "base_run"):
            # keep failing tests' details, summarise passing ones
            d[run] = {**d[run], "tests": {t: v for t, v in d[run].get("tests", {}).items()
                                          if v["outcome"] in FAIL}}
        return d
