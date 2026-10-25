"""`screener` command line. The pipeline and evals are CLI-first (SPEC.md, Milestones)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from screener import settings


def _provider(args):
    from screener.llm import CassetteClient, default_provider

    base = default_provider(live=args.live or settings.live_llm_enabled())
    if not getattr(args, "cassette", None):
        return base
    mode = "record" if args.record else "replay"
    cassettes: dict[str, CassetteClient] = {}

    def provide(model: str):
        if model not in cassettes:
            cassettes[model] = CassetteClient(Path(args.cassette), mode, base(model) if mode == "record" else None)
        return cassettes[model]

    return provide


def _add_llm_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--live", action="store_true", help="allow real model calls (needs ANTHROPIC_API_KEY)")
    p.add_argument("--cassette", help="JSON file of recorded responses to replay (or record with --record)")
    p.add_argument("--record", action="store_true", help="record live responses into --cassette")


def cmd_db_upgrade(args) -> int:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "migrations"))
    command.upgrade(cfg, "head")
    return 0


def cmd_config_validate(args) -> int:
    from screener.config import load_config
    from screener.lint.rubric_lint import lint_rubric

    loaded = load_config(args.path)
    findings = lint_rubric(loaded.rubric)
    print(json.dumps({"hash": loaded.hash, "rubric": loaded.rubric.ref, "prompts": loaded.prompt_hashes, "lint": [f.model_dump() for f in findings]}, indent=2))
    return 0


def cmd_lint(args) -> int:
    import yaml

    from screener.config import Rubric
    from screener.lint.rubric_lint import lint_rubric

    rubric = Rubric.model_validate(yaml.safe_load(Path(args.path).read_text()))
    findings = lint_rubric(rubric)
    for f in findings:
        state = "acknowledged" if f.acknowledged else "NEEDS ACKNOWLEDGMENT"
        print(f"{f.criterion}.{f.field}: {f.rule} ({f.match!r}) - {state}\n    {f.explanation}")
    if not findings:
        print("no proxy-risk findings")
    return 0


def cmd_fixtures(args) -> int:
    from screener.synthetic.fixtures import write_fixtures

    for path in write_fixtures(Path(args.out)):
        print(path)
    return 0


def cmd_evalset_build(args) -> int:
    from screener.evals.evalset import build_eval_set, save_eval_set

    es = build_eval_set(args.id, args.role, n_base=args.n_base, seed=args.seed)
    save_eval_set(es, Path(args.out))
    print(f"{args.out}: {len(es.cases)} cases ({len(es.base_cases())} base), hash {es.content_hash[:12]}")
    return 0


def _print_run(run) -> None:
    print(f"status: {run.status}" + (f" ({run.stop_reason})" if run.stop_reason else ""))
    print(f"config: {run.config_hash[:12]}")
    if run.aggregate:
        a = run.aggregate
        print(f"suggested tier: {a.tier} (score {a.weighted_score:.3f}, range {a.score_range[0]:.2f}-{a.score_range[1]:.2f})" + (f", must-have at level 0: {a.capped_by}" if a.capped_by else ""))
        print(f"needs attention: {a.needs_attention}")
    for s in run.scores:
        levels = [x.level for x in s.samples]
        print(f"  {s.criterion:<26} level {s.final_level}  samples {levels}" + ("  UNSTABLE" if s.unstable else "") + ("" if s.verified else "  UNVERIFIED"))
        for e in s.evidence[:2]:
            print(f"      evidence: {e.quote[:90]!r}")
    for f in run.flags:
        print(f"  flag [{f.node}] {f.type}: {f.detail}" + (f" ({f.criterion})" if f.criterion else ""))


def cmd_run(args) -> int:
    from screener.config import load_config
    from screener.pipeline.runner import run_pipeline

    loaded = load_config(args.config)
    data = Path(args.file).read_bytes()
    run, artifacts = run_pipeline(data, Path(args.file).name, loaded, _provider(args))
    _print_run(run)
    if args.json:
        Path(args.json).write_text(run.model_dump_json(indent=1))
        print(f"trace written to {args.json}")
    if args.store:
        from screener.models.session import session_scope
        from screener.store import ensure_config, save_run

        with session_scope() as s:
            ensure_config(s, loaded, "cli")
            row = save_run(s, run, artifacts)
            print(f"stored run {row.id}")
    return 0


def cmd_recompute(args) -> int:
    from screener.config import load_config
    from screener.pipeline.recompute import recompute
    from screener.pipeline.runner import RunResult

    run = RunResult.model_validate_json(Path(args.trace).read_text())
    report = recompute(run, load_config(args.config))
    print(json.dumps(report.model_dump(), indent=2))
    return 0 if report.matches else 1


def cmd_eval(args) -> int:
    from screener.config import load_config
    from screener.evals.evalset import load_eval_set
    from screener.evals.gate import evaluate_gate, load_gate
    from screener.evals.runner import report_summary, run_eval

    loaded = load_config(args.config)
    es = load_eval_set(Path(args.eval_set))

    def progress(i: int, n: int) -> None:
        if i % 25 == 0 or i == n:
            print(f"  {i}/{n} cases", file=sys.stderr)

    report = run_eval(es, loaded, _provider(args), repeats=args.repeats, progress=progress)
    report.gate = evaluate_gate(report, load_gate(args.gate))
    Path(args.out).write_text(report.model_dump_json(indent=1))
    print(json.dumps(report_summary(report), indent=2))
    for c in report.gate.checks:
        if not c.passed:
            print(f"GATE FAIL {c.metric} ({c.bound} bound {c.observed}) vs threshold {c.threshold} {c.note}")
    if args.store:
        from screener.models.session import session_scope
        from screener.services import store_eval_run
        from screener.store import ensure_config

        with session_scope() as s:
            ensure_config(s, loaded, args.actor)
            row = store_eval_run(s, report, args.actor)
            print(f"stored eval run {row.id} (passed={row.passed})")
    return 0 if report.gate.passed else 2


def cmd_diff(args) -> int:
    from screener.evals.diff import diff_reports, format_diff
    from screener.evals.runner import EvalReport

    a = EvalReport.model_validate_json(Path(args.before).read_text())
    b = EvalReport.model_validate_json(Path(args.after).read_text())
    d = diff_reports(a, b)
    print(d.model_dump_json(indent=1) if args.json else format_diff(d))
    return 0


def cmd_user_create(args) -> int:
    from screener.models.session import session_scope
    from screener.services import create_user

    with session_scope() as s:
        user, token = create_user(s, args.email, args.name or args.email, args.role, "cli")
        print(json.dumps({"id": user.id, "email": user.email, "role": user.role, "api_token": token}))
    return 0


def cmd_config_register(args) -> int:
    from screener.models.session import session_scope
    from screener.services import register_config

    with session_scope() as s:
        row, loaded = register_config(s, Path(args.path).read_text(), args.actor)
        print(json.dumps({"hash": row.hash, "status": row.status, "role": row.role_id}))
    return 0


def cmd_promote(args) -> int:
    from sqlalchemy import select

    from screener.models.db import User
    from screener.models.session import session_scope
    from screener.services import ServiceError, promote_config

    with session_scope() as s:
        admin = s.scalars(select(User).where(User.email == args.admin, User.role == "admin")).first()
        if admin is None:
            print("no admin user with that email", file=sys.stderr)
            return 1
        try:
            row = promote_config(s, args.hash, admin, override_justification=args.override_justification)
        except ServiceError as e:
            print(f"refused: {e.message}", file=sys.stderr)
            return 1
        print(json.dumps({"hash": row.hash, "status": row.status, "eval_run_id": row.eval_run_id, "override": row.override_justification}))
    return 0


def cmd_purge(args) -> int:
    from screener.models.session import session_scope
    from screener.services import purge_expired

    with session_scope() as s:
        removed = purge_expired(s)
        print(json.dumps({"purged_candidates": len(removed)}))
    return 0


def cmd_demo(args) -> int:
    """Seed a demo instance with synthetic data: users, an eval-gated config, a requisition, fixture resumes."""
    from screener.config import load_config
    from screener.evals.evalset import load_eval_set
    from screener.evals.gate import evaluate_gate, load_gate
    from screener.evals.runner import run_eval
    from screener.models.session import session_scope
    from screener.services import assign_reviewer, create_requisition, create_user, promote_config, register_config, store_eval_run, submit_application
    from screener.synthetic.fixtures import demo_files

    config_path = Path(args.config)
    loaded = load_config(config_path)
    tokens = {}
    with session_scope() as s:
        users = {}
        for email, role in [("admin@example.com", "admin"), ("owner@example.com", "rubric_owner"), ("reviewer@example.com", "reviewer"), ("auditor@example.com", "auditor")]:
            users[role], tokens[role] = create_user(s, email, email.split("@")[0].title(), role, "demo")
        register_config(s, config_path.read_text(), users["rubric_owner"].email)
    print("running the eval suite (synthetic, simulated model)...", file=sys.stderr)
    report = run_eval(load_eval_set(Path(args.eval_set)), loaded, _provider(args), repeats=2)
    report.gate = evaluate_gate(report, load_gate(settings.REPO_ROOT / "configs" / "evals" / "default-gate.yaml"))
    with session_scope() as s:
        store_eval_run(s, report, "owner@example.com")
    if not report.gate.passed:
        print("eval gate failed; config not promoted", file=sys.stderr)
        print(json.dumps({"tokens": tokens}, indent=2))
        return 2
    with session_scope() as s:
        from screener.models.db import User

        admin = s.get(User, users["admin"].id)
        promote_config(s, loaded.hash, admin)
        req = create_requisition(s, loaded.rubric.role, "BE-DEMO-1", admin.email)
        assign_reviewer(s, users["reviewer"].id, req.id, admin.email)
        req_id = req.id
    # Distinct synthetic people per file, so dedup doesn't fold them into resubmissions.
    for name, data in demo_files().items():
        with session_scope() as s:
            submit_application(s, requisition_id=req_id, data=data, filename=name, actor="admin@example.com", provider=_provider(args), notice_version="demo", ai_opt_out=name.startswith("opt_out"))
    print(json.dumps({"config_hash": loaded.hash, "requisition_id": req_id, "tokens": tokens}, indent=2))
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("screener.api.app:app", host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="screener", description="Evidence-backed resume screening with human review")
    sub = p.add_subparsers(dest="cmd", required=True)

    db = sub.add_parser("db", help="database").add_subparsers(dest="sub", required=True)
    db.add_parser("upgrade", help="run migrations").set_defaults(fn=cmd_db_upgrade)

    cfg = sub.add_parser("config", help="pipeline configs").add_subparsers(dest="sub", required=True)
    v = cfg.add_parser("validate", help="resolve a config, print its hash and lint findings")
    v.add_argument("path")
    v.set_defaults(fn=cmd_config_validate)
    r = cfg.add_parser("register", help="store a config (draft) in the database")
    r.add_argument("path")
    r.add_argument("--actor", default="cli")
    r.set_defaults(fn=cmd_config_register)
    pr = cfg.add_parser("promote", help="promote a config to active (admin; needs a passing eval)")
    pr.add_argument("hash")
    pr.add_argument("--admin", required=True, help="admin user email")
    pr.add_argument("--override-justification")
    pr.set_defaults(fn=cmd_promote)

    lint = sub.add_parser("lint", help="lint a rubric YAML for proxy-risk language")
    lint.add_argument("path")
    lint.set_defaults(fn=cmd_lint)

    fx = sub.add_parser("fixtures", help="write the synthetic fixture suite")
    fx.add_argument("--out", default=str(settings.REPO_ROOT / "fixtures" / "generated"))
    fx.set_defaults(fn=cmd_fixtures)

    es = sub.add_parser("evalset", help="eval sets").add_subparsers(dest="sub", required=True)
    b = es.add_parser("build", help="generate a synthetic eval set")
    b.add_argument("--id", default="backend-engineer-v1")
    b.add_argument("--role", default="backend-engineer")
    b.add_argument("--n-base", type=int, default=60)
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--out", required=True)
    b.set_defaults(fn=cmd_evalset_build)

    run = sub.add_parser("run", help="run the pipeline on one document")
    run.add_argument("file")
    run.add_argument("--config", required=True)
    run.add_argument("--json", help="write the full trace here")
    run.add_argument("--store", action="store_true", help="store the run in the database")
    _add_llm_args(run)
    run.set_defaults(fn=cmd_run)

    rc = sub.add_parser("recompute", help="re-derive scores and tier from a stored trace (no model calls)")
    rc.add_argument("trace")
    rc.add_argument("--config", required=True)
    rc.set_defaults(fn=cmd_recompute)

    ev = sub.add_parser("eval", help="run the eval suite and compute the gate")
    ev.add_argument("--config", required=True)
    ev.add_argument("--eval-set", required=True)
    ev.add_argument("--gate", default=str(settings.REPO_ROOT / "configs" / "evals" / "default-gate.yaml"))
    ev.add_argument("--repeats", type=int, default=2)
    ev.add_argument("--out", required=True)
    ev.add_argument("--store", action="store_true")
    ev.add_argument("--actor", default="cli")
    _add_llm_args(ev)
    ev.set_defaults(fn=cmd_eval)

    df = sub.add_parser("diff", help="compare two eval reports (version diff)")
    df.add_argument("before")
    df.add_argument("after")
    df.add_argument("--json", action="store_true")
    df.set_defaults(fn=cmd_diff)

    users = sub.add_parser("user", help="users").add_subparsers(dest="sub", required=True)
    uc = users.add_parser("create", help="create a user and print its API token once")
    uc.add_argument("--email", required=True)
    uc.add_argument("--name")
    uc.add_argument("--role", required=True, choices=["admin", "rubric_owner", "reviewer", "auditor"])
    uc.set_defaults(fn=cmd_user_create)

    sub.add_parser("purge", help="delete candidates past their retention date").set_defaults(fn=cmd_purge)

    demo = sub.add_parser("demo", help="seed a demo instance with synthetic data (prints API tokens)")
    demo.add_argument("--config", default=str(settings.REPO_ROOT / "configs" / "pipelines" / "backend-engineer-sim.yaml"))
    demo.add_argument("--eval-set", default=str(settings.REPO_ROOT / "fixtures" / "evalsets" / "backend-engineer-v1.jsonl"))
    _add_llm_args(demo)
    demo.set_defaults(fn=cmd_demo)

    sv = sub.add_parser("serve", help="run the API server")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
