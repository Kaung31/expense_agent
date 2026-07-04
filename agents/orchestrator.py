"""Orchestrator / Approval — the manager's assistant (guide §5, Phase 3).

Routing is **rules-first and deterministic** (guide §8: side-effects never sit
behind the model's say-so):

    action REJECT / extraction error  ─▶ REJECT
    risk HIGH                          ─▶ ESCALATE (human-in-the-loop)
    risk LOW + PROCEED + under limit   ─▶ AUTO-APPROVE
    everything else                    ─▶ ESCALATE

Escalation raises an `ApprovalRequest`. In the workflow this pauses the graph
(RequestInfoExecutor) until a human responds via a Teams card / email; the response
comes back as an `ApprovalOutcome` and `finalize()` turns it into the `Decision`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from decimal import Decimal
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, Field

from expense_extractor.config import Settings, get_settings
from expense_extractor.schemas import (
    Decision,
    ExtractionResult,
    ExtractorAction,
    RiskAssessment,
    RiskLevel,
    RouteDecision,
)

logger = logging.getLogger("expense_idp.approvals")


class RouteKind(str, Enum):
    AUTO_APPROVE = "auto_approve"
    REJECT = "reject"
    ESCALATE = "escalate"


class ApprovalRequest(BaseModel):
    """What a human approver is shown (rendered as a Teams Adaptive Card / email)."""

    record_id: str
    submitter: str | None = None
    vendor: str | None = None
    amount: Decimal | None = None
    currency: str = "USD"
    expense_date: str | None = None
    category: str | None = None
    risk: RiskLevel = RiskLevel.MEDIUM
    reasons: list[str] = Field(default_factory=list)
    approver_hint: str | None = None


class ApprovalOutcome(BaseModel):
    """The human's response coming back into the workflow."""

    approved: bool
    approver: str
    comment: str = ""


class ApprovalGateway(Protocol):
    """Sends the request out and returns the human's decision.

    In the graph workflow this is the RequestInfoExecutor pause/resume; the mock
    gateways below let us test the resume logic without a live Teams channel.
    """

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome: ...


class AutoApproveGateway:
    """Test/dev gateway that approves everything (simulates a human clicking Approve)."""

    def __init__(self, approver: str = "manager@corp.com") -> None:
        self._approver = approver

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        return ApprovalOutcome(approved=True, approver=self._approver, comment="approved (auto gateway)")


class AutoRejectGateway:
    def __init__(self, approver: str = "manager@corp.com") -> None:
        self._approver = approver

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        return ApprovalOutcome(approved=False, approver=self._approver, comment="rejected (auto gateway)")


class ApprovalPendingError(RuntimeError):
    """The human approval channel failed or timed out — the claim must stay pending.

    Deliberately NOT an approval and NOT a rejection: `Orchestrator.orchestrate` maps this
    to a PENDING_APPROVAL decision so a broken Teams channel can never auto-approve a claim.
    """


# HTTP transport shape used by LogicAppApprovalGateway — injectable so tests never hit the
# network: (method, url, body, headers, timeout) -> (status, headers, body_bytes).
HttpCall = Callable[[str, str, bytes | None, dict[str, str], float], tuple[int, dict[str, str], bytes]]


def _urllib_http(method: str, url: str, body: bytes | None, headers: dict[str, str], timeout: float):
    """Default transport (stdlib only). Returns non-2xx statuses instead of raising."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as err:  # non-2xx still carries a response
        return err.code, dict((err.headers or {}).items()), err.read()


def _logic_app_payload(request: ApprovalRequest, **extra) -> bytes:
    return json.dumps({
        "expenseId": request.record_id,
        "merchant": request.vendor or "unknown",
        "total": float(request.amount) if request.amount is not None else 0.0,
        "currency": request.currency,
        "date": request.expense_date or "",
        "category": request.category or "",
        "riskLevel": request.risk.value,
        "riskFlags": request.reasons,
        **extra,
    }).encode()


class LogicAppApprovalGateway:
    """ApprovalGateway backed by the Teams Adaptive Card Logic App (foundry backend).

    POSTs the expense details to APPROVAL_LOGIC_APP_URL. The Logic App posts the card to a
    Teams channel and waits for the click; because that outlives the ~2-minute HTTP window,
    the Logic App uses the asynchronous-response pattern — the POST returns 202 + a Location
    header, and this gateway polls that URL until the human responds (200 + decision JSON)
    or `timeout_seconds` elapses.

    Failure semantics: timeout, non-200, bad payload, or any network error raises
    `ApprovalPendingError` — the claim is left escalated/pending, never auto-approved.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 5.0,
        request_timeout_seconds: float = 60.0,
        http: HttpCall | None = None,
    ) -> None:
        if not url:
            raise ValueError("LogicAppApprovalGateway needs the Logic App trigger URL (APPROVAL_LOGIC_APP_URL).")
        self._url = url
        self._timeout = timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._request_timeout = request_timeout_seconds
        self._http: HttpCall = http or _urllib_http

    async def request_approval(self, request: ApprovalRequest) -> ApprovalOutcome:
        # The blocking HTTP + polling runs in a worker thread so the workflow loop stays live.
        return await asyncio.to_thread(self._request_sync, request)

    def __call__(self, request: ApprovalRequest) -> ApprovalOutcome:
        """Sync adapter, usable directly as `run_pipeline(..., approver=gateway)`."""
        return self._request_sync(request)

    def _payload(self, request: ApprovalRequest) -> bytes:
        return _logic_app_payload(request)

    def _request_sync(self, request: ApprovalRequest) -> ApprovalOutcome:
        try:
            status, headers, body = self._http(
                "POST", self._url, self._payload(request), {"Content-Type": "application/json"},
                self._request_timeout,
            )
            deadline = time.monotonic() + self._timeout
            # Async-response pattern: 202 + Location until the human clicks.
            while status == 202:
                location = {k.lower(): v for k, v in headers.items()}.get("location")
                if not location:
                    raise ApprovalPendingError("Logic App returned 202 without a Location header to poll.")
                if time.monotonic() >= deadline:
                    raise ApprovalPendingError(
                        f"Timed out after {self._timeout:.0f}s waiting for the Teams approver."
                    )
                time.sleep(self._poll_interval)
                status, headers, body = self._http("GET", location, None, {}, self._request_timeout)

            if status != 200:
                raise ApprovalPendingError(f"Logic App returned HTTP {status}.")

            data = json.loads(body.decode() or "{}")
            decision = str(data.get("decision", "")).lower()
            if decision not in ("approve", "reject"):
                raise ApprovalPendingError(f"Unexpected decision payload from Logic App: {data!r}")

            approver = data.get("approver") or "teams-approver"
            responded_at = data.get("respondedAt", "")
            return ApprovalOutcome(
                approved=decision == "approve",
                approver=approver,
                comment=f"via Teams card{f' at {responded_at}' if responded_at else ''}",
            )
        except ApprovalPendingError:
            raise
        except Exception as exc:  # network/JSON/etc. — never auto-approve on failure
            raise ApprovalPendingError(f"Approval channel failure: {exc}") from exc


def build_approval_gateway(settings: Settings | None = None) -> ApprovalGateway | None:
    """Teams-card gateway for the foundry backend; None for mock (offline/tests unchanged)."""
    settings = settings or get_settings()
    if settings.is_mock or not settings.approval_logic_app_url:
        return None
    return LogicAppApprovalGateway(settings.approval_logic_app_url)


def _amount_base(extraction: ExtractionResult, risk: RiskAssessment) -> Decimal:
    if risk.computed_total_base is not None:
        return risk.computed_total_base
    return extraction.expense.total or Decimal("0")


def decide_route(
    extraction: ExtractionResult, risk: RiskAssessment, *, auto_approve_limit: Decimal
) -> tuple[RouteKind, str]:
    """Pure routing decision — no side effects, fully testable."""
    if extraction.action is ExtractorAction.REJECT or extraction.has_error():
        return RouteKind.REJECT, "Document unusable or not an expense (extractor rejected)."

    if risk.risk is RiskLevel.HIGH:
        return RouteKind.ESCALATE, "High-risk claim requires human approval."

    amount = _amount_base(extraction, risk)
    if (
        risk.risk is RiskLevel.LOW
        and extraction.action is ExtractorAction.PROCEED
        and amount <= auto_approve_limit
    ):
        return RouteKind.AUTO_APPROVE, f"Low risk, {amount} {risk.base_currency} within limit {auto_approve_limit}."

    if amount > auto_approve_limit:
        return RouteKind.ESCALATE, f"Amount {amount} {risk.base_currency} over auto-approve limit {auto_approve_limit}."
    return RouteKind.ESCALATE, f"Medium risk or needs review (action={extraction.action.value})."


def build_approval_request(record_id: str, extraction: ExtractionResult, risk: RiskAssessment) -> ApprovalRequest:
    expense = extraction.expense
    return ApprovalRequest(
        record_id=record_id,
        submitter=expense.submitter,
        vendor=expense.vendor,
        amount=_amount_base(extraction, risk),
        currency=risk.base_currency,
        expense_date=expense.expense_date.isoformat() if expense.expense_date else None,
        category=expense.category.value,
        risk=risk.risk,
        reasons=[f"{f.code}: {f.message}" for f in risk.flags],
    )


class Orchestrator:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def propose(self, extraction: ExtractionResult, risk: RiskAssessment) -> tuple[RouteKind, str]:
        return decide_route(extraction, risk, auto_approve_limit=self.settings.auto_approve_limit)

    def finalize(
        self,
        route: RouteKind,
        reason: str,
        *,
        record_id: str | None = None,
        approval: ApprovalOutcome | None = None,
    ) -> Decision:
        """Turn a route (+ optional human outcome) into the final Decision."""
        if route is RouteKind.AUTO_APPROVE:
            return Decision(approved=True, route=RouteDecision.AUTO_APPROVED, approver="auto",
                            reason=reason, record_id=record_id)

        if route is RouteKind.REJECT:
            return Decision(approved=False, route=RouteDecision.REJECTED, approver="policy",
                            reason=reason, record_id=record_id)

        # ESCALATE
        if approval is None:
            # No human response yet — the workflow is paused here (HITL).
            return Decision(approved=False, route=RouteDecision.PENDING_APPROVAL, approver=None,
                            reason=reason, record_id=record_id)
        if approval.approved:
            return Decision(approved=True, route=RouteDecision.ESCALATED_APPROVED, approver=approval.approver,
                            reason=approval.comment or reason, record_id=record_id)
        return Decision(approved=False, route=RouteDecision.REJECTED, approver=approval.approver,
                        reason=approval.comment or "Rejected by approver.", record_id=record_id)

    async def orchestrate(
        self,
        extraction: ExtractionResult,
        risk: RiskAssessment,
        *,
        record_id: str | None = None,
        gateway: ApprovalGateway | None = None,
    ) -> Decision:
        """End-to-end decision, calling the approval gateway for escalations.

        Used directly in tests/scripts; the graph workflow uses `propose`/`finalize`
        with a RequestInfoExecutor so the pause survives long waits (checkpointing).
        """
        route, reason = self.propose(extraction, risk)
        if route is RouteKind.ESCALATE and gateway is not None:
            request = build_approval_request(record_id or extraction.expense.expense_id, extraction, risk)
            try:
                outcome = await gateway.request_approval(request)
            except ApprovalPendingError as exc:
                # Broken/timed-out approval channel must NEVER auto-approve: leave pending.
                logger.warning(
                    "Approval channel failed for %s — claim stays pending (not auto-approved): %s",
                    request.record_id, exc,
                )
                return self.finalize(route, f"{reason} [approval pending: {exc}]", record_id=record_id)
            return self.finalize(route, reason, record_id=record_id, approval=outcome)
        return self.finalize(route, reason, record_id=record_id)
