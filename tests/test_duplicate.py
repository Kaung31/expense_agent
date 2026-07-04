"""Duplicate-detection tests (guide Phase 2: duplicate claim must be caught)."""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import make_expense, make_record, sha_of
from tools.duplicate_check import check_duplicate


@pytest.mark.asyncio
async def test_no_duplicate_on_empty_store(store):
    exp = make_expense()
    result = await check_duplicate(exp, store)
    assert result.passed


@pytest.mark.asyncio
async def test_exact_content_hash_duplicate(store):
    sha = sha_of("receipt-bytes")
    first = make_expense(expense_id="exp-1", sha256=sha)
    await store.upsert(make_record(first, record_id="rec-1"))

    resubmit = make_expense(expense_id="exp-2", sha256=sha)
    result = await check_duplicate(resubmit, store)
    assert not result.passed
    assert result.data["duplicate_of"] == "rec-1"
    assert result.data["reason"] == "content_hash"


@pytest.mark.asyncio
async def test_fuzzy_duplicate_same_person_amount_near_date(store):
    first = make_expense(expense_id="exp-1", total="42.00", on=date(2026, 6, 15))
    await store.upsert(make_record(first, record_id="rec-1"))

    near = make_expense(expense_id="exp-2", total="42.00", on=date(2026, 6, 16))
    result = await check_duplicate(near, store)
    assert not result.passed
    assert result.data["reason"] == "fuzzy"


@pytest.mark.asyncio
async def test_different_person_is_not_duplicate(store):
    first = make_expense(expense_id="exp-1", submitter="alice@corp.com", total="42.00")
    await store.upsert(make_record(first, record_id="rec-1"))

    other = make_expense(expense_id="exp-2", submitter="bob@corp.com", total="42.00")
    assert (await check_duplicate(other, store)).passed
