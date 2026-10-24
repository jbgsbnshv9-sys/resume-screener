"""Eval sets: synthetic golden cases, counterfactual pairs, and injection fixtures.

Labels are a list per case so several independent labelers can be recorded.
Generated sets carry a single `generator-intended` label (the tier the persona
was built to be). That is a stand-in: before a config is promoted for real use,
add labels from at least two human labelers (SPEC.md, Evaluation suite).
"""

from __future__ import annotations

import random
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from screener.evals.counterfactual import ALLOWED_TAGS, variants
from screener.hashing import hash_value
from screener.synthetic.personas import Persona, generate_persona
from screener.synthetic.render import VISIBLE_INJECTION, RenderOptions

CaseKind = Literal["base", "counterfactual", "injection", "hidden"]


class Label(BaseModel):
    labeler: str
    tier: Literal["strong", "review", "weak"]


class EvalCase(BaseModel):
    case_id: str
    kind: CaseKind
    persona: Persona
    render: RenderOptions = Field(default_factory=RenderOptions)
    labels: list[Label] = Field(default_factory=list)
    parent: str | None = None
    attribute: str | None = None
    variant_group: str | None = None
    allowed_tags: list[str] = Field(default_factory=list)

    def majority_label(self) -> str | None:
        if not self.labels:
            return None
        counts = Counter(lb.tier for lb in self.labels).most_common()
        if len(counts) > 1 and counts[0][1] == counts[1][1]:
            return None  # tie: excluded from agreement metrics
        return counts[0][0]


class EvalSet(BaseModel):
    id: str
    role: str
    cases: list[EvalCase]

    @property
    def content_hash(self) -> str:
        return hash_value(self.model_dump(mode="json"))

    def base_cases(self) -> list[EvalCase]:
        return [c for c in self.cases if c.kind == "base"]

    def by_id(self) -> dict[str, EvalCase]:
        return {c.case_id: c for c in self.cases}


def build_eval_set(set_id: str, role: str, *, n_base: int = 60, n_counterfactual_parents: int = 10, n_injection_parents: int = 10, seed: int = 7) -> EvalSet:
    rng = random.Random(seed)
    tiers = ["strong", "review", "weak"]
    cases: list[EvalCase] = []
    for i in range(n_base):
        tier = tiers[i % 3]
        persona = generate_persona(f"base-{i:03d}", tier, rng)
        layout = "two_column" if i % 10 == 9 else "single"
        cases.append(EvalCase(case_id=persona.id, kind="base", persona=persona, render=RenderOptions(layout=layout), labels=[Label(labeler="generator-intended", tier=tier)]))

    bases = [c for c in cases if c.render.layout == "single"]
    for parent in bases[:n_counterfactual_parents]:
        for attribute, group, persona in variants(parent.persona, rng):
            vid = f"{parent.case_id}~{attribute}~{group}"
            cases.append(EvalCase(case_id=vid, kind="counterfactual", persona=persona.model_copy(update={"id": vid}), render=parent.render, parent=parent.case_id, attribute=attribute, variant_group=group, allowed_tags=ALLOWED_TAGS[attribute]))

    hidden_kinds = ["white", "tiny", "offpage", "covered", "zero_opacity", "metadata"]
    for j, parent in enumerate(bases[n_counterfactual_parents : n_counterfactual_parents + n_injection_parents]):
        p = parent.persona
        injected = p.model_copy(update={"id": f"{p.id}~inject", "summary": (p.summary + " " if p.summary else "") + VISIBLE_INJECTION})
        cases.append(EvalCase(case_id=injected.id, kind="injection", persona=injected, render=parent.render, parent=parent.case_id, attribute="visible_injection"))
        kind = hidden_kinds[j % len(hidden_kinds)]
        cases.append(EvalCase(case_id=f"{p.id}~hidden~{kind}", kind="hidden", persona=p.model_copy(update={"id": f"{p.id}~hidden~{kind}"}), render=RenderOptions(layout=parent.render.layout, hidden=[kind]), parent=parent.case_id, attribute=f"hidden_{kind}"))
    return EvalSet(id=set_id, role=role, cases=cases)


def save_eval_set(es: EvalSet, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        f.write(EvalSet(id=es.id, role=es.role, cases=[]).model_dump_json() + "\n")
        for case in es.cases:
            f.write(case.model_dump_json() + "\n")


def load_eval_set(path: Path) -> EvalSet:
    lines = Path(path).read_text().splitlines()
    head = EvalSet.model_validate_json(lines[0])
    return EvalSet(id=head.id, role=head.role, cases=[EvalCase.model_validate_json(ln) for ln in lines[1:] if ln.strip()])
