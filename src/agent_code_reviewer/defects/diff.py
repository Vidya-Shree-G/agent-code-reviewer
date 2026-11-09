"""Unified-diff parsing: which files and lines a PR touched."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def is_python_path(path: str) -> bool:
    return path.lower().endswith((".py", ".pyi"))


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        any(part in ("test", "tests", "testing") for part in p.parts[:-1])
        or p.name.startswith("test_")
        or p.name.endswith("_test.py")
        or p.name == "conftest.py"
    )


@dataclass
class FileDiff:
    old_path: str | None
    new_path: str | None
    added_lines: set[int] = field(default_factory=set)    # line numbers in the new file
    removed_lines: set[int] = field(default_factory=set)  # line numbers in the old file
    binary: bool = False
    text: str = ""                                         # this file's section of the diff

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def status(self) -> str:
        if self.old_path is None:
            return "added"
        if self.new_path is None:
            return "removed"
        return "renamed" if self.old_path != self.new_path else "modified"


def _strip_prefix(path: str) -> str | None:
    path = path.strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    if path == "/dev/null":
        return None
    return path[2:] if path[:2] in ("a/", "b/") else path


def parse_diff(text: str) -> list[FileDiff]:
    """Parse a git unified diff (as served by GitHub's .diff endpoint)."""
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    buf: list[str] = []
    old_ln = new_ln = 0
    in_hunk = False

    def flush() -> None:
        if cur is not None:
            cur.text = "".join(buf)
            files.append(cur)

    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            flush()
            buf = []
            m = re.match(r'diff --git (?:"?a/(.+?)"?) (?:"?b/(.+?)"?)\s*$', line)
            a, b = (m.group(1), m.group(2)) if m else (None, None)
            cur = FileDiff(old_path=a, new_path=b)
            in_hunk = False
        elif cur is None:
            continue
        elif line.startswith("@@"):
            m = _HUNK.match(line)
            if m:
                old_ln, new_ln = int(m.group(1)), int(m.group(3))
                in_hunk = True
        elif not in_hunk:
            if line.startswith("--- "):
                cur.old_path = _strip_prefix(line[4:])
            elif line.startswith("+++ "):
                cur.new_path = _strip_prefix(line[4:])
            elif line.startswith("new file mode"):
                cur.old_path = None
            elif line.startswith("deleted file mode"):
                cur.new_path = None
            elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                cur.binary = True
        elif line.startswith("+"):
            cur.added_lines.add(new_ln)
            new_ln += 1
        elif line.startswith("-"):
            cur.removed_lines.add(old_ln)
            old_ln += 1
        elif line.startswith(" "):
            old_ln += 1
            new_ln += 1
        if cur is not None:
            buf.append(line)
    flush()
    return files


def text_only_diff(files: list[FileDiff]) -> str:
    """Re-assemble the diff without binary sections (git apply cannot use those from GitHub)."""
    return "".join(f.text for f in files if not f.binary)
