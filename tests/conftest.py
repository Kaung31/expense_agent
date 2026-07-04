"""Shared test fixtures and factories."""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

import pytest

from expense_extractor.schemas import (
    DocumentRef,
    Expense,
    ExpenseCategory,
    ExpenseRecord,
    LineItem,
    RecordStatus,
)
from tools.stores import LocalRecordStore


def make_expense(
    *,
    expense_id: str = "exp-1",
    submitter: str = "alice@corp.com",
    vendor: str = "Bistro Nine",
    category: ExpenseCategory = ExpenseCategory.MEALS,
    currency: str = "USD",
    total: str | None = "42.00",
    subtotal: str | None = None,
    tax: str | None = None,
    tip: str | None = None,
    on: date | None = date(2026, 6, 15),
    items: list[tuple[str, str]] | None = None,
    sha256: str | None = None,
) -> Expense:
    line_items = [LineItem(description=d, amount=Decimal(a)) for d, a in (items or [])]
    return Expense(
        expense_id=expense_id,
        submitter=submitter,
        vendor=vendor,
        category=category,
        currency=currency,
        total=Decimal(total) if total is not None else None,
        subtotal=Decimal(subtotal) if subtotal is not None else None,
        tax=Decimal(tax) if tax is not None else None,
        tip=Decimal(tip) if tip is not None else None,
        expense_date=on,
        line_items=line_items,
        source=DocumentRef(uri="local://receipt.jpg", sha256=sha256),
    )


def make_record(expense: Expense, record_id: str | None = None) -> ExpenseRecord:
    return ExpenseRecord(
        id=record_id or expense.expense_id,
        partition_key=expense.submitter or "unknown",
        status=RecordStatus.VALIDATED,
        expense=expense,
    )


def sha_of(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def store(tmp_path) -> LocalRecordStore:
    return LocalRecordStore(path=tmp_path / "records.json")
