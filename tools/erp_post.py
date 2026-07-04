"""ERP / GL post — the final side-effect after approval (guide Phase 4).

Idempotent by design: posting the same record id twice returns the same voucher,
so a retried receipt never double-posts (guide §8). A stub function is fine for the
demo; set ERP_POST_URL to hit a real GL endpoint later.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from expense_extractor.schemas import ExpenseRecord


@dataclass
class PostResult:
    posted: bool
    voucher_id: str
    detail: str


def _voucher_id(record_id: str) -> str:
    # Deterministic id from the record → idempotent: same record ⇒ same voucher.
    return "GL-" + hashlib.sha256(record_id.encode()).hexdigest()[:12].upper()


async def post_to_erp(record: ExpenseRecord, url: str = "") -> PostResult:
    """Post an approved claim to the general ledger.

    With no URL configured this is a deterministic stub (good for demos/tests).
    A real implementation POSTs to `url` and returns the ledger's voucher id.
    """
    if not (record.decision and record.decision.approved):
        return PostResult(posted=False, voucher_id="", detail="Claim not approved; refusing to post.")

    voucher = _voucher_id(record.id)

    if not url:
        return PostResult(posted=True, voucher_id=voucher, detail="Stub GL post (no ERP_POST_URL set).")

    # Real path (wired in the Azure phase; kept import-light here on purpose).
    import json
    import urllib.request

    payload = {
        "voucher_id": voucher,
        "submitter": record.expense.submitter,
        "vendor": record.expense.vendor,
        "amount": str(record.expense.total),
        "currency": record.expense.currency,
        "category": record.expense.category.value,
        "record_id": record.id,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        body = resp.read().decode()
    return PostResult(posted=True, voucher_id=voucher, detail=f"Posted to ERP: {body[:200]}")
