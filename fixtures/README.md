# Fixtures

Everything here is synthetic (CLAUDE.md principle 7). Names, employers, schools,
phone numbers (555-01xx) and emails (example.com) are invented.

- `evalsets/backend-engineer-v1.jsonl`: the eval set: 60 base personas (balanced across
  tiers, including two-column layouts), counterfactual variants for 10 of them, and visible
  injection and hidden-text variants for 10 more. Labels are a single
  `generator-intended` label per base case. Add independent human labels before
  promoting a config for real use. Regenerate it with
  `screener evalset build --out fixtures/evalsets/backend-engineer-v1.jsonl`.
- `samples/`: the fixture suite as rendered files: clean resumes at each tier, every
  hidden-text kind, visible injection, image-only, two-column, and DOCX with and
  without hidden runs. Regenerate them with `screener fixtures --out fixtures/samples`.
