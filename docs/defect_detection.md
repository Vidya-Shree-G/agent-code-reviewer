# Defect detection and executable testing

This module implements step 3 (defect analysis) of the reviewer agent. It also serves as baseline
**B1** (static analysis only) when run with `--no-tests`. Code is in
[`src/agent_code_reviewer/defects/`](../src/agent_code_reviewer/defects/), and results are in
[`results/defects/`](../results/defects/).

## Pipeline for one PR

```
dataset row + cached diff
   │
   ├─ workspace.py   check out head_sha; base = head with the PR diff reverse-applied
   │                 (this equals the merge base; falls back to base_sha if the diff will not apply)
   ├─ static_analysis.py   ruff + bandit + compile() on changed .py files, at head AND base
   │                 → keep only findings the PR introduced
   ├─ test_runner.py  sandboxed venv → related tests at head and base → compare outcomes
   │                 + import every changed module at head and base
   └─ detector.py    merge → drop duplicates → rank (severity × confidence, changed lines first)
```

Every finding carries evidence: the tool rule and source line, or the failing test with its
output. This is the "drop findings without evidence" rule from the proposal.

## Static analysis integration

| Tool | What it checks | Rules |
|---|---|---|
| ruff (with `--isolated`, so repository configs are ignored and every PR gets the same rules) | Correctness, not style | `F` (pyflakes), `E9`, `B` (bugbear), `PLE`, `PLW`, `ASYNC`, `RUF006/008/009/012` |
| bandit | Security, with **CWE ids** (cf. Pearce et al.) | All checks except `B101` (assert). Seven noisy checks are skipped in test files. |
| `compile()` | Syntax errors under Python 3.12 | n/a |

Each rule has a category, severity and confidence, defined in
[`models.py`](../src/agent_code_reviewer/defects/models.py). For example, F821 (undefined name) is
correctness, high, high; B006 (mutable default) is correctness, medium, high; F401 (unused import)
is maintainability, low, high.

**Diff-aware attribution.** Each finding gets a fingerprint of (tool, rule, source line with
whitespace removed), computed at both head and base. The base multiset is then subtracted. A
second pass matches on (tool, rule, message), so code that was only reformatted or moved does not
count as new. This removed two false positives from a pure reformatting PR in the pilot.
`on_changed_line` marks findings on lines the PR added. Findings elsewhere are still kept,
because removing a definition can break a line the PR never touched.

## Test execution

1. **Environment.** `uv venv` plus an editable install with `test`/`tests`/`testing`/`dev` extras,
   PEP 735 dependency groups, Poetry dev groups and the usual `requirements*.txt` files, then
   `pytest` and `pytest-timeout`.
   - **Date pinning.** Dependencies resolve with `UV_EXCLUDE_NEWER` set to the date the PR was
     closed, or created if still open. Otherwise today's releases break 2025 code: in the pilot,
     mteb failed to import because a `transformers` class had since been removed.
   - **CPU wheels.** Torch installs CPU wheels (`UV_TORCH_BACKEND=cpu`).
2. **Test selection.** The steps below stop at the first that finds tests, up to 25 files:
   1. test files the PR added or changed, plus existing `test_<module>.py` / `<module>_test.py`
      files for each changed module;
   2. test files that import a changed module;
   3. the whole suite, if it has 30 or fewer test files.
3. **Run at head and base**, using one venv, with `PYTHONPATH` pointing at the version under test.
   If the repository's `addopts` break pytest, the run is retried without them.
4. **Repair round.** Third-party modules that are missing at the **base** are installed (for
   example `PIL` → `pillow`) and everything is rerun once. Modules missing only at head are not
   installed, so a dependency the PR forgot to declare is still reported.
5. **Attribution.**

| Head outcome | Base outcome | Result |
|---|---|---|
| fail/error | pass | **`test-regression`** (high), confirmed by rerunning both versions; flaky cases are dropped |
| fail/error | test absent (added by the PR) | **`new-test-failure`** (medium): the agent's own test fails |
| module import error | imports | **`import-error`** (high) |
| `ModuleNotFoundError` | imports, or module is new | **`missing-dependency`** (medium; low confidence if the module is new) |
| fail | fail | Pre-existing failure or environment problem. Counted, but not a finding |
| pass | fail | `fixed`. Counted, but not a finding |

6. **Sandbox.** Installs, tests and imports run under bubblewrap:
   - the host filesystem is read-only;
   - `/home`, `/root`, `/media`, `/mnt` and `/run` are hidden, so no credentials are visible;
   - only the PR's work directory and the package cache are writable;
   - the network is off for tests and imports, and on only while installing;
   - environment variables are scrubbed.

   `--no-sandbox` exists for machines without `bwrap`. Only use it inside a disposable VM or
   container.

**Hook for agent-written tests.** `detect(..., generated_tests=DIR)` copies `DIR/test_*.py` into
both versions and runs them alongside the selected tests. The LLM step should use this to execute
its targeted edge-case and CWE tests.

## Running it

```bash
pip install -e ".[analysis,dev]"           # ruff, bandit, uv, pytest
agent-code-reviewer defects run --cache CACHE --work /dev/shm/acr-work --out results/defects --jobs 8
agent-code-reviewer defects run ... --no-tests --out results/b1_static   # baseline B1
agent-code-reviewer defects run ... --pr 3071250357 --keep-work           # one PR; keep its checkout
agent-code-reviewer defects summarize --results results/defects
```

`CACHE` is the cache directory from `dataset build`, which holds the diffs. The work directory
needs a few GB of space. On our machine we used `/dev/shm`.

## Outputs (`results/defects/`)

| File | Contents |
|---|---|
| `prs/<pr_id>.json` | Full record for one PR: tool versions, base mode, static counts, environment steps, selected tests, failing tests at head and base, import results, ranked findings. |
| `findings.jsonl` | One finding per line, with PR metadata and whether a human review comment sits in the same file or within 3 lines. |
| `pr_results.jsonl` | One summary row per PR. |
| `summary.json`, `SUMMARY.md` | The aggregate tables. |

## Limitations

- **Findings are unvalidated.** They are suspected defects, not labels. Precision and recall need
  the reference labels (test outcomes, injected faults, human annotation) planned for H1.
- **Test coverage is limited.** Many PRs have no related tests or cannot build an environment, and
  tests miss what they do not cover (Liu et al.). See `SUMMARY.md` for the actual coverage.
- **Lint-level rules are noisy.** Some rules (F401, B904, PLW) are low severity and should not
  count as defects in H1 without filtering by severity.
- **Python only.** The static rules and the test runner assume Python.
