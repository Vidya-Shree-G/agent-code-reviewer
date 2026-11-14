from pathlib import Path
def build_requirement_input(pr: dict) -> dict:
    issue_texts = []

    for issue in pr.get("linked_issues") or []:
        number = issue.get("number")
        title = (issue.get("title") or "").strip()
        body = (issue.get("body") or "").strip()

        if not title and not body:
            continue

        heading = f"Issue #{number}" if number is not None else "Linked issue"
        issue_texts.append(f"{heading}: {title}\n{body}".strip())

    return {
        "title": pr.get("title") or "",
        "body": pr.get("pr_description") or "",
        "linked_issue": "\n\n".join(issue_texts),
    }
    
def _read_source(root: Path, relative_path: str) -> str:
    root = root.resolve()
    source = (root / relative_path).resolve()

    if not source.is_relative_to(root):
        raise ValueError(f"File is outside the repository: {relative_path}")

    return source.read_text(encoding="utf-8")


def build_changed_files(pr: dict, base_dir: Path, head_dir: Path) -> list[dict]:
    files = []

    for change in pr["changed_files"]:
        path = change["path"]
        old_path = change.get("previous_path") or path
        status = change["status"]

        if not (path.endswith(".py") or old_path.endswith(".py")):
            continue

        before = "" if status == "added" else _read_source(base_dir, old_path)
        after = "" if status == "removed" else _read_source(head_dir, path)

        files.append({"path": path, "before": before, "after": after})

    return files

def build_requirement_review_input(
    pr: dict, base_dir: Path, head_dir: Path
) -> dict:
    review_input = build_requirement_input(pr)
    review_input["files"] = build_changed_files(pr, base_dir, head_dir)
    return review_input
