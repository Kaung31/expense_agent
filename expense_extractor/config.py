"""Runtime configuration and business policy — all overridable via env, no code edits.

Keeping thresholds, caps, and the model backend in config (not hardcoded in agents)
is what lets the same code run offline (mock) in tests and against Azure in prod.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from expense_extractor.schemas import ExpenseCategory


class ModelBackend(str, Enum):
    MOCK = "mock"              # deterministic, offline — default for tests/CI
    FOUNDRY = "foundry"        # Azure AI Foundry Agent Service (FoundryChatClient)
    AZURE_OPENAI = "azure_openai"


class Settings(BaseSettings):
    """Environment-driven settings. See `.env.example` for the full list."""

    model_config = SettingsConfigDict(
        env_prefix="", env_file=".env", extra="ignore", case_sensitive=False
    )

    # Model backend
    expense_model_backend: ModelBackend = ModelBackend.MOCK
    azure_openai_endpoint: str = ""
    foundry_project_endpoint: str = ""
    foundry_model: str = "gpt-5.4-mini"
    foundry_model_escalation: str = "gpt-5.4"

    # Azure AI Search (policy RAG)
    azure_search_endpoint: str = ""
    azure_search_index: str = "expense-policy"

    # Cosmos / Blob
    cosmos_endpoint: str = ""
    cosmos_database: str = "expenses"
    cosmos_container: str = "records"
    # File path for the local fallback record store (used when COSMOS_ENDPOINT is empty).
    local_store_path: str = ".localstore/records.json"
    blob_account_url: str = ""
    blob_container: str = "receipts"

    # Approvals / ERP
    approval_logic_app_url: str = ""
    erp_post_url: str = ""

    # Web app (Stage 2 async approvals): the app's own public URL, used to build the
    # callback the Logic App invokes when the human clicks; optional shared token.
    public_base_url: str = ""
    approval_callback_token: str = ""

    # Observability
    applicationinsights_connection_string: str = ""

    # Business policy thresholds
    auto_approve_limit: Decimal = Field(default=Decimal("75"))
    base_currency: str = "USD"
    max_agent_iterations: int = 8
    # Receipts older than this escalate to a human (auto-approve requires freshness).
    max_receipt_age_days: int = 90

    @property
    def is_mock(self) -> bool:
        return self.expense_model_backend is ModelBackend.MOCK


@lru_cache
def get_settings() -> Settings:
    return Settings()


# ── Numeric policy caps (deterministic gate; the *narrative* policy is RAG) ──
# Per-item ceiling by category, in the base currency. Over-cap → a risk flag.
DEFAULT_CATEGORY_CAPS: dict[ExpenseCategory, Decimal] = {
    ExpenseCategory.MEALS: Decimal("75"),         # per-meal
    ExpenseCategory.LODGING: Decimal("350"),      # per-night
    ExpenseCategory.TRANSPORT: Decimal("150"),
    ExpenseCategory.AIRFARE: Decimal("1500"),
    ExpenseCategory.CAR_RENTAL: Decimal("120"),
    ExpenseCategory.ENTERTAINMENT: Decimal("200"),
    ExpenseCategory.SUPPLIES: Decimal("500"),
    ExpenseCategory.CONFERENCE: Decimal("2000"),
    ExpenseCategory.TELECOM: Decimal("100"),
    ExpenseCategory.OTHER: Decimal("250"),
}

# Offline FX rates (units of base currency per 1 unit of the key currency).
# Used only when EXPENSE_MODEL_BACKEND=mock; prod wires a real FX source.
MOCK_FX_TO_USD: dict[str, Decimal] = {
    "USD": Decimal("1.00"),
    "EUR": Decimal("1.08"),
    "GBP": Decimal("1.27"),
    "JPY": Decimal("0.0067"),
    "CAD": Decimal("0.73"),
    "AUD": Decimal("0.66"),
    "INR": Decimal("0.012"),
    "CHF": Decimal("1.12"),
    "SGD": Decimal("0.74"),
    "MMK": Decimal("0.00048"),
    "MYR": Decimal("0.21"),
    "THB": Decimal("0.027"),
    "CNY": Decimal("0.14"),
    "KRW": Decimal("0.00072"),
}
