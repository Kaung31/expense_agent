"""Web layer tests — FastAPI endpoints over the real pipeline graph, fully offline.

Uses injected MockVisionModels (canned payloads) and a tmp LocalRecordStore; no network,
no Azure. Covers: upload→auto-approve, upload→escalate→queue→approve/reject resume,
the Logic App async callback mapping, restart recovery (store fallback), and stats.

Note: clients are context-managed so all requests in a test share ONE event loop —
matching uvicorn, where a paused workflow is resumed on the same loop it started on.
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from expense_extractor.config import Settings
from expense_extractor.extractor import MockVisionModel, RawExtraction, RawLineItem
from tools.notify import LocalNotifier
from tools.stores import LocalRecordStore
from webapp.main import create_app
from webapp.service import PipelineService


def _settings(**overrides) -> Settings:
    return Settings(expense_model_backend="mock", auto_approve_limit="75", base_currency="USD", **overrides)


def clean_lunch() -> str:
    return RawExtraction(
        vendor="Bistro Nine", expense_date="2026-06-15", category="meals", currency="USD",
        subtotal=38.0, tax=4.0, total=42.0, line_items=[RawLineItem(description="Lunch", amount=38.0)],
        confidence=0.96,
    ).model_dump_json()


def pricey_hotel() -> str:
    return RawExtraction(
        vendor="Grand Hotel", expense_date="2026-06-15", category="lodging", currency="USD",
        total=500.0, line_items=[RawLineItem(description="1 night", amount=500.0)], confidence=0.95,
    ).model_dump_json()


@contextmanager
def make_client(tmp_path, payload: str, store: LocalRecordStore | None = None,
                settings: Settings | None = None):
    store = store or LocalRecordStore(tmp_path / "records.json")
    models = (MockVisionModel(default=payload, name="m"), MockVisionModel(default=payload, name="m-esc"))
    service = PipelineService(settings=settings or _settings(), store=store,
                              notifier=LocalNotifier(), models=models)
    with TestClient(create_app(service)) as client:
        yield client, service, store


def upload(client: TestClient, content: bytes = b"img-1", name: str = "r.png"):
    return client.post(
        "/api/expenses",
        files={"file": (name, content, "image/png")},
        data={"submitter": "alice@corp.com"},
    )


def test_health(tmp_path):
    with make_client(tmp_path, clean_lunch()) as (client, _, _):
        res = client.get("/health")
        assert res.status_code == 200
        assert res.json()["backend"] == "mock"


def test_upload_clean_receipt_auto_approves_and_shows_in_history(tmp_path):
    with make_client(tmp_path, clean_lunch()) as (client, _, _):
        res = upload(client)
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "auto_approved"
        assert body["record"]["status"] == "posted"
        assert body["record"]["vendor"] == "Bistro Nine"
        assert body["record"]["decision"]["approved"] is True

        history = client.get("/api/expenses").json()
        assert len(history) == 1 and history[0]["status"] == "posted"
        detail = client.get(f"/api/expenses/{history[0]['id']}").json()
        assert detail["risk"]["level"] == "low"
        assert any(c["name"] == "duplicate" for c in detail["risk"]["checks"])


def test_escalation_appears_in_queue_and_approve_resumes(tmp_path):
    with make_client(tmp_path, pricey_hotel()) as (client, _, _):
        res = upload(client)
        assert res.json()["status"] == "pending_approval"

        queue = client.get("/api/approvals").json()
        assert len(queue) == 1
        record_id = queue[0]["id"]
        assert queue[0]["resumable_in_memory"] is True

        res = client.post(f"/api/approvals/{record_id}/decide",
                          json={"approved": True, "approver": "boss@corp.com"})
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "escalated_approved"
        assert body["record"]["status"] == "posted"
        assert body["record"]["decision"]["approver"] == "boss@corp.com"
        assert client.get("/api/approvals").json() == []  # queue drained


def test_reject_from_queue(tmp_path):
    with make_client(tmp_path, pricey_hotel()) as (client, _, _):
        upload(client)
        record_id = client.get("/api/approvals").json()[0]["id"]
        res = client.post(f"/api/approvals/{record_id}/decide",
                          json={"approved": False, "approver": "boss@corp.com", "comment": "over budget"})
        assert res.json()["status"] == "rejected"
        assert res.json()["record"]["decision"]["posted"] is False


def test_logic_app_callback_maps_approve_and_reject(tmp_path):
    # approve
    with make_client(tmp_path, pricey_hotel()) as (client, _, _):
        upload(client)
        record_id = client.get("/api/approvals").json()[0]["id"]
        res = client.post(f"/api/approvals/{record_id}/callback",
                          json={"decision": "approve", "approver": "teams@corp.com",
                                "respondedAt": "2026-07-02T10:00:00Z"})
        assert res.status_code == 200
        assert res.json()["status"] == "escalated_approved"
        assert res.json()["record"]["decision"]["approver"] == "teams@corp.com"

    # reject (fresh app/store)
    with make_client(tmp_path / "b", pricey_hotel()) as (client2, _, _):
        upload(client2, content=b"img-2")
        record_id2 = client2.get("/api/approvals").json()[0]["id"]
        res2 = client2.post(f"/api/approvals/{record_id2}/callback",
                            json={"decision": "reject", "approver": "teams@corp.com"})
        assert res2.json()["status"] == "rejected"

        # invalid decision → 422 (already-decided claims produce 409)
        assert client2.post(f"/api/approvals/{record_id2}/callback",
                            json={"decision": "maybe"}).status_code in (409, 422)


def test_callback_token_enforced_when_configured(tmp_path):
    settings = _settings(approval_callback_token="s3cret")
    with make_client(tmp_path, pricey_hotel(), settings=settings) as (client, _, _):
        upload(client)
        record_id = client.get("/api/approvals").json()[0]["id"]

        assert client.post(f"/api/approvals/{record_id}/callback",
                           json={"decision": "approve"}).status_code == 401
        assert client.post(f"/api/approvals/{record_id}/callback",
                           json={"decision": "approve"},
                           headers={"X-Callback-Token": "s3cret"}).status_code == 200


def test_restart_recovery_finalizes_from_store(tmp_path):
    """Container scaled to zero mid-approval: new process, same store → decide still works."""
    store = LocalRecordStore(tmp_path / "records.json")
    with make_client(tmp_path, pricey_hotel(), store=store) as (client1, _, _):
        upload(client1)
        record_id = client1.get("/api/approvals").json()[0]["id"]

    # "Restart": a brand-new service sharing only the persistent store.
    with make_client(tmp_path, pricey_hotel(), store=store) as (client2, _, _):
        queue = client2.get("/api/approvals").json()
        assert queue and queue[0]["resumable_in_memory"] is False

        res = client2.post(f"/api/approvals/{record_id}/decide",
                           json={"approved": True, "approver": "boss@corp.com"})
        assert res.status_code == 200
        assert res.json()["status"] == "escalated_approved"
        assert res.json()["record"]["status"] == "posted"

        # deciding twice → 409 (already decided)
        res2 = client2.post(f"/api/approvals/{record_id}/decide",
                            json={"approved": True, "approver": "boss@corp.com"})
        assert res2.status_code == 409


def test_stats_dashboard_shape(tmp_path):
    with make_client(tmp_path, clean_lunch()) as (client, _, _):
        upload(client)
        stats = client.get("/api/stats").json()
        assert stats["total_claims"] == 1
        assert stats["by_status"].get("posted") == 1
        assert stats["spend_by_category"].get("meals") == 42.0
        assert stats["approval_rate"] == 1.0
        assert stats["base_currency"] == "USD"


def test_empty_upload_rejected(tmp_path):
    with make_client(tmp_path, clean_lunch()) as (client, _, _):
        assert upload(client, content=b"").status_code == 400


def test_unknown_record_404(tmp_path):
    with make_client(tmp_path, clean_lunch()) as (client, _, _):
        assert client.get("/api/expenses/nope").status_code == 404
        assert client.post("/api/approvals/nope/decide",
                           json={"approved": True}).status_code == 404
