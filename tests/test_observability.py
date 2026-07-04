"""Observability wiring — must be a safe no-op offline (no connection string)."""

from __future__ import annotations

from expense_extractor.config import Settings
from expense_extractor.observability import enable_observability, traced


def test_disabled_without_connection_string():
    assert enable_observability(Settings(applicationinsights_connection_string="")) is False


def test_traced_span_is_usable_offline():
    # Should not raise whether or not an exporter is configured.
    with traced("unit-test-span", foo="bar"):
        pass
