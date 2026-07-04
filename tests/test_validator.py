"""Validator tests (guide Phase 2 done-criteria)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from agents.orchestrator import Orchestrator, RouteKind
from agents.validator import PolicyFinding, Validator
from expense_extractor.config import Settings
from expense_extractor.extractor import ImageInput, RawExtraction, build_expense
from expense_extractor.schemas import (
    ExpenseCategory,
    ExtractionResult,
    ExtractorAction,
    RiskLevel,
    RouteDecision,
    Severity,
    ValidationIssue,
)
from tests.conftest import make_expense, make_record


def _settings(limit: str = "75") -> Settings:
    return Settings(expense_model_backend="mock", auto_approve_limit=Decimal(limit), base_currency="USD")


class ReceiptRequiredJudge:
    """Simulates the LLM judge over-flagging 'receipt required' regardless of presence —
    used to prove the validator's deterministic gate suppresses it when a receipt exists."""

    name = "receipt-flagger"

    async def judge(self, expense, citations):
        return [
            PolicyFinding(
                code="RECEIPT_REQUIRED",
                severity=RiskLevel.MEDIUM,
                message="Expense is USD 25 or more, so an itemized receipt is required.",
                cite_source="receipts-required",
            )
        ]


def extraction_for(expense, issues=None) -> ExtractionResult:
    return ExtractionResult(expense=expense, issues=issues or [], action=ExtractorAction.PROCEED)


@pytest.mark.asyncio
async def test_clean_claim_is_low_risk(store):
    exp = make_expense(category=ExpenseCategory.MEALS, total="42.00",
                       items=[("Lunch", "38.00")], subtotal="38.00", tax="4.00")
    assessment = await Validator().validate(extraction_for(exp), store)
    assert assessment.risk is RiskLevel.LOW
    assert assessment.computed_total_base is not None
    assert not [f for f in assessment.flags]


@pytest.mark.asyncio
async def test_over_limit_meal_raises_risk(store):
    exp = make_expense(category=ExpenseCategory.MEALS, total="120.00")
    assessment = await Validator().validate(extraction_for(exp), store)
    assert assessment.risk in (RiskLevel.MEDIUM, RiskLevel.HIGH)
    assert any(f.code == "over_cap" for f in assessment.flags)


@pytest.mark.asyncio
async def test_total_mismatch_flagged(store):
    exp = make_expense(total="99.00", items=[("a", "18.00"), ("b", "12.00")])
    assessment = await Validator().validate(extraction_for(exp), store)
    assert any(f.code == "total_mismatch" for f in assessment.flags)
    assert assessment.risk is RiskLevel.HIGH  # 99 vs 30 is a >25% mismatch


@pytest.mark.asyncio
async def test_duplicate_is_high_risk(store):
    first = make_expense(expense_id="e1", total="42.00", on=date(2026, 6, 15))
    await store.upsert(make_record(first, record_id="rec-1"))
    second = make_expense(expense_id="e2", total="42.00", on=date(2026, 6, 16))

    assessment = await Validator().validate(extraction_for(second), store)
    assert assessment.risk is RiskLevel.HIGH
    assert assessment.duplicate_of == "rec-1"
    assert any(f.code == "duplicate" for f in assessment.flags)


@pytest.mark.asyncio
async def test_alcohol_on_standard_cost_center_high_with_citation(store):
    exp = make_expense(category=ExpenseCategory.MEALS, total="60.00",
                       items=[("Dinner", "40.00"), ("Glass of wine", "20.00")])
    exp.cost_center = "engineering"
    assessment = await Validator().validate(extraction_for(exp), store)
    assert assessment.risk is RiskLevel.HIGH
    alcohol = [f for f in assessment.flags if f.code == "alcohol"]
    assert alcohol and alcohol[0].data.get("cite") == "alcohol"
    assert any(c.source == "alcohol" for c in assessment.policy_citations)


@pytest.mark.asyncio
async def test_alcohol_on_entertainment_cost_center_is_softer(store):
    exp = make_expense(category=ExpenseCategory.ENTERTAINMENT, total="60.00",
                       items=[("Client dinner", "40.00"), ("Wine", "20.00")])
    exp.cost_center = "client-entertainment"
    assessment = await Validator().validate(extraction_for(exp), store)
    alcohol = [f for f in assessment.flags if f.code == "alcohol"]
    assert alcohol and alcohol[0].severity is RiskLevel.MEDIUM


@pytest.mark.asyncio
async def test_injection_issue_becomes_medium_flag(store):
    exp = make_expense(total="42.00", items=[("Lunch", "42.00")])
    issues = [ValidationIssue(code="possible_injection", severity=Severity.WARNING,
                              message="doc tried to instruct the reader")]
    assessment = await Validator().validate(extraction_for(exp, issues), store)
    assert any(f.code == "possible_injection" for f in assessment.flags)


# ── Receipt-required false-positive regression suite ─────────────────────────


@pytest.mark.asyncio
async def test_small_valid_receipt_auto_approves(store):
    """A small, low-risk USD claim WITH a valid extracted receipt must auto-approve,
    even if the policy judge tries to raise a 'receipt required' finding."""
    exp = make_expense(vendor="Cafe Roma", on=date(2026, 6, 20), total="31.32",
                       category=ExpenseCategory.MEALS)
    validator = Validator(policy_judge=ReceiptRequiredJudge())  # simulate the LLM over-flag
    assessment = await validator.validate(extraction_for(exp), store)

    # the spurious receipt finding must be suppressed (receipt IS present)
    assert not any("receipt" in f.code.lower() for f in assessment.flags), assessment.flags
    assert assessment.risk is RiskLevel.LOW

    orch = Orchestrator(_settings("75"))
    route, reason = orch.propose(extraction_for(exp), assessment)
    assert route is RouteKind.AUTO_APPROVE
    decision = orch.finalize(route, reason)
    assert decision.approved and decision.route is RouteDecision.AUTO_APPROVED


@pytest.mark.asyncio
async def test_missing_receipt_still_escalates(store):
    """A claim with NO usable receipt (missing merchant + date + total) must still be
    flagged and escalate to a human — the gate only relaxes when a receipt is present."""
    exp = make_expense(vendor="", on=None, total=None, category=ExpenseCategory.MEALS)
    validator = Validator(policy_judge=ReceiptRequiredJudge())
    assessment = await validator.validate(extraction_for(exp), store)

    assert any(f.code == "receipt_missing" for f in assessment.flags), assessment.flags
    assert assessment.risk in (RiskLevel.MEDIUM, RiskLevel.HIGH)

    orch = Orchestrator(_settings("75"))
    route, _ = orch.propose(extraction_for(exp), assessment)
    assert route is not RouteKind.AUTO_APPROVE  # escalates, not auto-approved


@pytest.mark.asyncio
async def test_foreign_currency_receipt(store):
    """A non-USD claim with a valid receipt: currency detected as EUR, amount converted to
    base currency BEFORE the limit check, and routed correctly."""
    exp = make_expense(vendor="Boulangerie", on=date(2026, 6, 20), currency="EUR",
                       total="20.00", category=ExpenseCategory.MEALS)
    assert exp.currency == "EUR"

    assessment = await Validator().validate(extraction_for(exp), store)
    # 20 EUR * 1.08 = 21.60 USD — conversion happened before any limit check
    assert assessment.computed_total_base == Decimal("21.60")
    assert assessment.risk is RiskLevel.LOW

    orch = Orchestrator(_settings("75"))
    route, _ = orch.propose(extraction_for(exp), assessment)
    assert route is RouteKind.AUTO_APPROVE  # 21.60 USD < 75 limit


def test_ddmmyyyy_date_parses():
    """A DD/MM/YYYY receipt date parses to the correct ISO date, not a flipped month."""
    raw = RawExtraction(vendor="Cafe", expense_date="20/06/2026", currency="USD", total=10.0)
    img = ImageInput(data_uri="data:image/png;base64,AA==", media_type="image/png",
                     sha256="a" * 64, uri="local://r")
    exp = build_expense(raw, img, submitter="alice@corp.com")
    assert exp.expense_date == date(2026, 6, 20)


@pytest.mark.asyncio
async def test_unknown_currency_escalates_instead_of_crashing(store):
    """Regression: a currency missing from the FX table (e.g. a rare one) must not
    crash the validator — it flags fx_unknown and the claim needs human review."""
    exp = make_expense(vendor="Mystery Vendor", currency="XXX", total="500.00",
                       category=ExpenseCategory.MEALS, on=date(2026, 6, 20))
    assessment = await Validator().validate(extraction_for(exp), store)  # must not raise
    assert any(f.code == "fx_unknown" for f in assessment.flags), assessment.flags
    assert assessment.risk in (RiskLevel.MEDIUM, RiskLevel.HIGH)
    assert assessment.computed_total_base is None
    # cap check reports the problem instead of silently passing
    cap = next(c for c in assessment.checks if c.name == "category_cap")
    assert not cap.passed and cap.data.get("error") == "fx_unknown"


@pytest.mark.asyncio
async def test_myr_receipt_converts_and_goes_over_cap(store):
    """The Mitasu case: 780.45 MYR (~164 USD) meal converts and exceeds the $75 cap."""
    exp = make_expense(vendor="Mitasu Japanese Restaurant", currency="MYR", total="780.45",
                       category=ExpenseCategory.MEALS, on=date(2026, 6, 29))
    assessment = await Validator().validate(extraction_for(exp), store)
    assert assessment.computed_total_base == Decimal("163.89")
    assert any(f.code == "over_cap" for f in assessment.flags)
    assert assessment.risk in (RiskLevel.MEDIUM, RiskLevel.HIGH)


# ── Receipt staleness gate (auto-approve requires freshness) ─────────────────


@pytest.mark.asyncio
async def test_stale_receipt_escalates_not_auto_approved(store):
    """An old receipt under the amount limit must NOT auto-approve — freshness is a gate."""
    from datetime import timedelta
    old = date.today() - timedelta(days=200)
    exp = make_expense(vendor="Grand Lux Cafe", total="69.25", on=old,
                       category=ExpenseCategory.MEALS)
    assessment = await Validator().validate(extraction_for(exp), store)

    stale = [f for f in assessment.flags if f.code == "stale_receipt"]
    assert stale and stale[0].data["age_days"] == 200
    assert assessment.risk in (RiskLevel.MEDIUM, RiskLevel.HIGH)
    # the policy passage backing the flag is attached
    assert any(c.source == "submission-deadline" for c in assessment.policy_citations)

    orch = Orchestrator(_settings("75"))
    route, _ = orch.propose(extraction_for(exp), assessment)
    assert route is not RouteKind.AUTO_APPROVE


@pytest.mark.asyncio
async def test_future_dated_receipt_is_flagged(store):
    from datetime import timedelta
    exp = make_expense(total="20.00", on=date.today() + timedelta(days=5))
    assessment = await Validator().validate(extraction_for(exp), store)
    assert any(f.code == "future_date" for f in assessment.flags), assessment.flags
    assert assessment.risk in (RiskLevel.MEDIUM, RiskLevel.HIGH)


@pytest.mark.asyncio
async def test_fresh_receipt_still_auto_approves(store):
    """Guard against over-correction: a receipt inside the window stays LOW risk."""
    from datetime import timedelta
    exp = make_expense(vendor="Cafe Roma", total="31.32",
                       on=date.today() - timedelta(days=10), category=ExpenseCategory.MEALS)
    assessment = await Validator().validate(extraction_for(exp), store)
    assert not any(f.code in ("stale_receipt", "future_date") for f in assessment.flags)
    assert assessment.risk is RiskLevel.LOW
    route, _ = Orchestrator(_settings("75")).propose(extraction_for(exp), assessment)
    assert route is RouteKind.AUTO_APPROVE


@pytest.mark.asyncio
async def test_boundary_exactly_max_age_passes(store):
    from datetime import timedelta
    exp = make_expense(total="20.00", on=date.today() - timedelta(days=90))
    assessment = await Validator().validate(extraction_for(exp), store)
    assert not any(f.code == "stale_receipt" for f in assessment.flags)
