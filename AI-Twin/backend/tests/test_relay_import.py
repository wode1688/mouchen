from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.models import Goal, utc_now
from app.model_gateway import ModelGateway, ModelRoute, ModelUnavailable


@pytest.fixture
def local_client(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUCHEN_DB_PATH", str(tmp_path / "local.db"))
    monkeypatch.setenv("MOUCHEN_MODEL_PROVIDER", "rules")
    monkeypatch.setenv("MOUCHEN_LEGACY_AUTH_ENABLED", "false")
    monkeypatch.setenv("MOUCHEN_ANALYSIS_BACKGROUND_ENABLED", "true")
    import app.main as main
    main.repo.close()
    main.repo = main.Repository(tmp_path / "local.db")
    main.service = main.ProactiveService(main.repo)
    session = main.repo.register_account(username="synthetic-owner", password="synthetic-password-only",
                                        device_id="desktop", device_name=None,
                                        expires_at=utc_now() + timedelta(days=1))
    headers = {"Authorization": f"Bearer {session.access_token}"}
    with TestClient(main.app) as client:
        yield client, main, headers, session.principal.user_id


def request(operation, body=None, message_id=None):
    return {"schema": "ai-twin.sync/v1", "kind": "request", "message_id": str(message_id or uuid4()),
            "sender": "phone", "created_at": utc_now().isoformat(), "operation": operation, "body": body or {}}


GOAL = {"domain": "work", "quote": "本周为虚拟项目投入十小时", "title": "虚拟项目", "target": {"weekly_hours": 10}}
EVENT = {"source": "synthetic-phone", "type": "time.allocation", "confidence": .98,
         "facts": {"domain": "work", "actual_hours": 4, "expected_hours": 10}}


def post(ctx, data, **kwargs):
    client, _, headers, _ = ctx
    return client.post("/v1/relay/import", json=data, headers=headers, **kwargs)


def test_goal_event_advice_feedback_round_trip(local_client):
    created = post(local_client, request("goal.create", GOAL)).json()
    assert created["ok"] and len(created["result"]["goals"]) == 1
    evaluated = post(local_client, request("event.create", EVENT)).json()
    assert evaluated["ok"] and len(evaluated["result"]["advice"]) == 1
    advice = evaluated["result"]["advice"][0]
    assert advice["user_id"] == local_client[3]
    assert advice["status"] == "active"
    assert advice["evidence"][0]["source"] == "synthetic-phone"
    receipt = request("feedback.create", {"advice_id": advice["id"], "kind": "adopted", "note": "虚拟反馈"})
    response = post(local_client, receipt)
    assert response.json()["result"]["advice"][0]["status"] == "adopted"
    assert post(local_client, receipt).json() == response.json()
    assert local_client[1].repo._connection.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1


def test_retry_keeps_exact_receipt_and_conflict_is_rejected(local_client):
    payload = request("goal.create", GOAL)
    first = post(local_client, payload)
    post(local_client, request("goal.create", {**GOAL, "title": "Second goal"}))
    assert post(local_client, payload).content == first.content
    changed = {**payload, "body": {**GOAL, "title": "Changed content"}}
    assert post(local_client, changed).status_code == 409
    assert len(local_client[1].repo.list_goals(local_client[3])) == 2


def test_concurrent_retry_and_restart_have_one_business_effect(local_client):
    payload = request("goal.create", GOAL)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: post(local_client, payload).json(), range(8)))
    assert all(item == responses[0] for item in responses)
    main = local_client[1]
    assert len(main.repo.list_goals(local_client[3])) == 1
    path = main.repo.path
    main.repo.close()
    main.repo = main.Repository(path)
    main.service = main.ProactiveService(main.repo)
    assert post(local_client, payload).json() == responses[0]


def test_mutation_and_receipt_rollback_together(local_client, monkeypatch):
    import app.relay_import as imports
    main = local_client[1]
    original = imports._bounded_snapshot
    monkeypatch.setattr(imports, "_bounded_snapshot", lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic crash")))
    payload = request("goal.create", GOAL)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        post(local_client, payload)
    assert main.repo.list_goals(local_client[3]) == []
    assert main.repo._connection.execute("SELECT COUNT(*) FROM relay_receipts").fetchone()[0] == 0
    monkeypatch.setattr(imports, "_bounded_snapshot", original)
    assert post(local_client, payload).json()["ok"]


def test_endpoint_requires_rules_session_and_direct_loopback(local_client, monkeypatch):
    client, main, headers, uid = local_client
    payload = request("snapshot.get")
    assert client.post("/v1/relay/import", json=payload).status_code == 401
    assert client.post("/v1/relay/import", json=payload, headers={**headers, "X-Forwarded-For": "198.51.100.20"}).status_code == 403
    monkeypatch.setenv("MOUCHEN_MODEL_PROVIDER", "auto")
    assert post(local_client, payload).status_code == 403
    monkeypatch.setenv("MOUCHEN_MODEL_PROVIDER", "rules")
    monkeypatch.setenv("MOUCHEN_LEGACY_AUTH_ENABLED", "true")
    monkeypatch.setenv("MOUCHEN_SINGLE_USER_ID", uid)
    assert client.post("/v1/relay/import", json=payload, headers={"X-User-Id": uid}).status_code == 403


def test_tenant_identity_is_server_derived(local_client):
    client, main, headers, uid = local_client
    bad = post(local_client, request("goal.create", {**GOAL, "user_id": "another-user"})).json()
    assert bad["ok"] is False
    payload = request("goal.create", GOAL)
    first = post(local_client, payload).json()
    session = main.repo.register_account(username="synthetic-second", password="synthetic-password-two",
                                        device_id="desktop-two", device_name=None,
                                        expires_at=utc_now() + timedelta(days=1))
    other = {"Authorization": f"Bearer {session.access_token}"}
    second = client.post("/v1/relay/import", json=payload, headers=other).json()
    assert second["result"]["goals"][0]["id"] != first["result"]["goals"][0]["id"]
    assert second["result"]["goals"][0]["user_id"] == session.principal.user_id
    assert len(second["result"]["goals"]) == 1


def test_invalid_and_over_quota_requests_have_stable_error_receipts(local_client, monkeypatch):
    payload = request("event.create", {**EVENT, "facts": {"invalid": "x" * 3000}})
    monkeypatch.setenv("MOUCHEN_MAX_EVENT_FACTS_BYTES", "1024")
    first = post(local_client, payload).json()
    assert first["ok"] is False and first["error"]["code"] == "413"
    assert post(local_client, payload).json() == first
    assert local_client[1].repo.recent_events(local_client[3]) == []
    monkeypatch.setenv("MOUCHEN_MAX_GOALS_PER_USER", "1")
    assert post(local_client, request("goal.create", GOAL)).json()["ok"]
    response = post(local_client, request("goal.create", GOAL)).json()
    assert response["ok"] is False and response["error"]["code"] == "507"
    assert len(local_client[1].repo.list_goals(local_client[3])) == 1


def test_snapshot_and_response_are_bounded(local_client):
    client, main, headers, uid = local_client
    for index in range(105):
        main.repo.insert_goal(Goal(user_id=uid, **{**GOAL, "title": str(index), "target": {"large": "x" * 9000}}))
    response = post(local_client, request("snapshot.get"))
    assert response.status_code == 200 and len(response.content) <= 512 * 1024
    assert response.json()["result"]["truncated"] is True
    assert 0 < len(response.json()["result"]["goals"]) <= 100


def test_unreviewed_warning_stays_held_and_receipts_follow_account_deletion(local_client):
    post(local_client, request("goal.create", GOAL))
    response = post(local_client, request("event.create", {
        **EVENT, "facts": {"domain": "work", "actual_hours": 1, "expected_hours": 10}}))
    assert response.json()["result"]["advice"] == []
    main = local_client[1]
    main.repo.delete_account(user_id=local_client[3], current_password="synthetic-password-only")
    assert main.repo._connection.execute("SELECT COUNT(*) FROM relay_receipts").fetchone()[0] == 0


def test_rules_ignore_cloud_headers_and_do_not_start_consumers(local_client):
    client, main, headers, uid = local_client
    assert main.analysis_consumer is None and main.advice_localization_consumer is None
    headers = {**headers, "X-Proactive-Cloud-Approved": "true", "X-Semantic-Fast-Lane": "true"}
    response = client.post("/v1/events", json={"source": "synthetic", "type": "text.observed", "facts": {"text": "Review this"}}, headers=headers)
    assert response.status_code == 200
    assert response.json()["queued"] is False
    response = client.post("/v1/model/analyze", json={"level": 2, "purpose": "user_question", "prompt": "synthetic", "outbound_approved": True}, headers=headers)
    assert response.status_code == 409


@pytest.mark.parametrize("provider", ["openai", "codex_cli", "ollama", "template"])
def test_rules_gateway_blocks_even_preselected_routes(monkeypatch, provider):
    monkeypatch.setenv("MOUCHEN_MODEL_PROVIDER", "rules")
    with pytest.raises(ModelUnavailable, match="disabled"):
        asyncio.run(ModelGateway().generate(ModelRoute(provider, "synthetic", "routine"), "synthetic", {}))
    with pytest.raises(ModelUnavailable, match="disabled"):
        asyncio.run(ModelGateway().second_opinion("synthetic", {}))
