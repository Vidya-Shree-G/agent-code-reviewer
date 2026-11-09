"""Finding records shared by the static-analysis and test-execution modules."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

SEVERITY_WEIGHT = {"high": 3, "medium": 2, "low": 1}
CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.7, "low": 0.4}


@dataclass
class Finding:
    """One suspected defect, always tied to evidence (a tool result or a test outcome)."""

    tool: str                 # ruff | bandit | py_compile | pytest | import_check
    rule: str                 # tool rule id, e.g. F821, B006, B602, test-regression
    category: str             # correctness | security | error-handling | concurrency | maintainability | test
    severity: str             # high | medium | low
    confidence: str           # high | medium | low
    message: str
    file: str | None = None
    line: int | None = None
    end_line: int | None = None
    cwe: int | None = None
    on_changed_line: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)  # tools that reported the same issue

    @property
    def score(self) -> float:
        return SEVERITY_WEIGHT[self.severity] * CONFIDENCE_WEIGHT[self.confidence] + (
            0.5 if self.on_changed_line else 0.0)

    @property
    def finding_id(self) -> str:
        key = f"{self.tool}|{self.rule}|{self.file}|{self.line}|{self.message}"
        return hashlib.sha1(key.encode()).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["finding_id"] = self.finding_id
        d["score"] = round(self.score, 2)
        return d


# --- rule metadata ----------------------------------------------------------
# (category, severity, confidence) by exact rule, then by prefix.
_RUFF_EXACT: dict[str, tuple[str, str, str]] = {
    "F821": ("correctness", "high", "high"),     # undefined name
    "F822": ("correctness", "high", "high"),     # undefined name in __all__
    "F823": ("correctness", "high", "high"),     # local referenced before assignment
    "F811": ("correctness", "medium", "medium"),  # redefinition of unused name
    "F631": ("correctness", "high", "high"),     # assert on a tuple is always true
    "F632": ("correctness", "medium", "high"),   # `is` with a literal
    "F601": ("correctness", "medium", "medium"),  # repeated dict key
    "F602": ("correctness", "medium", "medium"),
    "F901": ("correctness", "medium", "high"),   # raise NotImplemented
    "F401": ("maintainability", "low", "high"),  # unused import
    "F841": ("maintainability", "low", "medium"),  # unused local variable
    "F541": ("maintainability", "low", "high"),  # f-string without placeholders
    "F403": ("maintainability", "low", "high"),
    "F405": ("maintainability", "low", "medium"),
    "B006": ("correctness", "medium", "high"),   # mutable default argument
    "B008": ("correctness", "low", "medium"),    # call in default argument
    "B011": ("correctness", "medium", "high"),   # assert False
    "B012": ("correctness", "high", "high"),     # return/break in finally swallows exceptions
    "B015": ("correctness", "medium", "medium"),  # pointless comparison
    "B018": ("correctness", "low", "medium"),    # useless expression
    "B019": ("correctness", "medium", "medium"),  # lru_cache on method leaks memory
    "B020": ("correctness", "high", "high"),     # loop variable overrides iterable
    "B023": ("correctness", "medium", "medium"),  # closure captures loop variable
    "B025": ("correctness", "medium", "high"),   # duplicate except
    "B031": ("correctness", "medium", "medium"),  # groupby iterator reused
    "B034": ("correctness", "medium", "high"),   # re.sub positional count/flags
    "B039": ("correctness", "medium", "high"),   # mutable ContextVar default
    "B904": ("error-handling", "low", "medium"),  # raise without from inside except
    "B007": ("maintainability", "low", "medium"),  # unused loop variable
    "B905": ("correctness", "low", "low"),       # zip() without strict
    "B017": ("test", "low", "medium"),           # assertRaises(Exception)
    "B028": ("maintainability", "low", "low"),   # warn() without stacklevel
    "RUF006": ("concurrency", "medium", "medium"),  # dangling asyncio task
    "RUF008": ("correctness", "medium", "high"),  # mutable dataclass default
    "RUF009": ("correctness", "medium", "medium"),  # call in dataclass default
    "RUF012": ("correctness", "low", "medium"),  # mutable class attribute default
    "PLW0120": ("correctness", "low", "medium"),
    "PLW0602": ("maintainability", "low", "medium"),
    "PLW0603": ("maintainability", "low", "low"),
    "PLW1508": ("correctness", "medium", "high"),  # invalid os.getenv default
    "PLW1510": ("correctness", "low", "medium"),  # subprocess.run without check
    "PLW2901": ("correctness", "medium", "medium"),  # loop variable overwritten
    "PLW0127": ("correctness", "medium", "high"),  # self-assignment
    "PLW0128": ("correctness", "medium", "high"),
    "PLW0129": ("correctness", "high", "high"),   # assert on string literal
    "PLW0131": ("correctness", "medium", "high"),
    "PLW0711": ("correctness", "medium", "high"),  # except A or B
    "PLW3301": ("correctness", "low", "medium"),
}
_RUFF_PREFIX: list[tuple[str, tuple[str, str, str]]] = [
    ("E9", ("correctness", "high", "high")),      # syntax / IO errors
    ("F5", ("correctness", "medium", "high")),    # broken %-format / .format calls
    ("F6", ("correctness", "medium", "medium")),
    ("F7", ("correctness", "high", "high")),      # break/continue/return outside loop/function
    ("F", ("maintainability", "low", "medium")),
    ("PLE", ("correctness", "high", "high")),     # pylint errors
    ("PLW", ("maintainability", "low", "low")),
    ("ASYNC", ("concurrency", "medium", "medium")),  # blocking call in async function
    ("B", ("correctness", "low", "medium")),
    ("RUF", ("maintainability", "low", "low")),
]
# Selected so that every rule is a plausible defect, not a style preference.
RUFF_SELECT = ["F", "E9", "B", "PLE", "PLW", "ASYNC", "RUF006", "RUF008", "RUF009", "RUF012"]


def ruff_rule_meta(code: str | None) -> tuple[str, str, str]:
    if not code:  # ruff reports syntax errors without a code
        return ("correctness", "high", "high")
    if code in _RUFF_EXACT:
        return _RUFF_EXACT[code]
    for prefix, meta in _RUFF_PREFIX:
        if code.startswith(prefix):
            return meta
    return ("maintainability", "low", "low")


def bandit_meta(severity: str, confidence: str) -> tuple[str, str, str]:
    return ("security", severity.lower(), confidence.lower())
