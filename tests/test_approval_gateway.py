"""LogicAppApprovalGateway tests — Teams-card approval mapping, fully offline.

The HTTP transport is injected (FakeHttp), so no real Logic App is ever hit. Covers:
approve→approved=True, reject→approved=False, the 202+Location async-response polling
pattern, and the fail-safe (failure/timeout NEVER auto-approves — claim stays pending).
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from agents.orchestrator import (
    ApprovalPendingError,
    LogicAppApprovalGateway,
    Orchestrator,
    build_approval_gateway,
)
from expense_extractor.config import Settings
from expense_extractor.schemas import (
    ExtractionResult,
    ExtractorAction,
    RiskAssessment,
    RiskLevel,
    RouteDecision,
)
from tests.conftest import make_expense

URL = "https://logic.azure.com/workflows/fake/triggers/manual/paths/invoke?sig=x"


class FakeHttp:
    """Scripted (status, headers, body) responses; records every call made."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, bytes | None]] = []

    def __call__(self, method, url, body, headers, timeout):
        self.calls.append((method, url, body))
        return self.responses.pop(0)


def _body(decision: str) -> bytes:
    return json.dumps(
        {"decision": decision, "approver": "boss@corp.com", "respondedAt": "2026-07-02T10:00:00Z"}
    ).encode()


def _request():
    from agents.orchestrator import ApprovalRequest

    return ApprovalRequest(
        record_id="rec-1", vendor="Cafe Roma", amount=Decimal("31.32"), currency="USD",
        expense_date="2026-06-20", category="meals", risk=RiskLevel.MEDIUM,
        reasons=["over_cap: meals over limit"],
    )


@pytest.mark.asyncio
async def test_logic_app_approve_maps_to_approved_true():
    http = FakeHttp([(200, {}, _body("approve"))])
    gateway = LogicAppApprovalGateway(URL, http=http, poll_interval_seconds=0)

    outcome = await gateway.request_approval(_request())

    assert outcome.approved is True
    assert outcome.approver == "boss@corp.com"
    # the POST carried the expense details the card renders
    method, url, posted = http.calls[0]
    assert (method, url) == ("POST", URL)
    payload = json.loads(posted)
    assert payload["expenseId"] == "rec-1"
    assert payload["merchant"] == "Cafe Roma"
    assert payload["total"] == 31.32
    assert payload["riskLevel"] == "medium"
    assert payload["riskFlags"] == ["over_cap: meals over limit"]


@pytest.mark.asyncio
async def test_logic_app_reject_maps_to_approved_false():
    http = FakeHttp([(200, {}, _body("reject"))])
    gateway = LogicAppApprovalGateway(URL, http=http, poll_interval_seconds=0)

    outcome = await gateway.request_approval(_request())

    assert outcome.approved is False
    assert outcome.approver == "boss@corp.com"


@pytest.mark.asyncio
async def test_async_202_location_polling_until_human_clicks():
    # Consumption async-response pattern: POST → 202+Location, poll → 202, poll → 200.
    http = FakeHttp([
        (202, {"Location": "https://logic.azure.com/runs/1"}, b""),
        (202, {"Location": "https://logic.azure.com/runs/1"}, b""),
        (200, {}, _body("approve")),
    ])
    gateway = LogicAppApprovalGateway(URL, http=http, poll_interval_seconds=0)

    outcome = await gateway.request_approval(_request())

    assert outcome.approved is True
    assert http.calls[0][0] == "POST"
    assert http.calls[1] == ("GET", "https://logic.azure.com/runs/1", None)
    assert http.calls[2] == ("GET", "https://logic.azure.com/runs/1", None)


@pytest.mark.asyncio
async def test_failure_never_auto_approves_claim_stays_pending():
    # Gateway level: non-200 raises ApprovalPendingError (no silent approve OR reject).
    gateway = LogicAppApprovalGateway(URL, http=FakeHttp([(500, {}, b"boom")]), poll_interval_seconds=0)
    with pytest.raises(ApprovalPendingError):
        await gateway.request_approval(_request())

    # Orchestrator level: the same failure leaves the claim PENDING, approved=False.
    extraction = ExtractionResult(expense=make_expense(total="500.00"), action=ExtractorAction.PROCEED)
    risk = RiskAssessment(risk=RiskLevel.HIGH, base_currency="USD", computed_total_base=Decimal("500.00"))
    orch = Orchestrator(Settings(expense_model_backend="mock", auto_approve_limit=Decimal("75")))
    failing = LogicAppApprovalGateway(URL, http=FakeHttp([(500, {}, b"boom")]), poll_interval_seconds=0)

    decision = await orch.orchestrate(extraction, risk, record_id="rec-1", gateway=failing)

    assert decision.approved is False
    assert decision.route is RouteDecision.PENDING_APPROVAL  # pending, NOT rejected/approved


def test_gateway_factory_mock_backend_returns_none_azure_returns_gateway():
    # mock backend → None (offline runs/tests keep the local approver, unchanged)
    assert build_approval_gateway(Settings(expense_model_backend="mock", approval_logic_app_url=URL)) is None
    # foundry backend without a URL → None; with URL → the Teams gateway
    assert build_approval_gateway(Settings(expense_model_backend="foundry", approval_logic_app_url="")) is None
    gw = build_approval_gateway(Settings(expense_model_backend="foundry", approval_logic_app_url=URL))
    assert isinstance(gw, LogicAppApprovalGateway)
