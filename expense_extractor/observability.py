"""Observability — Foundry/Agent Framework tracing → Application Insights (Phase 5).

When `APPLICATIONINSIGHTS_CONNECTION_STRING` is set, this wires the Agent Framework's
OpenTelemetry spans (agent calls, tool calls, workflow steps) to App Insights, so you
can trace one receipt across Extractor → Validator → Orchestrator and drill from a
failed run down to the exact step (guide §5).

Offline / no connection string → a no-op. `traced("name")` is always safe to use.
"""

from __future__ import annotations

import contextlib

from expense_extractor.config import Settings, get_settings

_configured = False


def enable_observability(settings: Settings | None = None) -> bool:
    """Turn on tracing if a connection string + exporter are available. Idempotent.

    Returns True if tracing was enabled, False if running without it (offline).
    """
    global _configured
    if _configured:
        return True

    settings = settings or get_settings()
    conn = settings.applicationinsights_connection_string
    if not conn:
        return False

    try:
        from agent_framework.observability import configure_otel_providers
        from azure.monitor.opentelemetry.exporter import (
            AzureMonitorLogExporter,
            AzureMonitorMetricExporter,
            AzureMonitorTraceExporter,
        )
    except ImportError:
        # `pip install -e ".[observability]"` to enable.
        return False

    configure_otel_providers(
        exporters=[
            AzureMonitorTraceExporter(connection_string=conn),
            AzureMonitorMetricExporter(connection_string=conn),
            AzureMonitorLogExporter(connection_string=conn),
        ],
        enable_sensitive_data=False,  # never export raw receipt PII into telemetry
    )
    _configured = True
    return True


def traced(name: str, **attributes):
    """Context manager for a parent span tying one receipt's run together.

    Falls back to a no-op span when the framework tracer isn't active.
    """
    try:
        from agent_framework.observability import get_tracer

        return get_tracer().start_as_current_span(name, attributes=attributes or None)
    except Exception:
        return contextlib.nullcontext()
