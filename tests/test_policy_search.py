"""Policy RAG tests — the Validator must be able to cite a real passage."""

from __future__ import annotations

import pytest

from tools.policy_search import LocalPolicySearch


@pytest.mark.asyncio
async def test_alcohol_query_returns_alcohol_policy():
    citations = await LocalPolicySearch().search("is alcohol reimbursable for this cost center?")
    assert citations
    assert citations[0].source == "alcohol"
    assert "not reimbursable" in citations[0].passage.lower()


@pytest.mark.asyncio
async def test_receipt_threshold_is_citable():
    citations = await LocalPolicySearch().search("do I need an itemized receipt for this amount?")
    sources = {c.source for c in citations}
    assert "receipts-required" in sources


@pytest.mark.asyncio
async def test_unrelated_query_returns_nothing():
    assert await LocalPolicySearch().search("quarterly rocket telemetry cadence") == []
