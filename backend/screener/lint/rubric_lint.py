"""Rubric lint: flags proxy-risk language for human review.

Scans every rubric text field (name, description, each level's description and
example). It never blocks saving. Each finding needs a recorded acknowledgment
before a config using the rubric can be promoted.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from screener.config import Rubric

RULES: dict[str, tuple[str, list[str]]] = {
    "min_tenure": ("Minimum tenure or years-of-experience thresholds can act as an age proxy (ADEA).", [r"\b\d+\s*\+?\s*(?:years?|yrs?)\b", r"\bat least \w+ years?\b", r"\bminimum (?:of )?\w+ years?\b", r"\byears of experience\b", r"\btenure\b"]),
    "prestige": ("Employer or school prestige correlates with socioeconomic background and race.", [r"\btop[- ]tier\b", r"\bivy\b", r"\bprestigious\b", r"\belite\b", r"\bFAANG\b", r"\btier[- ]?1\b", r"\btop \d+ (?:school|university|program|company)\b", r"\bwell[- ]known (?:company|employer|brand)\b"]),
    "employment_gap": ("Penalizing employment gaps disproportionately affects caregivers, people with disabilities, and veterans.", [r"\bgaps?\b", r"\bcontinuous(?:ly)? employ", r"\bunemploy"]),
    "location": ("Location requirements can proxy for national origin or race unless the job needs them.", [r"\blocal (?:candidates?|applicants?)\b", r"\blives? (?:in|near)\b", r"\bwithin \d+ miles\b", r"\bnative (?:english|speaker)\b", r"\bcitizens?(?:hip)?\b"]),
    "culture_fit": ("\"Culture fit\" is undefined and invites affinity bias; describe the behaviour instead.", [r"\bcultur(?:e|al) fit\b", r"\bfits? in\b", r"\bpersonality\b"]),
    "age_correlated": ("Age-correlated phrasing.", [r"\bdigital native\b", r"\byoung\b", r"\brecent (?:graduate|grad)s?\b", r"\benergetic\b", r"\bgraduated (?:within|in the last)\b", r"\boverqualified\b"]),
}


class LintFinding(BaseModel):
    criterion: str
    field: str
    rule: str
    match: str
    explanation: str
    acknowledged: bool = False


def _fields(rubric: Rubric):
    for c in rubric.criteria:
        yield c.id, "name", c.name
        yield c.id, "description", c.description
        for level, lv in sorted(c.levels.items()):
            yield c.id, f"levels.{level}.description", lv.description
            yield c.id, f"levels.{level}.example", lv.example


def lint_rubric(rubric: Rubric) -> list[LintFinding]:
    acks = {(a.criterion, a.rule, a.field) for a in rubric.lint_acknowledgments}
    findings: list[LintFinding] = []
    for criterion, field, text in _fields(rubric):
        for rule, (explanation, patterns) in RULES.items():
            for pattern in patterns:
                m = re.search(pattern, text, re.IGNORECASE)
                if m:
                    findings.append(LintFinding(criterion=criterion, field=field, rule=rule, match=m.group(0), explanation=explanation, acknowledged=(criterion, rule, field) in acks))
                    break
    return findings


def unacknowledged(rubric: Rubric) -> list[LintFinding]:
    return [f for f in lint_rubric(rubric) if not f.acknowledged]
