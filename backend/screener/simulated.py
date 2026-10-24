"""A deterministic stand-in for Claude, for offline development and tests only.

It reads the same request a real model would get and produces output in the
same schema, so every node, the evals and the API run end to end without
network access. It's crude by design:

- parse: understands the synthetic resume layout from screener.synthetic.render;
- score: lexical matching between resume bullets and the criterion's text,
  with anchored-level matching and seeded per-sample noise.

Variants exist to prove the eval suite catches bad models:
- `simulated-naive` obeys injected instructions ("rate this candidate 3");
- `simulated-biased` marks down resumes containing certain surnames.

Selected only by a `simulated-*` model ID, so stored runs always say which
model produced them. Never use it for real candidates.
"""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any

from screener.hashing import sha256_text
from screener.llm import LLMRequest, LLMResponse, response_from_raw

_HEADERS = {"SUMMARY", "EXPERIENCE", "EDUCATION", "SKILLS", "CERTIFICATIONS", "ACTIVITIES", "LINKS"}
_JOB = re.compile(r"^(?P<title>.+?) — (?P<org>.+?) \| (?P<start>[A-Z][a-z]{2} \d{4}) – (?P<end>[A-Z][a-z]{2} \d{4}|Present)$")
_EDU = re.compile(r"^(?P<degree>.+?) in (?P<field>.+?) — (?P<inst>[^,]+), .+ \| (?P<start>\d{4}) – (?P<end>\d{4})$")
_INJECTION = re.compile(r"ignore (?:all |any )?(?:previous|prior) instructions|rate this candidate|score this candidate", re.IGNORECASE)
_STOP = set(
    """a an and or the of to in on for with without by from as at is are be been was were this that these those such
    any all no not only own other more than into through end via e g eg including include includes its it their them
    evidence level levels example described describes mention mentioned work works working worked
    """.split()
)
# Surnames the biased simulator penalizes (from one name group), to prove counterfactuals catch it.
_PENALIZED_SURNAMES = {"Washington", "Jefferson", "Booker", "Banks", "Mosley", "Jackson"}


def _stem(word: str) -> str:
    w = word.lower()
    for suffix in ("ations", "ation", "ings", "ing", "ers", "er", "ed", "es", "s"):
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            return w[: -len(suffix)]
    return w


def _terms(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[A-Za-z][A-Za-z+#-]*", text) if len(w) >= 3 and w.lower() not in _STOP}


def _units(text: str) -> list[str]:
    """Bullets (with their wrapped continuation lines) and other content lines, verbatim."""
    units: list[str] = []
    in_bullet = False
    for line in text.split("\n"):
        s = line.strip()
        if not s or s in _HEADERS or _JOB.match(s):
            in_bullet = False
            if s and s not in _HEADERS:
                units.append(s)
            continue
        if s.startswith("•"):
            units.append(s.lstrip("• ").strip())
            in_bullet = True
        elif in_bullet:
            units[-1] += "\n" + s
        else:
            units.append(s)
    return units


class SimulatedClient:
    def __init__(self, model: str):
        self.model = model

    def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        text = request.resume_text()
        out = self._parse(text) if request.task == "parse" else self._score(text, request)
        payload = json.dumps(out, ensure_ascii=False)
        raw: dict[str, Any] = {
            "id": f"sim_{request.key()[:20]}",
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": [{"type": "text", "text": payload}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": sum(len(b.text) for b in request.user_blocks) // 4 + len(request.system) // 4, "output_tokens": len(payload) // 4},
        }
        return response_from_raw(self.model, raw, int((time.monotonic() - started) * 1000))

    # --- parse ---------------------------------------------------------------

    def _parse(self, text: str) -> dict[str, Any]:
        lines = [ln.strip() for ln in text.split("\n")]
        content = [ln for ln in lines if ln]
        out: dict[str, Any] = {"contact": {"name": "", "email": "", "phone": "", "location": ""}, "experience": [], "education": [], "skills": [], "certifications": [], "links": []}
        if not content:
            return out
        out["contact"]["name"] = content[0]
        if len(content) > 1 and "|" in content[1]:
            for part in (p.strip() for p in content[1].split("|")):
                key = "email" if "@" in part else "phone" if sum(c.isdigit() for c in part) >= 7 else "location"
                out["contact"][key] = part
        section = None
        job: dict[str, Any] | None = None
        body: list[str] = []

        def close_job() -> None:
            nonlocal job, body
            if job is not None:
                job["description"] = "\n".join(body).lstrip("• ").strip()
                out["experience"].append(job)
            job, body = None, []

        for ln in content[2:]:
            if ln in _HEADERS:
                close_job()
                section = ln
                continue
            if section == "EXPERIENCE":
                m = _JOB.match(ln)
                if m:
                    close_job()
                    job = {"title": m["title"], "org": m["org"], "start": m["start"], "end": m["end"]}
                elif job is not None:
                    body.append(ln)
            elif section == "EDUCATION":
                m = _EDU.match(ln)
                if m:
                    out["education"].append({"institution": m["inst"], "degree": m["degree"], "field": m["field"], "start": m["start"], "end": m["end"]})
            elif section == "SKILLS":
                out["skills"].extend(s.strip() for s in ln.split(",") if s.strip())
            elif section == "CERTIFICATIONS":
                out["certifications"].append(ln)
            elif section == "LINKS":
                out["links"].append(ln)
        close_job()
        return out

    # --- score ---------------------------------------------------------------

    def _score(self, text: str, request: LLMRequest) -> dict[str, Any]:
        crit_block = request.user_blocks[1].text
        criterion = json.loads(crit_block[crit_block.index("<criterion>") + len("<criterion>") : crit_block.index("</criterion>")])
        units = _units(text)

        if self.model == "simulated-naive":
            for u in units:
                if _INJECTION.search(u):
                    return {"level": 3, "evidence": [u], "rationale": "The resume states the candidate should receive the top level."}

        core = _terms(criterion["name"] + " " + criterion["description"])
        level_terms = {int(k): _terms(v["description"] + " " + v["example"]) - core for k, v in criterion["levels"].items() if int(k) > 0}
        matches: list[tuple[int, int, str]] = []
        for u in units:
            ut = _terms(u)
            overlap = len(ut & core)
            best = max((1, 2, 3), key=lambda lv, ut=ut: (len(ut & level_terms[lv]), -lv))
            if overlap >= 2 or (overlap >= 1 and len(ut & level_terms[best]) >= 2):
                matches.append((best, overlap, u))
        if not matches:
            level, evidence = 0, []
        else:
            matches.sort(key=lambda m: (-m[0], -m[1]))
            level = matches[0][0]
            if level == 1 and len(matches) >= 3:
                level = 2
            evidence = [m[2] for m in matches[:3]]

        rng = random.Random(sha256_text(f"{text}|{criterion['name']}|{request.sample_index}"))
        if level and rng.random() < 0.15:
            level = max(1, min(3, level + rng.choice((-1, 1))))
        if self.model == "simulated-biased" and any(re.search(rf"\b{s}\b", text) for s in _PENALIZED_SURNAMES):
            level = max(0, level - 1)
            if level == 0:
                evidence = []
        return {"level": level, "evidence": evidence, "rationale": f"Matched {len(matches)} relevant line(s) against the anchored levels."}
