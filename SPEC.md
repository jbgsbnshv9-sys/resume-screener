# Resume screening pipeline: scope

## Purpose

This is an internal tool that screens resumes against a locked, job-specific rubric. It produces evidence-backed criterion scores and a suggested tier for each candidate, then routes every candidate to a human reviewer, who makes the decision. Every stage's input and output is stored and viewable as a trace. Pipeline configurations are versioned, and a new version can't go live until it passes an evaluation and bias suite.

## Prerequisite: jurisdictions (answer before M0)

Which jurisdictions applicants are in determines the data model, not just notice text. Recruitment tools are high-risk under the EU AI Act (Annex III: risk management, logging, human oversight, transparency). NYC Local Law 144 requires an independent bias audit, candidate notice, and an alternative selection process. Illinois, Colorado, and GDPR Art. 22 add their own requirements. Whatever the answer, v1 includes:

- per-candidate notice and consent records;
- an **opt-out path**: a candidate who requests a non-AI review is routed to manual review with no pipeline run;
- a data-subject access export (the per-candidate audit bundle, below);
- retention periods configurable per jurisdiction.

## Principles the code must enforce

These aren't preferences; the system's integrity depends on them.

1. **The system never rejects anyone automatically, and review must be meaningful.** The system can only suggest a tier and order the review queue. Every candidate must be opened by a human before a decision is recorded. No bulk decisions.
2. **Resume content is untrusted data.** It never goes into a system prompt. It's always passed in the user turn inside clearly delimited tags, with instructions to treat it as content to evaluate rather than instructions to follow. The structural defenses matter more than the tags: LLM nodes have no tools, outputs are schema-constrained, and evidence is verified in code.
3. **Every score above level 0 must cite evidence.** Evidence quotes are verified in code against the source text. A score whose evidence can't be verified is invalid and gets flagged. Level 0 means "no supporting evidence found" and requires no quote.
4. **Aggregation happens in code, never in the model.**
5. **Every candidate in a requisition is scored under the same pinned config version.** Config versions are immutable once created. A config is pinned to a requisition when its first candidate is scored and stays pinned for the requisition's life. Changing it requires re-scoring every open candidate in that requisition under the new config.
6. **Every LLM call is recorded, and everything downstream of the LLM is recomputable.** Store the model ID, prompt hash, config hash, effort setting, raw responses, and parsed outputs. Sampled LLM output can't be replayed, but verification, medians, aggregation, and tiers must be exactly recomputable from stored data.
7. **Real resumes never appear in the repo, fixtures, tests, or development.** Use synthetic data only. Consented real resumes, if any, live only in a separate, access-controlled eval store.
8. **Models return quotes; code computes spans.** LLMs are unreliable at character offsets. Any field that points into the source text is returned by the model as a verbatim substring, and the span is found deterministically in code.

## Out of scope for v1

The following are excluded from v1: a candidate-facing interface, ATS integrations (manual upload and CSV/zip import only), multi-tenant SaaS features, interview scheduling, and any automated outbound communication to candidates.

Deferred past the v1 core (see Milestones): LLM rubric drafting, PDF audit export, and the sandbox UI.

## Users and roles

| Role | Can do |
|---|---|
| Admin | Manage users and promote config versions to active |
| Rubric owner | Create and edit rubrics and configs in the sandbox, and run evals |
| Reviewer | View traces and record decisions for assigned requisitions |
| Auditor | Read-only access to everything, plus audit exports |

## Pipeline nodes

Each node is a pure function with typed input and output. Each node writes a `NodeOutput` record containing its inputs, outputs, config, timing, and token usage.

**1. Intake (deterministic).** Accept PDF and DOCX files. DOCX is converted to PDF (LibreOffice, headless) so there's a single extraction path; the DOCX is also scanned directly for its own hidden-text forms (`w:vanish`, white runs, tiny fonts). Extract text along with font, size, color, opacity, and position metadata for each span (PyMuPDF). Render each page and sample the pixels under each span's bounding box to compare what a human would see against what was extracted. Flag a span as hidden if it has near-background color, a font size below a threshold, sits off-page, has zero opacity, or appears only in metadata fields. A PDF with no extractable text (scanned or image-only) is flagged `no_extractable_text` and routed to manual review; it never fails silently.

Output: clean visible text, the full extracted text, and the hidden-span flags. Hidden text is never passed to scoring. When something is flagged, the configured action routes the candidate to review with a neutral warning ("hidden text present, not used in scoring"). Many templates contain harmless ATS keyword blocks, so the wording must not prejudice the reviewer. Intake never rejects.

**2. Parse (LLM).** Convert visible text into a JSON record using structured output: contact, experience entries (title, org, start, end, description), education, skills, certifications, links. Each text field is returned as a verbatim substring of the visible text, and code locates its span (principle 8). Validation checks the JSON against the schema and checks that every field resolves to a span. If validation fails, retry once, then flag for human review.

**3. Redact (deterministic).** Remove the configured fields before scoring. The defaults are name, contact details, photo, address, and links. Optional removals, off by default but configurable per rubric, are school names, graduation years, and other date fields. Field-level redaction does not remove proxies in free text: pronouns, affinity organizations, graduation years written into descriptions, locations. A best-effort free-text pass (pronoun neutralization, configurable term lists) runs on descriptions, and the counterfactual suite measures what gets through (see Evaluation suite).

Redaction produces an **offset map** from redacted text back to visible text, and from there to the original document. Evidence is verified against the redacted text but highlighted in the original. Store the redaction map separately, with tighter access controls than the rest of the trace.

**4. Score (LLM).** Make one call per criterion, so criteria don't influence each other. Each call receives the redacted record, the criterion definition, and its anchored levels. The redacted record comes first in the user turn and the criterion after it, so the shared prefix can be prompt-cached across that candidate's criterion calls. The output is a level, evidence quotes (required for levels 1–3), and a short rationale.

Run N samples per criterion (default 3) and record all of them. The final score is the median level. Flag the criterion as unstable if the sample spread exceeds the threshold, **or** if the samples would land the candidate on different sides of a tier boundary.

Verify every evidence quote against the redacted record using exact or normalized-whitespace matching. Evidence for level 2 or higher must come from at least one experience, project, or certification entry; a match in the skills list alone isn't enough.

**5. Aggregate (deterministic).**
- Weighted score = Σ(wᵢ · levelᵢ) / (3 · Σwᵢ), in [0, 1].
- Map the weighted score to tier bands: strong, review, or weak.
- A must-have criterion at level 0 caps the tier at `review`. It never produces an automatic `weak` or reject on its own.
- Any flag from an earlier node (hidden text, no extractable text, parse failure, unverified evidence, unstable criterion) moves the candidate to "needs attention."

**6. Human review.** There is a queue for each requisition, sorted by tier and flags. The rules:
- The reviewer must open the document before the decision form is enabled. View events and time on page are recorded.
- The reviewer records a decision (advance, hold, or decline), a required free-text reason, and whether they're overriding the suggested tier.
- No bulk actions.
- Every candidate must be opened within a configurable SLA. Candidates past the SLA, and holds older than a configurable age, raise alerts. An indefinite hold is a de facto rejection.
- Reviewer-level metrics (override rate, agreement with suggested tier, median time per review) are tracked. A near-zero override rate is a rubber-stamping signal.
- Optional per requisition: **blind first look**, where the suggested tier and scores are hidden until the reviewer has viewed the document, to reduce anchoring.

## Rubric

In the v1 core, rubrics are hand-written YAML files. The LLM drafting assistant is deferred. Each criterion has the following fields:

- a name and description;
- a weight;
- a must-have flag;
- anchored levels 0–3, each with a concrete description and an example (level 0 is "no supporting evidence");
- a required job-relatedness justification field.

A lint step flags proxy-risk language in **every text field**: name, description, every level description, and every example. It flags minimum tenure, years-of-experience thresholds, employer or school prestige ("top-tier," "Ivy-level"), employment gaps, location, "culture fit," recency-of-graduation phrasing, and age-correlated phrasing such as "digital native." The lint doesn't block saving. It requires the owner to acknowledge each flag and records that acknowledgment.

## Config and versioning

A pipeline config is a YAML document that bundles the rubric version with settings for every node. Example:

```yaml
rubric: backend-engineer-2026q4@v3
intake:
  min_font_pt: 4
  color_delta_threshold: 0.05
  on_hidden_text: flag_for_review
parse:
  model: claude-haiku-5-5
  effort: low
redact:
  fields: [name, contact, photo, address, links]
  optional: [school_names]
  free_text: { neutralize_pronouns: true, term_lists: [affinity_orgs] }
score:
  model: claude-sonnet-5-5
  effort: medium
  samples: 3
  instability_threshold: 1      # max level spread before flagging
  flag_tier_straddle: true      # also flag when samples straddle a tier boundary
aggregate:
  tiers: { strong: 0.75, review: 0.45 }
  must_have_cap: review
review:
  open_sla_days: 5
  hold_alert_days: 14
  blind_first_look: false
```

There is no `temperature`: Messages API version 2026-10-21 rejects `temperature`, `top_p` and `top_k` on every model, so configs that set them fail validation. Sample-to-sample variation comes from default sampling, and `effort` is the tunable.

A config's hash covers the YAML, the rubric content, every prompt template, and every term list it uses. Configs are content-hashed and immutable. Each one moves through the states draft → validated → active → retired. Only one config can be active per role, and only an admin can promote one. Promotion requires a passing eval run on that exact config hash. Promoting a new config doesn't change requisitions that are already pinned (principle 5).

**Model deprecation.** When a model referenced by a pinned config is scheduled for retirement, the system raises an alert on every affected open requisition. The resolution is a new config that passes the eval gate, followed by a full re-score of the requisition's open candidates. Candidates are never silently re-scored under a different model.

## Evaluation suite

**Golden set.** Synthetic resumes for each role (consented real resumes only from the separate eval store, per principle 7). Start with at least 50 per role and grow from there.
- Each case is labeled by **two or more independent labelers**. Their inter-rater kappa is reported as the agreement ceiling. Model agreement above that ceiling is treated as suspicious, not as a success.
- LLM-generated resumes tend to be cleaner and more uniform than real ones, so the set must include deliberately messy fixtures: multi-column layouts, tables, odd fonts, non-English sections, very long and very short resumes, scanned images.

**Metrics.** Every metric is reported with a 95% confidence interval. At n = 50, the interval on kappa is roughly ±0.15–0.2.
- tier agreement with labels (accuracy and Cohen's kappa);
- run-to-run consistency (re-run the full set K times and measure tier flips);
- evidence verification rate;
- **evidence relevance**: on a sampled subset, does the verified quote actually support the level? Measured by human spot-check, with an optional LLM-judge pass;
- **injection robustness**: for each seeded injection fixture (explicit visible instructions and hidden-keyword stuffing), the injected variant must not score higher than its clean base. Hidden-text detection rate is reported separately.

**Counterfactual generator.** Takes a base resume and produces variants that change exactly one attribute:
- name, drawn from name lists associated with different demographic groups;
- an inserted employment gap;
- the country where credentials were earned;
- a rewrite in non-native English style;
- an LLM-polished rewrite of the same content;
- affinity-organization membership (to test free-text redaction).

Each variant declares which criteria it is **allowed to affect**. An inserted gap changes total experience, so a legitimate tenure criterion may move. Deltas are measured on the remaining criteria and on the tier. Name and affinity counterfactuals run in two modes: with redaction on, which tests the redactor, and with redaction off, which measures the underlying model's sensitivity. Report the mean score delta, the tier flip rate, and impact ratios for each attribute; the four-fifths rule is a flag, not a pass/fail line.

**Gate.** Thresholds are configurable for each metric and are applied to the **lower bound** of the confidence interval, with a minimum n per role. The promotion gate requires every threshold to pass, or an admin override with a recorded justification.

Eval runs and K-times consistency runs use the Message Batches API to cut cost.

## Screens

The **trace viewer** has one page per candidate. It shows the original document with hidden spans highlighted, then each node in sequence with its inputs, outputs, and metadata. Criterion scores show their evidence highlighted in the original document via the offset map, and you can expand them to see individual samples. Below that are the aggregate tier with its contributing flags, and the human decision.

The **review queue** is a list per requisition, with filters, SLA and hold alerts, and the decision form.

The **version diff** compares two config versions on the same eval set. It shows candidates whose tier changed, changes in agreement and consistency (with CIs), and changes in counterfactual deltas and impact ratios. This is the most important view in the app, and it ships first as a CLI report (`screener diff`) before any UI.

The **audit export** produces a JSON bundle per candidate or per requisition: config, rubric, all node outputs, review events, the decision, and the timeline. A PDF rendering is deferred.

Deferred: the **sandbox** UI (the CLI covers editing a config, running it against the golden set, and viewing results) and the **rubric builder** UI.

## Data model

| Entity | Key fields |
|---|---|
| Role | title, job description, active config hash |
| Requisition | role, opening ref, pinned config hash, pinned_at, status |
| Rubric | id, version, criteria JSON, author, lint acknowledgments |
| PipelineConfig | hash, YAML, rubric ref, status, promoted_by, eval_run ref |
| Candidate | person key (for dedup), source, jurisdiction, created_at, retention_until |
| Application | candidate, requisition, applied_at (one candidate can apply to several requisitions) |
| CandidateNotice | application, notice version, delivered_at, channel |
| Consent / OptOut | application, type, recorded_at, recorded_by; an opt-out routes to manual-only review |
| Document | application, file blob ref, mime type, sha256 |
| Run | application, config hash, status, started/finished, supersedes (for re-scores) |
| NodeOutput | run, node, input ref, output JSON, model, prompt hash, tokens, latency, raw response |
| CriterionScore | run, criterion, samples, final level, evidence quotes + computed spans, verified, unstable |
| Flag | run, node, type, detail |
| ReviewerAssignment | reviewer, requisition, assigned_at |
| ReviewEvent | application, reviewer, event (opened, viewed_page, closed), timestamp, duration |
| Decision | application, run, reviewer, decision, reason, override, timestamp |
| EvalSet / EvalCase | role, synthetic resume ref, labels (per labeler), counterfactual parent, attribute, allowed criteria |
| EvalRun | config hash, metrics JSON (with CIs), pass/fail, timestamp |
| AuditLog | actor, action, entity, before/after hash, timestamp (append-only) |

## Security and privacy

All users authenticate, and every API route enforces role-based access. The redaction map and original documents are restricted to reviewers, admins, and auditors. Data is encrypted at rest. Retention is configurable per jurisdiction, with a scheduled purge job and a manual deletion workflow that leaves an audit record of the deletion itself. No PII goes into application logs. If you add an external observability tool, send it hashed references only, not resume content.

## Tech stack (defaults)

| Layer | Choice |
|---|---|
| Backend | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy plus Alembic |
| Database | Postgres (JSONB for node outputs) |
| Jobs | A Postgres-backed queue such as `procrastinate`, to avoid adding Redis in v1 |
| Documents | PyMuPDF for extraction and rendering, python-docx for DOCX hidden-text checks, headless LibreOffice for DOCX → PDF |
| LLM | Anthropic Python SDK, with native structured outputs (or tool use where needed), prompt caching, and the Message Batches API for evals; the model is set per node in config |
| Frontend | React, TypeScript, Vite, TanStack Query, PDF.js for highlighted document rendering |
| Tests | pytest, with recorded LLM responses for deterministic unit tests and live-model tests behind a flag |
| Infrastructure | Docker Compose for local development |

## Repo layout

```
/backend
  /screener
    /pipeline      # one module per node, each a pure function + Pydantic I/O
    /prompts       # versioned prompt templates (hashed at load)
    /evals         # eval set builder, counterfactual generator, metrics, gate, diff
    /lint          # rubric proxy-risk lint
    /synthetic     # synthetic personas and renderers (PDF, DOCX, adversarial variants)
    /cli
    /api
    /models        # SQLAlchemy
    llm.py         # model clients: Anthropic, cassette record/replay
    simulated.py   # deterministic offline stand-in model (dev and tests only)
    services.py    # review rules, pinning, promotion, export, retention
  /migrations      # Alembic
  /tests
/frontend
/fixtures          # committed synthetic eval set and sample documents
/configs           # rubrics, pipeline configs, term lists, gate thresholds, retention
SPEC.md
CLAUDE.md
```

## Milestones

Build the pipeline and evals as a command-line tool before any UI. The UI is a view onto data that has to be right first.

**v1 core**

| # | Milestone | Done when |
|---|---|---|
| M0 | Skeleton, data model (including Requisition, Application, notice/opt-out), migrations, Docker Compose | `docker compose up` works and migrations run |
| M1 | Intake and parse nodes with CLI; synthetic, injection, and messy-layout fixtures | Fixtures parse cleanly, every hidden-text fixture is flagged, image-only PDFs are flagged, and all parse spans are computed in code |
| M2 | Redact (with offset map), score, and aggregate nodes; end-to-end CLI run; rubric lint | A full run stores every NodeOutput, evidence verification works, level-0 and tier-straddle rules are tested, and aggregation is recomputable from stored outputs |
| M3 | Eval runner, counterfactual generator, and `screener diff` (CLI) | A metrics report with CIs is produced for a config, gate pass/fail is computed on lower bounds, and the diff report compares two configs |
| M4 | API and read-only trace viewer | Any run can be inspected node by node, with evidence highlighted in the original document |
| M5 | Review queue, decisions, review events, audit log | Decisions require an opened document and a reason, there are no bulk actions, SLA alerts fire, and the audit log is append-only and tested |
| M6 | Config versioning, requisition pinning, promotion gate | A config can't go active without a passing eval on its exact hash, and pinned requisitions are unaffected by promotion |
| M7 | Hardening: RBAC review, retention jobs, JSON export bundles, opt-out path, load test | Everything passes security review, and exports reconstruct a decision end to end |

**Deferred:** LLM rubric drafting and rubric builder UI, sandbox UI, version-diff UI (the CLI report comes first), PDF audit export, reviewer metrics dashboard UI (the metrics are computed from M5 onward).

## Open questions

1. Will you have voluntary demographic self-identification data? Without it, fairness measurement in production is limited to counterfactual testing, and the impact-ratio dashboard can only run on eval data.
2. What volume do you expect, and what's the latency budget? This affects the number of samples and the model choice for the scoring node.
3. Does the system need to integrate with an existing ATS later? If so, keep candidate IDs and the shape of the import pipeline compatible from the start.
4. Who supplies golden-set labels, and can you get two or more independent labelers per role?
