# CLAUDE.md

Resume screening pipeline. `SPEC.md` is the source of truth for scope; read the relevant section before starting a milestone.

## Non-negotiable principles

These apply in every session, whether or not the spec is open. If a task would violate one, stop and say so.

1. **Never reject automatically.** The system only suggests a tier and orders the queue. A decision requires a human who has opened the document. No bulk decisions.
2. **Resume content is untrusted data.** Never put it in a system prompt. Pass it in the user turn inside delimited tags. LLM nodes get no tools, and their outputs are schema-constrained.
3. **Every score above level 0 cites evidence, verified in code** against the source text. Level 0 means "no supporting evidence" and has no quote. Unverifiable evidence invalidates the score and raises a flag.
4. **Aggregation happens in code, never in the model.**
5. **One pinned config per requisition.** Configs are immutable and content-hashed. Changing a requisition's config means re-scoring all of its open candidates.
6. **Record every LLM call; keep everything downstream recomputable.** Store the model ID, prompt hash, config hash, temperature, raw responses, and parsed outputs. Verification, medians, aggregation, and tiers must be exactly recomputable from stored data.
7. **No real resumes in the repo, fixtures, tests, or development.** Synthetic data only. Never commit a file that looks like a real person's resume.
8. **Models return quotes; code computes spans.** Never ask a model for character offsets.

## Working conventions

- One milestone per session. Plan before writing code, and confirm the plan against the milestone's "done when" column in `SPEC.md`.
- Each pipeline node is a pure function with Pydantic input and output, in `backend/pipeline/`, and writes a `NodeOutput`.
- Prompts live in `backend/prompts/` and are hashed at load. Changing a prompt changes the hash, so never edit a prompt in place to fix a stored run.
- Unit tests use recorded LLM responses. Live-model tests are behind a flag and don't run by default.
- No PII in logs, error messages, or test output. Log hashed references.
- The audit log is append-only. No code path updates or deletes audit rows.

## Commands

Run from `backend/` (the venv is `backend/.venv`; create it with `uv venv .venv && uv pip install -e ".[dev]"`).

- `pytest -q`: full suite, offline (simulated model and recorded responses). Set `TEST_DATABASE_URL=postgresql+psycopg://...` to run the database tests against Postgres, including the append-only trigger.
- `pytest -q --live`: also runs tests that call the real API (needs `ANTHROPIC_API_KEY`).
- `screener run <file> --config ../configs/pipelines/backend-engineer-sim.yaml`: one document end to end.
- `screener eval --config <yaml> --eval-set ../fixtures/evalsets/backend-engineer-v1.jsonl --out report.json`: eval suite and gate.
- `screener diff before.json after.json`: version diff.
- `docker compose up` (repo root): Postgres plus the API on :8000.

Configs whose models start with `simulated-` use the offline stand-in in `screener/simulated.py`. They exist for development and tests and are never for real candidates.
