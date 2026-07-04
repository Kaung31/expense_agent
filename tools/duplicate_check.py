"""Duplicate detection — a Cosmos lookup for the same claim submitted twice.

Two signals: an exact content-hash match (the identical file re-submitted) and a
fuzzy match (same person + near-identical base-currency amount + near date). This
is a deterministic gate; the LLM never decides whether something is a duplicate.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from expense_extractor.schemas import CheckResult, Expense
from tools.stores import RecordStore


async def check_duplicate(
    expense: Expense,
    store: RecordStore,
    *,
    total_base: Decimal | None = None,
    window_days: int = 3,
) -> CheckResult:
    """Return a CheckResult; when a duplicate is found, its id is in ``data``."""
    # 1) Exact same document (content hash) — strongest signal, order-independent.
    sha = expense.source.sha256 if expense.source else None
    if sha:
        hit = await store.find_by_hash(sha)
        if hit and hit.expense.expense_id != expense.expense_id:
            return CheckResult(
                name="duplicate",
                passed=False,
                detail=f"Identical document already recorded as {hit.id}.",
                data={"duplicate_of": hit.id, "reason": "content_hash"},
            )

    # 2) Fuzzy: same submitter + near amount + near date.
    on: date | None = expense.expense_date
    similar = await store.find_similar(
        submitter=expense.submitter,
        total_base=total_base if total_base is not None else expense.total,
        on_date=on,
        window_days=window_days,
    )
    similar = [r for r in similar if r.expense.expense_id != expense.expense_id]
    if similar:
        first = similar[0]
        return CheckResult(
            name="duplicate",
            passed=False,
            detail=f"Possible duplicate of {first.id} (same submitter, amount, near date).",
            data={"duplicate_of": first.id, "reason": "fuzzy", "candidates": [r.id for r in similar]},
        )

    return CheckResult(name="duplicate", passed=True, detail="No duplicate found.", data={})
