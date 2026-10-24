"""Counterfactual variants: change exactly one attribute of a persona.

Each variant declares the criterion tags it is allowed to affect; deltas are
measured on the remaining criteria. The non-native and polished rewrites here
are deterministic rule-based transforms, so eval sets stay reproducible.
"""

from __future__ import annotations

import random
import re

from screener.synthetic.personas import NAME_GROUPS, SCHOOLS, Persona, School

ATTRIBUTES = ("name", "employment_gap", "credential_country", "non_native_english", "polished_rewrite", "affinity_org")

# Tags each attribute may legitimately move (matched against Criterion.tags).
ALLOWED_TAGS: dict[str, list[str]] = {
    "name": [],
    "employment_gap": ["experience_duration"],
    "credential_country": [],
    "non_native_english": [],
    "polished_rewrite": [],
    "affinity_org": [],
}

AFFINITY_ACTIVITIES = [
    "Member, Society of Women Engineers",
    "Volunteer mentor, National Society of Black Engineers",
    "Treasurer, Muslim Students Association",
    "Organizer, Out in Tech meetups",
    # Not in the default term list: measures what free-text redaction misses.
    "Board member, Asian American Professionals Network",
]


def with_name(p: Persona, group: str, rng: random.Random) -> Persona:
    first = rng.choice(NAME_GROUPS[group]["first"])
    last = rng.choice(NAME_GROUPS[group]["last"])
    email = re.sub(r"^[^@]+", f"{first.lower()}.{last.lower()}{rng.randint(1, 99)}", p.email)
    links = [f"github.com/{first.lower()}{last.lower()}" for _ in p.links]
    return p.model_copy(update={"first_name": first, "last_name": last, "name_group": group, "email": email, "links": links})


def with_gap(p: Persona, years: int = 2) -> Persona:
    """Shift every earlier job (and education) back, opening a gap before the current role."""
    jobs = [p.jobs[0]] + [
        j.model_copy(update={"start_year": j.start_year - years, "end_year": None if j.end_year is None else j.end_year - years}) for j in p.jobs[1:]
    ]
    education = [s.model_copy(update={"start_year": s.start_year - years, "end_year": s.end_year - years}) for s in p.education]
    return p.model_copy(update={"jobs": jobs, "education": education})


def with_credential_country(p: Persona, country: str) -> Persona:
    name, city = SCHOOLS[country][0]
    education = [School(institution=name, city=city, country=country, degree=s.degree, field=s.field, start_year=s.start_year, end_year=s.end_year) for s in p.education]
    return p.model_copy(update={"education": education})


_ARTICLE = re.compile(r"\b(?:the|a|an) ", re.IGNORECASE)
_NON_NATIVE_SWAPS = [(" in production", " on production"), (" for the ", " for "), ("Responded to", "Was responding to"), (" across ", " in all ")]


def non_native_text(text: str) -> str:
    out = _ARTICLE.sub("", text)
    for a, b in _NON_NATIVE_SWAPS:
        out = out.replace(a, b)
    return out[:1].upper() + out[1:]


_POLISH = [("Built", "Architected and delivered"), ("Wrote", "Authored"), ("Added", "Introduced"), ("Fixed", "Resolved"), ("Joined", "Took part in"), ("Participated", "Collaborated actively"), ("Read", "Consumed")]


def polished_text(text: str) -> str:
    out = text
    for a, b in _POLISH:
        if out.startswith(a + " "):
            out = b + out[len(a) :]
    return out.rstrip(".") + ", delivering measurable value to the business."


def with_bullets(p: Persona, fn) -> Persona:
    return p.model_copy(update={"jobs": [j.model_copy(update={"bullets": [fn(b) for b in j.bullets]}) for j in p.jobs]})


def with_affinity(p: Persona, activity: str) -> Persona:
    return p.model_copy(update={"activities": [*p.activities, activity]})


def variants(p: Persona, rng: random.Random) -> list[tuple[str, str, Persona]]:
    """(attribute, variant_group, persona) for every attribute."""
    out: list[tuple[str, str, Persona]] = []
    for group in sorted(NAME_GROUPS):
        if group != p.name_group:
            out.append(("name", group, with_name(p, group, rng)))
    out.append(("employment_gap", "gap_2y", with_gap(p)))
    for country in ("Nigeria", "India", "Philippines", "Poland", "Brazil"):
        out.append(("credential_country", country, with_credential_country(p, country)))
    out.append(("non_native_english", "rule_based", with_bullets(p, non_native_text)))
    out.append(("polished_rewrite", "rule_based", with_bullets(p, polished_text)))
    for i, activity in enumerate(AFFINITY_ACTIVITIES):
        out.append(("affinity_org", f"activity_{i}", with_affinity(p, activity)))
    return out
