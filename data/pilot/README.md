# CSE 572 Group 31 — AIDev-pop dataset pilot

This pilot is for the Automated Code-Review Agent for Agent-Generated Code.

Source: [AIDev archive, Zenodo record 16919051](https://zenodo.org/records/16919051).
Credit: Hao Li, Haoxiang Zhang, and Ahmed E. Hassan, *The Rise of AI Teammates in
Software Engineering (SE) 3.0: How Autonomous Coding Agents Are Reshaping Software
Engineering*, https://doi.org/10.48550/arXiv.2507.15003.

## Contents

- `prepare_pilot.py`: joins archived Parquet tables, filters metadata, and selects up to one PR per repository with a fixed seed. It optionally screens locally fetched patches for Python changes.
- `pilot_candidates.jsonl`: all 10 sampled candidates, one JSON object per line.
- `pilot_python_prs.jsonl`: five candidates whose accessible patches included Python file changes.
- `pilot_stats.json`: source counts, metadata exclusions, and pilot counts.

A record includes the archive PR ID, GitHub repository and PR number, archived stars, agent, PR description, linked issues when present, PR and patch URLs, split, and patch screening details. The candidate split and the screened split are separate; use the screened file's split for the pilot. No repository overlaps between its four development PRs and one evaluation PR. This is a workflow pilot, not a large enough sample for performance claims.

## What was checked

The archive contains 33,596 subset PR rows across 2,807 repository records. Joining metadata and filtering for Python repositories with more than 100 stars, a nonempty PR description or linked issue body, and no obvious documentation-only title left 6,913 candidates across 491 repositories. The script sampled 10 PRs from 10 repositories with seed 572. Five accessible patches showed Python changes, two accessible patches did not, and three remained unverified after the download attempt. A repository's primary language is only a first filter; the patch screening checks the actual files.

Before using the five PRs for evaluation, review the requirement text for specificity, locate relevant existing tests and repository files, and create independent reference labels. Changed test file hints describe patch paths only. Tests were not run, full repository snapshots were not fetched, and no review agent results were produced. The PR descriptions are author-provided context, not ground truth. The package links to patches without redistributing raw patch files.

## Reproduce locally

1. Download `pull_request.parquet`, `repository.parquet`, `related_issue.parquet`, and `issue.parquet` from the archived record into a folder named `raw`.
2. Run `python -m pip install pyarrow`.
3. Run `python prepare_pilot.py --raw raw --out pilot`.
4. Download each candidate's `patch_url` into `patches/<pr_id>.patch`. Rerun with `python prepare_pilot.py --raw raw --out pilot --patch-dir patches`. Live GitHub access can change screening outcomes; the included statistics describe this run.

## Suggested progress report wording

We inspected the archived AIDev-pop subset, which contains 33,596 pull requests across 2,807 repositories. We applied preliminary metadata filters and selected 10 candidate pull requests from distinct Python repositories. Five had accessible patches containing Python changes; these form an initial pilot split into four development examples and one evaluation example, with no repository overlap. Requirement and test verification and reference labeling are pending.

## Suggested GitHub handoff

Create a branch such as `dataset-pilot` and place these files in a `data/pilot/` folder. Keep downloaded source Parquet files and raw patches local. Suggested commit message: `Add AIDev-pop dataset pilot`.
