"""Tests for diff parsing, diff-aware static analysis (with injected faults) and test attribution."""

import importlib.util
import textwrap

import pytest

from agent_code_reviewer.defects.diff import is_test_path, parse_diff, text_only_diff
from agent_code_reviewer.defects.models import Finding
from agent_code_reviewer.defects.detector import rank
from agent_code_reviewer.defects.test_runner import (compare_runs, import_findings, missing_modules,
                                                     module_name, parse_junit, pypi_names, select_tests)

DIFF = """diff --git a/pkg/mod.py b/pkg/mod.py
index 1..2 100644
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,4 +1,5 @@
 import os
--- a comment line that starts with two dashes
+def f(x=[]):
+    return undefined_name
 y = 1
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+a = 1
+b = 2
diff --git a/img.png b/img.png
Binary files a/img.png and b/img.png differ
"""


def test_parse_diff():
    files = parse_diff(DIFF)
    assert [f.path for f in files] == ["pkg/mod.py", "new.py", "img.png"]
    mod, new, img = files
    assert mod.status == "modified" and mod.old_path == "pkg/mod.py"
    assert mod.added_lines == {2, 3} and mod.removed_lines == {2}
    assert new.status == "added" and new.added_lines == {1, 2}
    assert img.binary and "img.png" not in text_only_diff(files)


def test_is_test_path():
    assert is_test_path("tests/helpers.py") and is_test_path("pkg/test_x.py") and is_test_path("x_test.py")
    assert not is_test_path("pkg/contest.py")


needs_tools = pytest.mark.skipif(not (importlib.util.find_spec("ruff") and importlib.util.find_spec("bandit")),
                                 reason="ruff and bandit are needed")


@needs_tools
def test_static_analysis_reports_only_injected_faults(tmp_path):
    """Inject three known faults into a file with a pre-existing issue and a reformatted line."""
    from agent_code_reviewer.defects.static_analysis import introduced_findings

    base, head = tmp_path / "base", tmp_path / "head"
    (base / "pkg").mkdir(parents=True)
    (head / "pkg").mkdir(parents=True)
    base_src = textwrap.dedent("""\
        import os
        import subprocess


        def run(cmd):
            return subprocess.call(cmd, shell=True)
        """)
    head_src = textwrap.dedent("""\
        import os
        import subprocess


        def run(cmd):
            return subprocess.call(cmd,  shell=True)


        def add(item, items=[]):
            items.append(item)
            return itemz


        def load(path):
            return eval(open(path).read())
        """)
    (base / "pkg/mod.py").write_text(base_src)
    (head / "pkg/mod.py").write_text(head_src)
    import difflib
    diff = "diff --git a/pkg/mod.py b/pkg/mod.py\n" + "".join(difflib.unified_diff(
        base_src.splitlines(True), head_src.splitlines(True), "a/pkg/mod.py", "b/pkg/mod.py"))
    res = introduced_findings(head, base, parse_diff(diff))
    rules = {(f.tool, f.rule) for f in res.findings}
    assert ("ruff", "F821") in rules        # undefined name `itemz`
    assert ("ruff", "B006") in rules        # mutable default argument
    assert ("bandit", "B307") in rules      # eval
    assert ("ruff", "F401") not in rules    # unused `os` existed before the PR
    assert ("bandit", "B602") not in rules  # shell=True existed before; only whitespace changed
    assert all(f.on_changed_line for f in res.findings)
    f821 = next(f for f in res.findings if f.rule == "F821")
    assert f821.severity == "high" and f821.line == 11


def test_rank_orders_by_severity_and_dedupes():
    low = Finding("ruff", "F401", "maintainability", "low", "high", "m", "a.py", 1)
    high = Finding("ruff", "F821", "correctness", "high", "high", "m", "a.py", 2)
    assert [f.rule for f in rank([low, high, high])] == ["F821", "F401"]


def test_parse_junit_and_compare(tmp_path):
    xml = tmp_path / "j.xml"
    xml.write_text("""<testsuites><testsuite>
      <testcase classname="tests.test_a" name="test_ok"/>
      <testcase classname="tests.test_a" name="test_broken"><failure message="boom">tests/test_a.py:7: AssertionError</failure></testcase>
      <testcase classname="tests.test_a" name="test_new"><error message="x"/></testcase>
      <testcase classname="tests.test_a" name="test_skip"><skipped/></testcase>
      <testcase classname="tests.test_a" name="test_fixed"/>
    </testsuite></testsuites>""")
    head = {"tests": parse_junit(xml)}
    assert head["tests"]["tests.test_a::test_broken"]["outcome"] == "failed"
    assert head["tests"]["tests.test_a::test_skip"]["outcome"] == "skipped"
    base = {"tests": {"tests.test_a::test_ok": {"outcome": "passed"},
                      "tests.test_a::test_broken": {"outcome": "passed"},
                      "tests.test_a::test_fixed": {"outcome": "failed"}}}
    cmp = compare_runs(head, base, [])
    assert cmp["regressions"] == ["tests.test_a::test_broken"]
    assert cmp["new_test_failures"] == ["tests.test_a::test_new"]
    assert cmp["fixed"] == ["tests.test_a::test_fixed"]


def test_collection_error_on_existing_module_is_regression():
    head = {"tests": {"::tests.test_a": {"outcome": "error", "detail": "ImportError"}}}
    base = {"tests": {"tests.test_a::test_x": {"outcome": "passed"}}}
    assert compare_runs(head, base, [])["regressions"] == ["::tests.test_a"]


def test_module_name_and_selection(tmp_path):
    for p in ("src/pkg/__init__.py", "src/pkg/core.py", "ns/thing.py", "scripts/run.py",
              "tests/test_core.py", "tests/test_other.py"):
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text("from pkg import core\n" if p.endswith("test_other.py") else "")
    assert module_name(tmp_path, "src/pkg/core.py") == "pkg.core"
    assert module_name(tmp_path, "src/pkg/__init__.py") == "pkg"
    assert module_name(tmp_path, "ns/thing.py") == "ns.thing"     # namespace package
    assert module_name(tmp_path, "scripts/run.py") is None
    sel = select_tests(tmp_path, tmp_path, parse_diff(
        "diff --git a/src/pkg/core.py b/src/pkg/core.py\n--- a/src/pkg/core.py\n+++ b/src/pkg/core.py\n"
        "@@ -1 +1 @@\n-a\n+b\n"))
    assert [s["path"] for s in sel] == ["tests/test_core.py"]


def test_missing_module_helpers_and_import_findings():
    assert missing_modules("ModuleNotFoundError: No module named 'PIL'") == {"PIL"}
    assert pypi_names({"PIL", "yaml.loader", "mypkg.sub"}, {"mypkg"}) == ["pillow", "pyyaml"]
    head = {"pkg.a": {"ok": False, "type": "NameError", "message": "x", "file": "pkg/a.py", "line": 3},
            "pkg.b": {"ok": False, "type": "ModuleNotFoundError", "message": "No module named 'foo'"},
            "pkg.c": {"ok": False, "type": "ImportError", "message": "y"}}
    base = {"pkg.a": {"ok": True}, "pkg.c": {"ok": False, "type": "ImportError"}}
    fs = {f.evidence["module"]: f for f in import_findings(head, base, {"pkg.b": "pkg/b.py"})}
    assert fs["pkg.a"].rule == "import-error" and fs["pkg.a"].severity == "high"
    assert fs["pkg.b"].rule == "missing-dependency" and fs["pkg.b"].file == "pkg/b.py"
    assert "pkg.c" not in fs  # fails the same way before the PR
