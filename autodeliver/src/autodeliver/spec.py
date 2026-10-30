"""The committed spec of the agentic-SDLC convention: `docs/specs/<work-item-id>.md`.

One spec per work item, written from `docs/specs/TEMPLATE.md` and reviewed in a pull request, so unlike the
live ticket it is a reviewed record. Headings that autodeliver reads:

  Goal · Existing acceptance criteria (the confirmed regression baseline, with file:line) ·
  Acceptance criteria (the additional criteria, numbered Given/When/Then) · Use cases · Edge cases ·
  Non-goals · Security criteria · Open questions · plus a `Risk tier` line in the header.

What it changes in a run:
- criteria come from the spec when it has any; the existing/additional split it declares is a third vote
  next to the planner and the independent checker (a mismatch escalates);
- Non-goals are scope limits for every agent;
- unanswered Open questions and a risk tier above `spec.max_risk_tier` stop the run before any agent runs.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

RISK_RANK = {"low": 1, "medium": 2, "high": 3}
MAX_TEXT = 20_000

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_HEADING = re.compile(r"^##[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
_BULLET = re.compile(r"^[ \t]*(?:[-*•]|\d+[.)])[ \t]+(.*)$")
_RISK = re.compile(r"\*\*Risk tier:?\*\*:?[ \t]*(low|medium|high)\b(?![ \t]*\|)", re.I)
_TITLE = re.compile(r"^#[ \t]+(?:\d+[ \t]*[:\-–][ \t]*)?(.+?)[ \t]*$", re.M)
# Template and empty-section leftovers that are not real content, including the guidance sentences of
# templates/repo/docs/specs/TEMPLATE.md (an unfilled template must not yield criteria or open questions).
_PLACEHOLDER = re.compile(r"^(?:…|\.{3}|-|–|tbd\??|n/?a|none(?:\s*(?:stated|yet|[:\-–—.(].*))?\.?|<[^>]*>|given <context>.*"
                          r"|from the work item's security criteria field.*|questions for the product owner.*|link to .ai-ready. wiki pages.*)$",
                          re.I)

CANONICAL = {
    "goal": "goal",
    "existing acceptance criteria": "existing",
    "acceptance criteria": "additional",
    "use cases": "use_cases",
    "edge cases": "edge_cases",
    "non-goals": "non_goals",
    "non goals": "non_goals",
    "security criteria": "security",
    "business rules involved": "business_rules",
    "related adrs": "adrs",
    "open questions": "open_questions",
    "design": "design",
}


@dataclass
class Spec:
    path: str
    text: str
    title: str = ""
    risk_tier: str | None = None                                   # "Low" | "Medium" | "High" | None (not filled in)
    sections: dict[str, str] = field(default_factory=dict)
    existing: list[str] = field(default_factory=list)              # confirmed regression baseline
    additional: list[str] = field(default_factory=list)            # what this change adds
    security: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("sections")
        return d


def is_placeholder(item: str) -> bool:
    return bool(_PLACEHOLDER.match(item.strip()))


def items(block: str) -> list[str]:
    """Bullets or numbered lines, one item each (indented continuation lines join the previous item);
    without bullets each non-empty line is an item. Placeholders and template leftovers are dropped."""
    lines = [l.rstrip() for l in block.splitlines() if l.strip()]
    out: list[str] = []
    if any(_BULLET.match(l) for l in lines):
        for l in lines:
            b = _BULLET.match(l)
            if b:
                out.append(b.group(1).strip())
            elif out:
                out[-1] += " " + l.strip()
    else:
        out = [l.strip() for l in lines]
    return [i for i in out if i and not is_placeholder(i)]


def split_sections(text: str) -> dict[str, str]:
    marks = list(_HEADING.finditer(text))
    sections: dict[str, str] = {}
    for n, m in enumerate(marks):
        key = CANONICAL.get(re.sub(r"[^a-z\- ]", "", m.group(1).lower()).strip())
        if key:
            end = marks[n + 1].start() if n + 1 < len(marks) else len(text)
            sections[key] = text[m.end():end].strip()
    return sections


def parse_spec(raw: str, path: str) -> Spec:
    text = _COMMENT.sub("", raw.replace("\r\n", "\n")).strip()
    sections = split_sections(text)
    title = _TITLE.search(text)
    risk = _RISK.search(text)
    return Spec(
        path=path, text=text[:MAX_TEXT], title=title.group(1) if title else "",
        risk_tier=risk.group(1).capitalize() if risk else None, sections=sections,
        existing=items(sections.get("existing", "")), additional=items(sections.get("additional", "")),
        security=items(sections.get("security", "")), open_questions=items(sections.get("open_questions", "")))


def problems(spec: Spec, max_risk_tier: str, block_on_open_questions: bool) -> list[str]:
    """Reasons the run must not start unattended (the same stops the sdlc skill makes for a human)."""
    out: list[str] = []
    if block_on_open_questions:
        out += [f"open question in {spec.path}: {q}" for q in spec.open_questions]
    if spec.risk_tier is None:
        out.append(f"{spec.path}: the Risk tier is missing or not filled in (Low, Medium or High)")
    elif RISK_RANK[spec.risk_tier.lower()] > RISK_RANK[max_risk_tier.lower()]:
        out.append(f"{spec.path}: risk tier {spec.risk_tier} is above spec.max_risk_tier ({max_risk_tier}); "
                   "money, personal data, security or irreversible operations need a human")
    if not spec.additional:
        out.append(f"{spec.path}: no additional acceptance criteria, so there is nothing to build")
    return out


def is_pointer(text: str, spec_path: str) -> bool:
    """A tracker field that only says 'See docs/specs/42.md.' is a pointer, not a criterion."""
    return spec_path.lower() in text.lower() or bool(re.search(r"\bspecs?/\d+\.md\b", text, re.I))
