import io
import zipfile
from datetime import timedelta

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from screener import services
from screener.evals.evalset import build_eval_set, save_eval_set
from screener.models.db import AppendOnlyViolation, Application, AuditLog, Candidate, Document, RedactionMapRow, Run
from tests.conftest import SIM_CONFIG

SIM_YAML = SIM_CONFIG.read_text()


@pytest.fixture
def env(db_engine, blob_dir, provider, tmp_path, monkeypatch):
    import screener.api.app as api

    sets = tmp_path / "evalsets"
    save_eval_set(build_eval_set("tiny", "backend-engineer", n_base=9, n_counterfactual_parents=1, n_injection_parents=2, seed=5), sets / "tiny.jsonl")
    monkeypatch.setattr(api, "EVAL_SETS_DIR", sets)
    maker = sessionmaker(bind=db_engine, expire_on_commit=False)
    with maker() as s:
        admin, admin_token = services.create_user(s, "admin@example.com", "Admin", "admin", "bootstrap")
        s.commit()
    client = TestClient(api.create_app(maker, provider, run_evals_inline=True))

    def as_(token):
        return {"Authorization": f"Bearer {token}"}

    tokens = {"admin": admin_token}
    for name, role in [("owner", "rubric_owner"), ("rev", "reviewer"), ("rev2", "reviewer"), ("aud", "auditor")]:
        r = client.post("/api/users", json={"email": f"{name}@example.com", "display_name": name, "role": role}, headers=as_(admin_token))
        assert r.status_code == 200, r.text
        tokens[name] = r.json()["api_token"]
        tokens[name + "_id"] = r.json()["id"]
    return client, {k: (as_(v) if not k.endswith("_id") else v) for k, v in tokens.items()}, maker


def _setup_requisition(client, h, yaml_text=SIM_YAML, justification="test override"):
    cfg = client.post("/api/configs", json={"yaml": yaml_text}, headers=h["owner"]).json()
    r = client.post(f"/api/configs/{cfg['hash']}/promote", json={"override_justification": justification}, headers=h["admin"])
    assert r.status_code == 200, r.text
    req = client.post("/api/requisitions", json={"role_id": "backend-engineer", "opening_ref": "BE-1"}, headers=h["admin"]).json()
    assert client.post(f"/api/requisitions/{req['id']}/reviewers", json={"reviewer_id": h["rev_id"]}, headers=h["admin"]).status_code == 200
    return cfg, req


def _upload(client, h, req_id, data, name="r.pdf", who="rev", **form):
    return client.post(f"/api/requisitions/{req_id}/applications", files={"file": (name, data, "application/pdf")}, data={k: str(v) for k, v in form.items()}, headers=h[who])


def test_auth_and_roles(env):
    client, h, _ = env
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/me", headers=h["rev"]).json()["role"] == "reviewer"
    assert client.post("/api/users", json={"email": "x@example.com", "display_name": "x", "role": "admin"}, headers=h["rev"]).status_code == 403
    assert client.post("/api/configs", json={"yaml": SIM_YAML}, headers=h["rev"]).status_code == 403


def test_promotion_requires_passing_eval_or_justified_override(env):
    client, h, _ = env
    cfg = client.post("/api/configs", json={"yaml": SIM_YAML}, headers=h["owner"]).json()
    assert cfg["status"] == "draft" and cfg["hash_still_resolves"]
    bad = yaml.safe_load(SIM_YAML)
    bad["score"]["temperature"] = 0.3
    assert client.post("/api/configs", json={"yaml": yaml.safe_dump(bad)}, headers=h["owner"]).status_code == 400

    r = client.post(f"/api/configs/{cfg['hash']}/promote", json={}, headers=h["admin"])
    assert r.status_code == 409 and "no passing eval" in r.json()["detail"]
    assert client.post(f"/api/configs/{cfg['hash']}/promote", json={}, headers=h["owner"]).status_code == 403

    # A tiny eval set can't pass the gate (too few cases, wide intervals).
    assert client.post(f"/api/configs/{cfg['hash']}/evals", json={"eval_set": "tiny", "repeats": 1}, headers=h["owner"]).status_code == 202
    view = client.get(f"/api/configs/{cfg['hash']}", headers=h["owner"]).json()
    assert len(view["eval_runs"]) == 1 and view["eval_runs"][0]["passed"] is False
    run = client.get(f"/api/eval-runs/{view['eval_runs'][0]['id']}", headers=h["aud"]).json()
    assert "tier_kappa" in run["metrics"] and run["gate"]["checks"]
    assert client.post(f"/api/configs/{cfg['hash']}/promote", json={}, headers=h["admin"]).status_code == 409

    r = client.post(f"/api/configs/{cfg['hash']}/promote", json={"override_justification": "pilot with synthetic data only"}, headers=h["admin"])
    assert r.status_code == 200 and r.json()["status"] == "active"
    audit_rows = client.get("/api/audit", params={"entity_id": cfg["hash"]}, headers=h["aud"]).json()
    promote = next(a for a in audit_rows if a["action"] == "config.promote")
    assert promote["detail"]["override"] and promote["detail"]["justification"] == "pilot with synthetic data only"


def test_promotion_with_passing_eval(env):
    client, h, maker = env
    from screener.config import load_config_text
    from screener.evals.runner import EvalReport, GateResult

    cfg = client.post("/api/configs", json={"yaml": SIM_YAML}, headers=h["owner"]).json()
    loaded = load_config_text(SIM_YAML)
    report = EvalReport(config_hash=loaded.hash, eval_set_id="x", eval_set_hash="y", created_at="now", repeats=1, results=[], metrics={}, counterfactual={}, gate=GateResult(passed=True, checks=[]))
    with maker() as s:
        services.store_eval_run(s, report, "owner@example.com")
        s.commit()
    assert client.get(f"/api/configs/{cfg['hash']}", headers=h["owner"]).json()["status"] == "validated"
    r = client.post(f"/api/configs/{cfg['hash']}/promote", json={}, headers=h["admin"])
    assert r.status_code == 200 and r.json()["eval_run_id"] and r.json()["override_justification"] is None


def test_review_flow(env, fixture_files):
    client, h, maker = env
    cfg, req = _setup_requisition(client, h)
    r = _upload(client, h, req["id"], fixture_files["clean_strong.pdf"], jurisdiction="unknown", notice_version="2026-10")
    assert r.status_code == 200, r.text
    app_id, run_id = r.json()["application_id"], r.json()["run_id"]
    reqs = client.get("/api/requisitions", headers=h["rev"]).json()
    assert reqs[0]["pinned_config_hash"] == cfg["hash"]

    # Unassigned reviewers and rubric owners can't see candidate data.
    assert client.get(f"/api/requisitions/{req['id']}/queue", headers=h["rev2"]).status_code == 403
    assert client.get(f"/api/runs/{run_id}/trace", headers=h["rev2"]).status_code == 403
    assert client.get(f"/api/runs/{run_id}/trace", headers=h["owner"]).status_code == 403
    assert client.get(f"/api/runs/{run_id}/redaction-map", headers=h["owner"]).status_code == 403

    queue = client.get(f"/api/requisitions/{req['id']}/queue", headers=h["rev"]).json()
    assert queue[0]["tier"] == "strong" and not queue[0]["opened"]
    trace = client.get(f"/api/runs/{run_id}/trace", headers=h["rev"]).json()
    assert [n["node"] for n in trace["nodes"]] == ["intake", "parse", "redact", "score", "aggregate"]
    assert trace["evidence"] and trace["evidence"][0]["pages"]
    assert client.get(f"/api/runs/{run_id}/redaction-map", headers=h["rev"]).json()

    # A decision needs the document opened first, and a reason. No bulk endpoint exists.
    r = client.post(f"/api/applications/{app_id}/decision", json={"decision": "advance", "reason": "strong backend work", "override": False}, headers=h["rev"])
    assert r.status_code == 409 and "open" in r.json()["detail"]
    assert client.post(f"/api/applications/{app_id}/events", json={"event": "opened"}, headers=h["rev"]).status_code == 400
    doc_id = client.get(f"/api/applications/{app_id}", headers=h["rev"]).json()["document_id"]
    pdf = client.get(f"/api/documents/{doc_id}/render", headers=h["rev"])
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
    assert client.post(f"/api/applications/{app_id}/decision", json={"decision": "advance", "reason": "   ", "override": False}, headers=h["rev"]).status_code == 400
    assert client.post(f"/api/applications/{app_id}/decision", json={"decision": "reject", "reason": "x", "override": False}, headers=h["rev"]).status_code == 400
    r = client.post(f"/api/applications/{app_id}/decision", json={"decision": "advance", "reason": "Owned production Go services end to end.", "override": False}, headers=h["rev"])
    assert r.status_code == 200 and r.json()["suggested_tier"] == "strong"

    # Export reconstructs the decision end to end.
    bundle = client.get(f"/api/export/applications/{app_id}", headers=h["aud"]).json()
    assert bundle["decisions"][0]["reason"].startswith("Owned production")
    assert bundle["configs"][cfg["hash"]]["yaml"] == SIM_YAML
    assert bundle["runs"][0]["result"]["aggregate"]["tier"] == "strong"
    assert [e["event"] for e in bundle["review_events"]] == ["opened"]
    assert bundle["notices"][0]["version"] == "2026-10"
    assert len(bundle["bundle_hash"]) == 64
    assert client.get(f"/api/export/applications/{app_id}", headers=h["rev"]).status_code == 403

    metrics = client.get("/api/metrics/reviewers", headers=h["admin"]).json()
    assert metrics[0]["decisions"] == 1 and metrics[0]["override_rate"] == 0.0


def test_opt_out_never_runs_pipeline(env, fixture_files):
    client, h, maker = env
    _, req = _setup_requisition(client, h)
    r = _upload(client, h, req["id"], fixture_files["clean_strong.pdf"], ai_opt_out=True)
    assert r.json()["run_id"] is None and r.json()["status"] == "manual_only"
    queue = client.get(f"/api/requisitions/{req['id']}/queue", headers=h["rev"]).json()
    assert queue[0]["ai_opt_out"] and queue[0]["tier"] is None and queue[0]["needs_attention"]
    with maker() as s:
        assert s.scalars(select(Run)).first() is None

    # Opting out after scoring hides the AI output from reviewers.
    r = _upload(client, h, req["id"], fixture_files["clean_review.pdf"])
    app_id, run_id = r.json()["application_id"], r.json()["run_id"]
    assert client.post(f"/api/applications/{app_id}/opt-out", headers=h["admin"]).status_code == 200
    trace = client.get(f"/api/runs/{run_id}/trace", headers=h["rev"]).json()
    assert trace["ai_hidden"] and trace["tier"] is None and trace["scores"] == []


def test_requisition_pinning_and_rescore(env, fixture_files):
    client, h, maker = env
    cfg1, req = _setup_requisition(client, h)
    _upload(client, h, req["id"], fixture_files["clean_review.pdf"])
    v2 = yaml.safe_load(SIM_YAML)
    v2["score"]["samples"] = 5
    cfg2 = client.post("/api/configs", json={"yaml": yaml.safe_dump(v2)}, headers=h["owner"]).json()
    assert client.post(f"/api/configs/{cfg2['hash']}/promote", json={"override_justification": "test"}, headers=h["admin"]).status_code == 200
    assert client.get(f"/api/configs/{cfg1['hash']}", headers=h["admin"]).json()["status"] == "retired"

    # The pinned requisition keeps scoring under v1 after v2 goes live.
    r = _upload(client, h, req["id"], fixture_files["clean_weak.pdf"])
    with maker() as s:
        assert s.get(Run, r.json()["run_id"]).config_hash == cfg1["hash"]
    # A new requisition pins to v2.
    req2 = client.post("/api/requisitions", json={"role_id": "backend-engineer", "opening_ref": "BE-2"}, headers=h["admin"]).json()
    r = _upload(client, h, req2["id"], fixture_files["clean_weak.pdf"], who="admin")
    with maker() as s:
        assert s.get(Run, r.json()["run_id"]).config_hash == cfg2["hash"]

    # Moving the first requisition to v2 re-scores every open candidate.
    r = client.post(f"/api/requisitions/{req['id']}/rescore", json={"config_hash": cfg2["hash"]}, headers=h["admin"])
    assert r.status_code == 200 and r.json()["rescored"] == 2
    with maker() as s:
        apps = s.scalars(select(Application).where(Application.requisition_id == req["id"])).all()
        for a in apps:
            latest = services.latest_run(s, a.id)
            assert latest.config_hash == cfg2["hash"] and latest.supersedes_run_id is not None
    assert client.post(f"/api/requisitions/{req['id']}/rescore", json={"config_hash": cfg1["hash"]}, headers=h["admin"]).status_code == 409


def test_stale_config_is_refused(env, fixture_files):
    client, h, maker = env
    cfg, req = _setup_requisition(client, h)
    from screener.models.db import PipelineConfigRow

    with maker() as s:
        s.get(PipelineConfigRow, cfg["hash"]).yaml_text = SIM_YAML.replace("samples: 3", "samples: 4")
        s.commit()
    r = _upload(client, h, req["id"], fixture_files["clean_review.pdf"])
    assert r.status_code == 409 and "no longer resolves" in r.json()["detail"]


def test_blind_first_look(env, fixture_files):
    client, h, _ = env
    blind = yaml.safe_load(SIM_YAML)
    blind["review"]["blind_first_look"] = True
    _, req = _setup_requisition(client, h, yaml.safe_dump(blind))
    r = _upload(client, h, req["id"], fixture_files["clean_strong.pdf"])
    app_id, run_id = r.json()["application_id"], r.json()["run_id"]
    assert client.get(f"/api/requisitions/{req['id']}/queue", headers=h["rev"]).json()[0]["tier_hidden"]
    assert client.get(f"/api/runs/{run_id}/trace", headers=h["rev"]).json()["blind"]
    assert client.post(f"/api/applications/{app_id}/events", json={"event": "scores_revealed"}, headers=h["rev"]).status_code == 409
    doc_id = client.get(f"/api/applications/{app_id}", headers=h["rev"]).json()["document_id"]
    client.get(f"/api/documents/{doc_id}/render", headers=h["rev"])
    assert client.post(f"/api/applications/{app_id}/events", json={"event": "scores_revealed"}, headers=h["rev"]).status_code == 200
    trace = client.get(f"/api/runs/{run_id}/trace", headers=h["rev"]).json()
    assert not trace["blind"] and trace["tier"] == "strong"
    # Admins and auditors aren't blinded.
    assert client.get(f"/api/runs/{run_id}/trace", headers=h["aud"]).json()["tier"] == "strong"


def test_sla_and_hold_alerts(env, fixture_files):
    client, h, maker = env
    _, req = _setup_requisition(client, h)
    a1 = _upload(client, h, req["id"], fixture_files["clean_strong.pdf"]).json()["application_id"]
    a2 = _upload(client, h, req["id"], fixture_files["clean_review.pdf"]).json()["application_id"]
    assert client.get("/api/alerts", headers=h["admin"]).json() == []
    doc = client.get(f"/api/applications/{a2}", headers=h["rev"]).json()["document_id"]
    client.get(f"/api/documents/{doc}/render", headers=h["rev"])
    client.post(f"/api/applications/{a2}/decision", json={"decision": "hold", "reason": "waiting on references", "override": False}, headers=h["rev"])
    with maker() as s:
        s.get(Application, a1).applied_at = services.utcnow() - timedelta(days=6)
        d = s.scalars(select(services.Decision).where(services.Decision.application_id == a2)).one()
        d.at = services.utcnow() - timedelta(days=15)
        s.commit()
    alerts = {(a["type"], a["application_id"]) for a in client.get("/api/alerts", headers=h["rev"]).json()}
    assert alerts == {("not_opened", a1), ("stale_hold", a2)}
    assert client.get(f"/api/requisitions/{req['id']}/queue", headers=h["rev"]).json()[0]["overdue"]


def test_delete_candidate_leaves_audit_record(env, fixture_files, blob_dir):
    client, h, maker = env
    _, req = _setup_requisition(client, h)
    app_id = _upload(client, h, req["id"], fixture_files["clean_strong.pdf"]).json()["application_id"]
    with maker() as s:
        app = s.get(Application, app_id)
        cand_id = app.candidate_id
        refs = [d.blob_ref for d in s.scalars(select(Document).where(Document.application_id == app_id))]
    assert client.post(f"/api/candidates/{cand_id}/delete", json={"reason": "candidate request"}, headers=h["rev"]).status_code == 403
    r = client.post(f"/api/candidates/{cand_id}/delete", json={"reason": "candidate request"}, headers=h["admin"])
    assert r.status_code == 200 and r.json()["deleted"]["runs"] == 1
    with maker() as s:
        assert s.get(Candidate, cand_id) is None
        assert s.scalars(select(Run)).first() is None and s.scalars(select(RedactionMapRow)).first() is None
        entry = s.scalars(select(AuditLog).where(AuditLog.action == "candidate.delete")).one()
        assert entry.detail["reason"] == "candidate request"
    from screener import blobs

    for ref in refs:
        with pytest.raises(FileNotFoundError):
            blobs.get(ref)


def test_retention_purge(env, fixture_files):
    client, h, maker = env
    _, req = _setup_requisition(client, h)
    app_id = _upload(client, h, req["id"], fixture_files["clean_strong.pdf"]).json()["application_id"]
    with maker() as s:
        assert services.purge_expired(s) == []
        cand = s.get(Candidate, s.get(Application, app_id).candidate_id)
        cand.retention_until = services.utcnow() - timedelta(days=1)
        s.commit()
        assert services.purge_expired(s) == [cand.id]
        s.commit()


def test_zip_import_with_manifest(env, fixture_files):
    client, h, _ = env
    _, req = _setup_requisition(client, h)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.pdf", fixture_files["clean_strong.pdf"])
        z.writestr("b.docx", fixture_files["clean_strong.docx"])
        z.writestr("c.pdf", fixture_files["image_only.pdf"])
        z.writestr("manifest.csv", "filename,jurisdiction,ai_opt_out,notice_version\na.pdf,unknown,false,v1\nc.pdf,unknown,true,v1\n")
    r = client.post(f"/api/requisitions/{req['id']}/import", files={"file": ("batch.zip", buf.getvalue(), "application/zip")}, headers=h["admin"])
    assert r.status_code == 200, r.text
    by_file = {a["file"]: a for a in r.json()["applications"]}
    assert by_file["a.pdf"]["run_status"] == "completed"
    assert by_file["b.docx"]["run_status"] == "completed"
    assert by_file["c.pdf"]["ai_opt_out"] and by_file["c.pdf"]["run_id"] is None


def test_audit_log_is_append_only(env, db_engine):
    _, _, maker = env
    with maker() as s:
        row = s.scalars(select(AuditLog)).first()
        row.actor = "tampered"
        with pytest.raises(AppendOnlyViolation):
            s.flush()
        s.rollback()
        row = s.scalars(select(AuditLog)).first()
        s.delete(row)
        with pytest.raises(AppendOnlyViolation):
            s.flush()
        s.rollback()
    if db_engine.dialect.name == "postgresql":
        from sqlalchemy.exc import DBAPIError

        for stmt in ("UPDATE audit_log SET actor = 'x'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
            with pytest.raises(DBAPIError):
                with db_engine.begin() as conn:
                    conn.execute(text(stmt))
