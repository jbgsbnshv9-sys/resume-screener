# Resume screener

An internal tool that screens resumes against a locked, job-specific rubric. It produces evidence-backed criterion scores and a suggested tier, then routes every candidate to a human reviewer, who makes the decision. Every stage is stored as a trace, and a config can only go live after it passes an evaluation and bias suite.

`SPEC.md` is the scope; `CLAUDE.md` holds the principles the code enforces.

## Quick start

```bash
docker compose up --build          # Postgres + API + UI on http://localhost:8000
docker compose exec backend screener demo   # seed synthetic data; prints API tokens per role
```

Sign in with a printed token. `demo` registers the offline config, runs the full eval suite on it, promotes it through the gate, creates a requisition, and uploads synthetic resumes. Those resumes include hidden text, a visible injection, an image-only scan, a DOCX, and a candidate who opted out of AI screening.

Without Docker (from `backend/`):

```bash
uv venv .venv && uv pip install -e ".[dev]"
.venv/bin/pytest -q                                   # offline suite (SQLite)
TEST_DATABASE_URL=postgresql+psycopg://... .venv/bin/pytest -q   # same suite on Postgres, incl. audit trigger
.venv/bin/screener run ../fixtures/samples/clean_strong.pdf --config ../configs/pipelines/backend-engineer-sim.yaml
.venv/bin/screener eval --config ../configs/pipelines/backend-engineer-sim.yaml \
    --eval-set ../fixtures/evalsets/backend-engineer-v1.jsonl --out report.json
.venv/bin/screener diff before.json after.json
```

Frontend dev server: `cd frontend && npm install && npm run dev` (proxies `/api` to :8000).

## Real models

`configs/pipelines/backend-engineer-claude.yaml` uses `claude-haiku-5-5` for parsing and `claude-sonnet-5-5` for scoring. Set `ANTHROPIC_API_KEY`, and either `SCREENER_LIVE_LLM=1` (server) or `--live` (CLI). `pytest --live` runs the live tests. Record responses for deterministic replay with `--cassette file.json --record`, and replay them with `--cassette file.json`.

Configs whose model starts with `simulated-` use a deterministic offline stand-in (`backend/screener/simulated.py`) so the whole system runs and tests without network access. It is crude by design, and its eval numbers describe the plumbing, not screening quality. Two deliberately bad variants prove the suite catches problems: `simulated-naive` obeys injected instructions, and `simulated-biased` marks down certain surnames.

## How the pipeline works

| Node | What it does |
|---|---|
| intake | PyMuPDF extraction with per-span style and position. Renders each page and samples the pixels under each span to flag text a person wouldn't see: low contrast, tiny, off-page, transparent, covered, or in metadata. DOCX is converted with LibreOffice and its runs are scanned for `w:vanish`/white/tiny text. Image-only documents go to manual review. |
| parse | The model quotes values verbatim; code finds their spans. Unresolved values trigger a retry, then a flag. |
| redact | Removes name, contact, address and links, plus optional school names and dates. A free-text pass neutralizes pronouns and redacts term lists. An offset map maps redacted text back to the original document. The redaction map is stored separately. |
| score | One call per criterion per sample (default 3). The resume goes in the user turn, never the system prompt, and is cached across criterion calls. Every quote is verified in code. Level 0 needs no quote. Level ≥2 needs evidence from an experience entry; skills-list-only evidence is capped at 1. The lower median of valid samples is used, and wide spread is flagged. |
| aggregate | Weighted score in code; a must-have at level 0 caps the tier at `review`. Any flag, or samples that straddle a tier boundary, marks the candidate "needs attention". |

`screener recompute` re-derives verification, medians and the tier from a stored trace without calling a model.

There is no `temperature` anywhere: API version 2026-10-21 rejects sampling parameters, so `effort` is the tunable.

## Guarantees enforced in code

- No automatic rejection. A decision needs a reviewer who fetched the document (that fetch is the recorded `opened` event) and a reason. There is no bulk endpoint. SLA and stale-hold alerts surface unopened candidates.
- Candidates who opt out are never run through the pipeline, and earlier AI output for them is hidden.
- Optional blind first look hides tiers until the reviewer has read the document.
- Requisitions pin the active config at their first scored candidate. Moving one re-scores every open candidate.
- A config's hash covers its YAML, the rubric, the prompts and the term lists. Promotion needs a passing eval on that exact hash, or an admin override with a recorded justification, and all rubric-lint findings acknowledged. A config whose rubric or prompts changed on disk is refused.
- The gate applies thresholds to the conservative end of each 95% interval. Name and affinity counterfactuals also run with redaction off, so a model that is biased underneath can't pass on redaction alone.
- The audit log is append-only: the ORM refuses changes, and a Postgres trigger blocks UPDATE, DELETE and TRUNCATE.

## Known limitations

- Labels in the committed eval set are a single `generator-intended` label. Add at least two independent human labelers before trusting agreement metrics; the gate flags this.
- The non-native-English and polished rewrites in the counterfactual generator are rule-based, not model-written.
- Partially covered text (half under a shape) isn't detected; fully hidden text is.
- Encryption at rest relies on the database and volume. Eval runs execute as in-process background tasks; a durable Postgres-backed job queue is still to do.
- Not built yet (deferred in `SPEC.md`): LLM rubric drafting, a sandbox UI beyond config/eval pages, PDF audit export.
- Retention periods in `configs/retention.yaml` are placeholders to set with counsel.
