"""End-to-end workflow-graph tests — the real Agent Framework WorkflowBuilder graph,
running offline. Covers auto-approve, escalate+approve, escalate+reject, reject, and
duplicate detection across runs (guide Phases 3-4 done-criteria)."""

from __future__ import annotations

import hashlib

import pytest

from agents.orchestrator import ApprovalOutcome, ApprovalRequest
from expense_extractor.config import Settings
from expense_extractor.extractor import ImageInput, MockVisionModel, RawExtraction, RawLineItem
from expense_extractor.schemas import RecordStatus, RouteDecision
from tools.notify import LocalNotifier
from tools.stores import LocalRecordStore
from workflow.pipeline import PipelineInput, build_pipeline, run_pipeline


def image(seed: str) -> ImageInput:
    sha = hashlib.sha256(seed.encode()).hexdigest()
    return ImageInput(data_uri="data:image/png;base64,AA==", media_type="image/png", sha256=sha, uri=f"local://{seed}")


def models_for(json_payload: str):
    m = MockVisionModel(default=json_payload, name="gpt-4o-mini")
    return (m, MockVisionModel(default=json_payload, name="gpt-4o"))


def clean_lunch() -> str:
    return RawExtraction(
        vendor="Bistro Nine", expense_date="2026-06-15", category="meals", currency="USD",
        subtotal=38.0, tax=4.0, total=42.0, line_items=[RawLineItem(description="Lunch", amount=38.0)],
        confidence=0.96,
    ).model_dump_json()


def alcohol_dinner() -> str:
    return RawExtraction(
        vendor="Steakhouse", expense_date="2026-06-15", category="meals", currency="USD",
        total=60.0, line_items=[RawLineItem(description="Steak", amount=40.0), RawLineItem(description="Wine", amount=20.0)],
        confidence=0.95,
    ).model_dump_json()


def pricey_hotel() -> str:
    return RawExtraction(
        vendor="Grand Hotel", expense_date="2026-06-15", category="lodging", currency="USD",
        total=500.0, line_items=[RawLineItem(description="1 night", amount=500.0)], confidence=0.95,
    ).model_dump_json()


def not_a_receipt() -> str:
    return RawExtraction(is_expense_document=False, confidence=0.8).model_dump_json()


def _settings() -> Settings:
    return Settings(expense_model_backend="mock", auto_approve_limit="75", base_currency="USD")


@pytest.mark.asyncio
async def test_low_risk_claim_auto_approves_and_posts(tmp_path):
    store = LocalRecordStore(tmp_path / "r.json")
    notifier = LocalNotifier()
    workflow, store, notifier = build_pipeline(
        settings=_settings(), store=store, notifier=notifier, models=models_for(clean_lunch())
    )
    decision = await run_pipeline(workflow, PipelineInput(document=image("clean"), submitter="alice@corp.com"))

    assert decision.approved
    assert decision.route is RouteDecision.AUTO_APPROVED
    assert decision.posted
    records = await store.list_all()
    assert records[0].status is RecordStatus.POSTED
    assert any(n.kind == "approved" for n in notifier.sent)


@pytest.mark.asyncio
async def test_high_risk_escalates_and_resumes_on_approval(tmp_path):
    store = LocalRecordStore(tmp_path / "r.json")
    notifier = LocalNotifier()
    workflow, store, notifier = build_pipeline(
        settings=_settings(), store=store, notifier=notifier, models=models_for(alcohol_dinner())
    )

    approvals: list[ApprovalRequest] = []

    def approver(req: ApprovalRequest) -> ApprovalOutcome:
        approvals.append(req)
        return ApprovalOutcome(approved=True, approver="boss@corp.com", comment="ok this once")

    decision = await run_pipeline(
        workflow, PipelineInput(document=image("alc"), submitter="alice@corp.com"), approver=approver
    )

    assert approvals, "workflow should have paused for human approval"
    assert decision.approved
    assert decision.route is RouteDecision.ESCALATED_APPROVED
    assert decision.approver == "boss@corp.com"
    assert decision.posted


@pytest.mark.asyncio
async def test_escalation_rejected_by_human(tmp_path):
    store = LocalRecordStore(tmp_path / "r.json")
    notifier = LocalNotifier()
    workflow, store, notifier = build_pipeline(
        settings=_settings(), store=store, notifier=notifier, models=models_for(pricey_hotel())
    )

    decision = await run_pipeline(
        workflow, PipelineInput(document=image("hotel"), submitter="alice@corp.com"),
        approver=lambda req: ApprovalOutcome(approved=False, approver="boss@corp.com", comment="over budget"),
    )
    assert not decision.approved
    assert decision.route is RouteDecision.REJECTED
    assert not decision.posted
    assert any(n.kind == "rejected" for n in notifier.sent)


@pytest.mark.asyncio
async def test_non_receipt_is_rejected(tmp_path):
    workflow, store, notifier = build_pipeline(
        settings=_settings(), store=LocalRecordStore(tmp_path / "r.json"),
        notifier=LocalNotifier(), models=models_for(not_a_receipt()),
    )
    decision = await run_pipeline(workflow, PipelineInput(document=image("junk"), submitter="alice@corp.com"))
    assert not decision.approved
    assert decision.route is RouteDecision.REJECTED


@pytest.mark.asyncio
async def test_duplicate_second_submission_escalates(tmp_path):
    # Same store across two runs. Two DIFFERENT documents (different hash) but same
    # submitter + amount + near date → fuzzy duplicate on the second.
    store = LocalRecordStore(tmp_path / "r.json")
    settings = _settings()

    wf1, store, _ = build_pipeline(settings=settings, store=store, notifier=LocalNotifier(), models=models_for(clean_lunch()))
    d1 = await run_pipeline(wf1, PipelineInput(document=image("first"), submitter="alice@corp.com"))
    assert d1.route is RouteDecision.AUTO_APPROVED

    wf2, store, _ = build_pipeline(settings=settings, store=store, notifier=LocalNotifier(), models=models_for(clean_lunch()))
    captured = {}

    def approver(req: ApprovalRequest) -> ApprovalOutcome:
        captured["reasons"] = req.reasons
        return ApprovalOutcome(approved=False, approver="boss@corp.com", comment="dup")

    d2 = await run_pipeline(wf2, PipelineInput(document=image("second"), submitter="alice@corp.com"), approver=approver)
    assert d2.route is RouteDecision.REJECTED
    assert any("duplicate" in r for r in captured.get("reasons", []))
