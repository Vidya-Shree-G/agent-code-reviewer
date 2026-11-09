# agent-code-reviewer

CSE 572 Group 31 is building an automated, tool-using review agent for pull requests written by AI
coding agents. It checks those PRs for defects, requirement mismatches and unnecessary complexity.
The data comes from the AIDev-pop subset of AIDev.

## Layout

| Path | What it holds |
|---|---|
| `data/pilot/` | The original 5-PR pilot and its preprocessing script |
| `data/clean/` | The **cleaned dataset** (100 PRs), a datasheet and statistics. See [`data/clean/README.md`](data/clean/README.md) and [`STATS.md`](data/clean/STATS.md) |
| `src/agent_code_reviewer/dataset/` | **Preprocessing:** filtering, text cleaning, GitHub enrichment, splits, statistics |
| `src/agent_code_reviewer/defects/` | **Defect detection:** diff-aware static analysis (ruff, bandit), sandboxed test execution, ranked findings |
| `results/defects/` | **Defect detection results** for the 100 PRs. See [`SUMMARY.md`](results/defects/SUMMARY.md) |
| `docs/defect_detection.md` | How the defect module works and how to run it |
| `tests/` | Unit tests, including an injected-fault check for the static analyser |

## Quick start

```bash
python -m pip install -e ".[data,analysis,dev]"
pytest
agent-code-reviewer dataset build --raw RAW --cache CACHE --out data/clean --target 100
agent-code-reviewer defects run --cache CACHE --work WORK --out results/defects --jobs 8
```

Test execution needs [bubblewrap](https://github.com/containers/bubblewrap) (`apt install bubblewrap`)
on Linux, or a disposable VM with `--no-sandbox`.
