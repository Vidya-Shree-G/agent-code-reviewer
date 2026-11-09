"""Tests for dataset cleaning, candidate building, splitting and statistics."""

from agent_code_reviewer.dataset.aidev import build_candidates, is_bot, review_evidence
from agent_code_reviewer.dataset.clean import (clean_issue_body, clean_pr_body, is_documentation_title,
                                               word_count)
from agent_code_reviewer.dataset.pipeline import assign_splits
from agent_code_reviewer.dataset.stats import compute_stats, stats_markdown
from agent_code_reviewer.defects.report import comment_line

CODEX = """## Summary
- prevent sharing state between `WfProjectContext` instances

## Testing
- `pytest -q` *(fails: 3 errors during collection)*

------
https://chatgpt.com/codex/tasks/task_e_686702656dfc8333b0c517c4f34a51c8"""

COPILOT = """Thanks for assigning this issue to me. I'm starting to work on it.

Original issue description:

> Export should always add the raw timestamps.

Fixes #7330.

<!-- START COPILOT CODING AGENT TIPS -->
---

💡 You can make Copilot smarter by setting up custom instructions."""

DEVIN = """Fixes #2082

This PR lets Crew.test() use any LLM.

```mermaid
graph TD
  A --> B
```

Link to Devin run: https://app.devin.ai/sessions/d7f3dae93fe3480b97d3cdced861782e
Requested by: Someone (someone@example.com)"""


def test_codex_testing_section_is_split_out():
    c = clean_pr_body(CODEX)
    assert c.description == "## Summary\n- prevent sharing state between `WfProjectContext` instances"
    assert "pytest -q" in c.agent_reported_testing
    assert c.agent_reported_test_failure
    assert "chatgpt.com" not in c.description + c.agent_reported_testing
    assert "testing_section_split" in c.removed


def test_copilot_boilerplate_removed_issue_text_kept():
    c = clean_pr_body(COPILOT)
    assert "raw timestamps" in c.description and "Fixes #7330." in c.description
    assert "Copilot smarter" not in c.description
    assert "Thanks for assigning" not in c.description
    assert "Original issue description" not in c.description
    assert "copilot_tips" in c.removed


def test_devin_boilerplate_and_mermaid_removed():
    c = clean_pr_body(DEVIN)
    assert c.description == "Fixes #2082\n\nThis PR lets Crew.test() use any LLM."
    assert {"mermaid", "boilerplate_line"} <= set(c.removed)


def test_crlf_html_comments_and_empty_input():
    assert clean_issue_body("a\r\n<!-- template -->\r\nb") == "a\n\nb"
    assert clean_pr_body(None).description == ""
    assert word_count("fix `foo_bar` in 2 places") == 5


def test_documentation_title():
    assert is_documentation_title("docs: update README")
    assert is_documentation_title("Docs(api) fix typo")
    assert not is_documentation_title("Add docstring checker")


def test_is_bot():
    assert is_bot("coderabbitai[bot]") and is_bot("copilot") and is_bot("ellipsis-dev")
    assert not is_bot("octocat")


def _tables():
    repos = [{"id": 1, "full_name": "a/x", "language": "Python", "stars": 500, "license": "MIT"},
             {"id": 2, "full_name": "b/y", "language": "Python", "stars": 50, "license": None},
             {"id": 3, "full_name": "c/z", "language": "Python", "stars": 900, "license": None},
             {"id": 4, "full_name": "d/w", "language": "Rust", "stars": 900, "license": None}]
    long = "Fix the parser so that empty lines no longer crash the tokenizer loop."

    def pr(i, repo, number, title, body, merged="None"):
        return {"id": str(i), "number": str(number), "title": title, "body": body, "agent": "Devin",
                "state": "closed", "created_at": "2025-06-01T00:00:00Z", "closed_at": None,
                "merged_at": merged, "repo_id": str(repo)}

    prs = [pr(10, 1, 1, "Fix parser", long, merged="2025-06-02T00:00:00Z"),
           pr(11, 1, 2, "docs: readme", long),
           pr(12, 2, 3, "Fix", long),
           pr(13, 3, 4, "Tweak", "ok"),
           pr(14, 3, 5, "Tweak", "ok"),
           pr(15, 4, 6, "Fix", long),
           pr(16, 3, 7, "Improve cache", "")]
    issues = [{"id": 100, "number": 9, "title": "Cache misses on reload",
               "body": "The cache is rebuilt from scratch every time the config reloads.",
               "state": "open", "html_url": "u"}]
    return {"pull_request": prs, "repository": repos, "issue": issues,
            "related_issue": [{"pr_id": "16", "issue_id": "100", "source": "body"}],
            "pr_reviews": [{"id": 1, "pr_id": "10", "user": "octocat", "state": "COMMENTED",
                            "submitted_at": "2025-06-01", "body": "looks off"}],
            "pr_review_comments": [{"id": 2, "pull_request_review_id": 1, "user": "octocat",
                                    "diff_hunk": "@@ -1,2 +1,3 @@\n a\n+b", "path": "p.py",
                                    "position": 2, "original_position": 2, "commit_id": "c",
                                    "original_commit_id": "c", "body": "why?",
                                    "pull_request_url": "https://api.github.com/repos/a/x/pulls/1",
                                    "created_at": "2025-06-01", "updated_at": None,
                                    "in_reply_to_id": None}]}


def test_build_candidates_filters_and_pins():
    rows, funnel = build_candidates(_tables(), min_requirement_words=10, pinned_pr_ids=[10])
    assert funnel["excluded_language_or_stars"] == 2       # 50 stars, Rust
    assert funnel["excluded_documentation_title"] == 1
    assert funnel["excluded_insufficient_requirement_text"] == 2
    assert [r["pr_id"] for r in rows] == [10, 16]          # pinned first, one per repository
    assert rows[0]["pr_state"] == "merged"
    assert rows[1]["linked_issues"][0]["number"] == 9
    assert "Linked issue #9" in rows[1]["requirement_text"]
    ev = review_evidence(_tables(), rows)
    assert ev[0]["n_human_reviews"] == 1 and ev[0]["n_human_inline_comments"] == 1
    assert ev[1]["inline_comments"] == []


def test_assign_splits_keeps_pins_and_groups_repositories():
    rows = [{"pr_id": i, "repository": f"o/r{i % 7}"} for i in range(20)]
    assign_splits(rows, {0: "evaluation", 1: "development"}, eval_fraction=0.3, seed=1)
    by_repo = {}
    for r in rows:
        by_repo.setdefault(r["repository"], set()).add(r["split"])
    assert all(len(s) == 1 for s in by_repo.values())
    assert rows[0]["split"] == "evaluation" and rows[1]["split"] == "development"
    assert sum(s == {"evaluation"} for s in by_repo.values()) == round(7 * 0.3)


def test_stats_roundtrip():
    rows, _ = build_candidates(_tables(), pinned_pr_ids=[10])
    for r in rows:
        r.update({"split": "development", "changed_test_files": [], "changed_lines": 5, "additions": 4,
                  "deletions": 1, "n_changed_files": 1, "changed_python_files": ["a.py"], "n_commits": 1})
    s = compute_stats(rows, review_evidence(_tables(), rows))
    assert s["prs"] == 2 and s["repository_overlap_between_splits"] == 0
    assert "| PRs | 2 |" in stats_markdown(s)


def test_comment_line_from_diff_hunk():
    assert comment_line("@@ -10,3 +12,4 @@\n a\n-b\n+c\n+d") == 14
    assert comment_line(None) is None
