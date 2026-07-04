"""Orchestrator / Approval — the manager's assistant (guide §5, Phase 3).

Routing is **rules-first and deterministic** (guide §8: side-effects never sit
behind the model's say-so):

    action REJECT / extraction error  ─▶ REJECT
    risk HIGH                          ─▶ ESCALATE (human-in-the-loop)
    risk LOW + PROCEED + under limit   ─▶ AUTO-APPROVE
    everything else                    ─▶ ESCALATE

Escalation raises an `ApprovalRequest`. In the workflow this pauses the graph
(RequestInfoExecutor) until a human responds via the web queue or a Teams card;
the response comes back as an `ApprovalOutcome` and `finalize()` turns it into
the `Decision`.
"""

from __future__ import annotations

import json
from decimal import Decimal
from enum import Enum

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


def _logic_app_payload(request: ApprovalRequest, **extra) -> bytes:
    """Serialize an ApprovalRequest into the Teams-card Logic App's expected JSON.

    Used by the web layer to ask the Logic App to post the card and call back
    (async approval pattern) when the human clicks Approve/Reject.
    """
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
