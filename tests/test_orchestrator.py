"""Orchestrator tests (guide Phase 3): auto-approve easy path, escalate + resume."""

from __future__ import annotations

from decimal import Decimal

from agents.orchestrator import (
    ApprovalOutcome,
    Orchestrator,
    RouteKind,
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


def settings(limit: str = "75") -> Settings:
    return Settings(expense_model_backend="mock", auto_approve_limit=Decimal(limit), base_currency="USD")


def result(action=ExtractorAction.PROCEED, total="42.00") -> ExtractionResult:
    return ExtractionResult(expense=make_expense(total=total), action=action)


def risk(level=RiskLevel.LOW, total_base="42.00") -> RiskAssessment:
    return RiskAssessment(risk=level, base_currency="USD", computed_total_base=Decimal(total_base))


def test_low_risk_small_amount_auto_approves():
    orch = Orchestrator(settings())
    route, reason = orch.propose(result(), risk())
    assert route is RouteKind.AUTO_APPROVE
    decision = orch.finalize(route, reason, record_id="rec-1")
    assert decision.approved and decision.route is RouteDecision.AUTO_APPROVED
    assert decision.approver == "auto"


def test_reject_when_extractor_rejects():
    orch = Orchestrator(settings())
    route, reason = orch.propose(result(action=ExtractorAction.REJECT), risk())
    assert route is RouteKind.REJECT
    decision = orch.finalize(route, reason)
    assert not decision.approved and decision.route is RouteDecision.REJECTED


def test_high_risk_escalates():
    orch = Orchestrator(settings())
    route, _ = orch.propose(result(), risk(level=RiskLevel.HIGH))
    assert route is RouteKind.ESCALATE


def test_low_risk_over_limit_escalates():
    orch = Orchestrator(settings("75"))
    route, reason = orch.propose(result(total="500.00"), risk(total_base="500.00"))
    assert route is RouteKind.ESCALATE
    assert "over auto-approve limit" in reason


def test_pending_when_no_human_response_yet():
    orch = Orchestrator(settings())
    decision = orch.finalize(RouteKind.ESCALATE, "needs approval", record_id="rec-1")
    assert not decision.approved
    assert decision.route is RouteDecision.PENDING_APPROVAL


def test_escalation_finalizes_approved():
    orch = Orchestrator(settings())
    outcome = ApprovalOutcome(approved=True, approver="boss@corp.com", comment="ok")
    decision = orch.finalize(RouteKind.ESCALATE, "needs approval", record_id="rec-1", approval=outcome)
    assert decision.approved
    assert decision.route is RouteDecision.ESCALATED_APPROVED
    assert decision.approver == "boss@corp.com"


def test_escalation_finalizes_rejected():
    orch = Orchestrator(settings())
    outcome = ApprovalOutcome(approved=False, approver="boss@corp.com", comment="no")
    decision = orch.finalize(RouteKind.ESCALATE, "needs approval", record_id="rec-1", approval=outcome)
    assert not decision.approved
    assert decision.route is RouteDecision.REJECTED
    assert decision.approver == "boss@corp.com"
