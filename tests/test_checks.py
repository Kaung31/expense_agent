"""Deterministic-check tests, including the guide's required failing cases:
over-limit meal and total mismatch (guide Phase 2 done-criteria)."""

from __future__ import annotations

from decimal import Decimal

from expense_extractor.schemas import ExpenseCategory
from tests.conftest import make_expense
from tools.checks import (
    StaticFxConverter,
    check_category_caps,
    check_required_fields,
    check_total_vs_items,
)


def test_total_matches_itemized_receipt():
    exp = make_expense(total="30.00", items=[("Burger", "18.00"), ("Fries", "12.00")])
    result = check_total_vs_items(exp)
    assert result.passed, result.detail


def test_total_matches_subtotal_tax_tip():
    exp = make_expense(total="46.20", subtotal="40.00", tax="3.20", tip="3.00")
    assert check_total_vs_items(exp).passed


def test_total_mismatch_is_caught():
    # total (99) != items (30) — a classic manipulated/misread receipt.
    exp = make_expense(total="99.00", items=[("Burger", "18.00"), ("Fries", "12.00")])
    result = check_total_vs_items(exp)
    assert not result.passed
    assert Decimal(result.data["diff"]) == Decimal("69.00")


def test_missing_total_fails():
    exp = make_expense(total=None)
    assert not check_total_vs_items(exp).passed


def test_meal_within_cap_passes():
    exp = make_expense(category=ExpenseCategory.MEALS, total="60.00")
    assert check_category_caps(exp, base_currency="USD").passed


def test_over_limit_meal_is_caught():
    # $120 meal against a $75 per-meal cap must be flagged.
    exp = make_expense(category=ExpenseCategory.MEALS, total="120.00")
    result = check_category_caps(exp, base_currency="USD")
    assert not result.passed
    assert Decimal(result.data["over_by"]) == Decimal("45.00")


def test_foreign_currency_converted_before_cap_check():
    # 70 EUR ≈ 75.60 USD, just over the $75 meal cap after conversion.
    exp = make_expense(category=ExpenseCategory.MEALS, currency="EUR", total="70.00")
    result = check_category_caps(exp, base_currency="USD")
    assert not result.passed, result.detail


def test_fx_converter_roundtrips_base_currency():
    fx = StaticFxConverter()
    assert fx.to_base(Decimal("10.00"), "USD", "USD") == Decimal("10.00")
    assert fx.to_base(Decimal("100.00"), "EUR", "USD") == Decimal("108.00")


def test_required_fields_flags_missing():
    exp = make_expense(vendor="", total=None)
    result = check_required_fields(exp)
    assert not result.passed
    assert set(result.data["missing"]) == {"vendor", "total"}
