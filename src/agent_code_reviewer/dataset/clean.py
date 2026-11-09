"""Text cleaning for agent-authored PR descriptions and linked issues.

Agent PR bodies mix three kinds of text:

* the requirement the reviewer should check against (what the change is for),
* the agent's own testing log (for example Codex's ``## Testing`` section), and
* boilerplate added by the agent platform or the repository's PR template
  (session links, tips banners, mermaid diagrams, HTML comments).

``clean_pr_body`` separates the first two and drops the third. The rules are
deliberately conservative: they only remove text that was observed verbatim in
AIDev-pop and that carries no information about the requested behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- boilerplate patterns -------------------------------------------------

# Everything after this marker is the Copilot coding agent tips banner.
_COPILOT_TIPS = re.compile(r"<!--\s*START COPILOT CODING AGENT TIPS\s*-->.*\Z", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_UNCLOSED_HTML_COMMENT = re.compile(r"<!--.*\Z", re.S)
_MERMAID = re.compile(r"```mermaid\b.*?```", re.S)
# Ellipsis bot summary appended to some PRs ("> [!IMPORTANT] ... <sup>...Ellipsis...</sup>").
_ELLIPSIS = re.compile(r"^>\s*\[!IMPORTANT\].*?(?:Ellipsis|ellipsis).*?(?:</sup>|$)", re.S | re.M)
_DETAILS = re.compile(r"<details>.*?</details>", re.S)

# Whole lines that are platform boilerplate.
_BOILERPLATE_LINES = [
    re.compile(r"^\s*https?://chatgpt\.com/codex/tasks/\S+\s*$"),
    re.compile(r"^\s*(?:Link to Devin run|Requested by)\s*:.*$", re.I),
    re.compile(r"^\s*#+\s*Link to Devin run\s*$", re.I),
    re.compile(r"^\s*https?://app\.devin\.ai/\S+\s*$"),
    re.compile(r"^\s*Thanks for assigning this issue to me\..*$"),
    re.compile(r"^\s*💡 You can make Copilot smarter.*$"),
    re.compile(r"^\s*💬 Share your feedback on Copilot.*$"),
    re.compile(r"^\s*🤖 Generated with \[Claude Code\].*$"),
    re.compile(r"^\s*Co-?authored-?by:.*$", re.I),
    re.compile(r"^.*cursor\.com/background-agent.*$"),
    re.compile(r"^\s*<a href=\"https://cursor\.com.*$"),
    re.compile(r"^\s*Original issue description:\s*$", re.I),
]
_RULE_LINE = re.compile(r"^\s*(?:-{3,}|_{3,}|\*{3,})\s*$")
_HEADING = re.compile(r"^(#{1,6})\s*(.+?)\s*#*\s*$")
_TESTING_HEADING = re.compile(
    r"^(?:testing|tests?|test plan|testing done|tests? run|how to test|how was this tested\??|"
    r"verification|validation)\b",
    re.I,
)
# Codex marks failed commands as "*(fails: ...)*"; other agents write "failed"/"error".
_FAIL_MARK = re.compile(r"\((?:fails?|failed|error)\b[^)]*\)|\bfail(?:s|ed|ing)\b", re.I)
_WORD = re.compile(r"[A-Za-z0-9_]+")


@dataclass
class CleanedBody:
    description: str
    agent_reported_testing: str
    agent_reported_test_failure: bool
    removed: list[str] = field(default_factory=list)


def normalize(text: str | None) -> str:
    """Normalise newlines and whitespace without touching content."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_boilerplate(text: str) -> tuple[str, list[str]]:
    """Remove platform boilerplate; return the text and the names of rules that fired."""
    removed: list[str] = []
    for name, pattern in (
        ("copilot_tips", _COPILOT_TIPS),
        ("mermaid", _MERMAID),
        ("ellipsis_summary", _ELLIPSIS),
        ("html_comment", _HTML_COMMENT),
        ("html_comment", _UNCLOSED_HTML_COMMENT),
    ):
        text, n = pattern.subn("", text)
        if n and name not in removed:
            removed.append(name)
    kept = []
    for line in text.split("\n"):
        if any(p.match(line) for p in _BOILERPLATE_LINES):
            if "boilerplate_line" not in removed:
                removed.append("boilerplate_line")
            continue
        kept.append(line)
    # Drop horizontal rules left dangling at the end (Codex "------" before the task link).
    while kept and (not kept[-1].strip() or _RULE_LINE.match(kept[-1])):
        kept.pop()
    # Drop headings that lost all their content.
    out: list[str] = []
    for i, line in enumerate(kept):
        if _HEADING.match(line):
            rest = [ln for ln in kept[i + 1:] if ln.strip()]
            if not rest or _HEADING.match(rest[0]) and len(_HEADING.match(rest[0]).group(1)) <= len(
                _HEADING.match(line).group(1)
            ):
                continue
        out.append(line)
    return normalize("\n".join(out)), removed


def split_testing_section(text: str) -> tuple[str, str]:
    """Move markdown sections titled like "Testing" out of the description."""
    lines = text.split("\n")
    desc: list[str] = []
    testing: list[str] = []
    level = 0  # heading level of the testing section we are inside, 0 = not inside
    for line in lines:
        m = _HEADING.match(line)
        if m:
            this_level = len(m.group(1))
            if level and this_level <= level:
                level = 0
            if not level and _TESTING_HEADING.match(m.group(2).strip("*_ :")):
                level = this_level
                continue
        (testing if level else desc).append(line)
    return normalize("\n".join(desc)), normalize("\n".join(testing))


def clean_pr_body(body: str | None) -> CleanedBody:
    text, removed = strip_boilerplate(normalize(body))
    desc, testing = split_testing_section(text)
    if testing:
        removed.append("testing_section_split")
    return CleanedBody(
        description=desc,
        agent_reported_testing=testing,
        agent_reported_test_failure=bool(testing and _FAIL_MARK.search(testing)),
        removed=removed,
    )


def clean_issue_body(body: str | None) -> str:
    return strip_boilerplate(normalize(body))[0]


def word_count(text: str) -> int:
    return len(_WORD.findall(text or ""))


_DOC_TITLE = re.compile(r"^(?:docs?|documentation)(?:\(|:|\s|!)", re.I)


def is_documentation_title(title: str) -> bool:
    return bool(_DOC_TITLE.match((title or "").strip()))
