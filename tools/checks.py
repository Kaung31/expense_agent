"""Deterministic checks — the hard-logic gates the Validator runs *before* any LLM.

Rule from the guide (§5): never let the LLM be the sole fraud gate. These pure
functions are fully unit-testable offline and produce `CheckResult`s that feed the
`RiskAssessment`.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from expense_extractor.config import DEFAULT_CATEGORY_CAPS, MOCK_FX_TO_USD
from expense_extractor.schemas import CheckResult, Expense, ExpenseCategory

_CENTS = Decimal("0.01")


def _round(amount: Decimal) -> Decimal:
    return amount.quantize(_CENTS, rounding=ROUND_HALF_UP)


# ── FX conversion ───────────────────────────────────────────────────────────


class FxConverter(Protocol):
    def to_base(self, amount: Decimal, currency: str, base_currency: str) -> Decimal: ...


class StaticFxConverter:
    """Table-driven converter. Offline default; prod swaps in a live-rate source."""

    def __init__(self, rates_to_usd: dict[str, Decimal] | None = None) -> None:
        self._rates = rates_to_usd or MOCK_FX_TO_USD

    def to_base(self, amount: Decimal, currency: str, base_currency: str) -> Decimal:
        cur = (currency or base_currency).upper()
        base = base_currency.upper()
        if cur == base:
            return _round(amount)
        if cur not in self._rates or base not in self._rates:
            raise KeyError(f"No FX rate for {cur}->{base}")
        usd = amount * self._rates[cur]                 # to USD
        return _round(usd / self._rates[base])          # USD to base


# ── Checks ──────────────────────────────────────────────────────────────────


def check_total_vs_items(expense: Expense, tolerance: Decimal = _CENTS) -> CheckResult:
    """total should equal subtotal+tax+tip, and (when itemized) the sum of items."""
    items_sum = expense.items_sum()
    total = expense.total

    if total is None:
        return CheckResult(
            name="total_vs_items",
            passed=False,
            detail="No grand total present on the document.",
            data={"total": None, "items_sum": str(items_sum) if items_sum is not None else None},
        )

    # Prefer subtotal+tax+tip when available; fall back to items sum.
    parts = [p for p in (expense.subtotal, expense.tax, expense.tip) if p is not None]
    computed = sum(parts, Decimal("0")) if expense.subtotal is not None else items_sum

    if computed is None:
        return CheckResult(
            name="total_vs_items",
            passed=True,
            detail="Only a grand total present; nothing to reconcile against.",
            data={"total": str(total)},
        )

    diff = abs(_round(total) - _round(computed))
    passed = diff <= tolerance
    return CheckResult(
        name="total_vs_items",
        passed=passed,
        detail=(
            "Total reconciles with components."
            if passed
            else f"Total {total} != components {computed} (diff {diff})."
        ),
        data={"total": str(total), "computed": str(computed), "diff": str(diff)},
    )


def check_category_caps(
    expense: Expense,
    base_currency: str,
    fx: FxConverter | None = None,
    caps: dict[ExpenseCategory, Decimal] | None = None,
) -> CheckResult:
    """Compare the claim's total (in base currency) against the per-category cap."""
    fx = fx or StaticFxConverter()
    caps = caps or DEFAULT_CATEGORY_CAPS
    cap = caps.get(expense.category)

    if expense.total is None or cap is None:
        return CheckResult(
            name="category_cap",
            passed=True,
            detail="No total or no cap for this category; skipped.",
            data={"category": expense.category.value, "cap": str(cap) if cap else None},
        )

    try:
        total_base = fx.to_base(expense.total, expense.currency, base_currency)
    except KeyError:
        # Unknown currency: the cap can't be verified, so the claim must NOT pass
        # silently — fail the check (→ escalates to a human) instead of crashing.
        return CheckResult(
            name="category_cap",
            passed=False,
            detail=f"No FX rate for {expense.currency} — cap cannot be verified; needs review.",
            data={"category": expense.category.value, "currency": expense.currency, "error": "fx_unknown"},
        )
    passed = total_base <= cap
    return CheckResult(
        name="category_cap",
        passed=passed,
        detail=(
            f"{expense.category.value} {total_base} {base_currency} within cap {cap}."
            if passed
            else f"{expense.category.value} {total_base} {base_currency} exceeds cap {cap}."
        ),
        data={
            "category": expense.category.value,
            "total_base": str(total_base),
            "cap": str(cap),
            "over_by": str(_round(total_base - cap)) if not passed else "0",
        },
    )


def check_receipt_age(expense: Expense, max_age_days: int = 90, today: date | None = None) -> CheckResult:
    """Reject staleness at the gate: old receipts (and future-dated ones) need a human.

    No date → skipped here; `required_fields` already fails and escalates that case.
    """
    if expense.expense_date is None:
        return CheckResult(
            name="receipt_age", passed=True,
            detail="No date to check; handled by required_fields.", data={},
        )

    today = today or date.today()
    age_days = (today - expense.expense_date).days

    if age_days < 0:
        return CheckResult(
            name="receipt_age", passed=False,
            detail=f"Receipt is dated {-age_days} day(s) in the FUTURE ({expense.expense_date}) — misread or fraud.",
            data={"age_days": age_days, "max_age_days": max_age_days, "error": "future_date"},
        )
    if age_days > max_age_days:
        return CheckResult(
            name="receipt_age", passed=False,
            detail=f"Receipt is {age_days} days old ({expense.expense_date}); policy window is {max_age_days} days.",
            data={"age_days": age_days, "max_age_days": max_age_days, "error": "stale_receipt"},
        )
    return CheckResult(
        name="receipt_age", passed=True,
        detail=f"Receipt is {age_days} days old — within the {max_age_days}-day window.",
        data={"age_days": age_days, "max_age_days": max_age_days},
    )


def check_required_fields(expense: Expense) -> CheckResult:
    """A claim needs at least a total, a date, and a vendor to be auto-processable."""
    missing = [
        name
        for name, val in (("total", expense.total), ("expense_date", expense.expense_date), ("vendor", expense.vendor))
        if val in (None, "")
    ]
    return CheckResult(
        name="required_fields",
        passed=not missing,
        detail="All required fields present." if not missing else f"Missing: {', '.join(missing)}.",
        data={"missing": missing},
    )
