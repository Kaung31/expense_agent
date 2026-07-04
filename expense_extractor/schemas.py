"""Typed contracts for the Expense IDP pipeline.

These Pydantic models are the *only* thing agents pass to each other. The
Extractor emits an ``ExtractionResult``; the Validator consumes ``.expense`` and
emits a ``RiskAssessment``; the Orchestrator consumes both and emits a
``Decision``. Because every handoff is a validated schema, a change in one agent
cannot silently corrupt the next (guide §1).

Money is ``Decimal`` everywhere — never float — so that "total == sum(items)"
is an exact comparison, not a rounding lottery.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ── Enumerations ────────────────────────────────────────────────────────────


class ExpenseCategory(str, Enum):
    MEALS = "meals"
    LODGING = "lodging"
    TRANSPORT = "transport"       # taxi, rideshare, rail, local transit
    AIRFARE = "airfare"
    CAR_RENTAL = "car_rental"
    ENTERTAINMENT = "entertainment"
    SUPPLIES = "supplies"
    CONFERENCE = "conference"
    TELECOM = "telecom"
    OTHER = "other"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ExtractorAction(str, Enum):
    """What the Extractor recommends after reading the document.

    The Extractor never *acts* — it only advises. Routing is the Orchestrator's job.
    """

    PROCEED = "proceed"        # clean read, hand downstream
    REVIEW = "review"          # readable but something is off (missing total, mismatch)
    ESCALATE = "escalate"      # too hard for the small model — re-run on the big one
    REJECT = "reject"          # not an expense document / unusable


class RouteDecision(str, Enum):
    AUTO_APPROVED = "auto_approved"
    PENDING_APPROVAL = "pending_approval"     # HITL pause
    ESCALATED_APPROVED = "escalated_approved"  # human said yes
    REJECTED = "rejected"


class RecordStatus(str, Enum):
    RECEIVED = "received"
    EXTRACTED = "extracted"
    VALIDATED = "validated"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    POSTED = "posted"


# ── Source document ─────────────────────────────────────────────────────────


class DocumentRef(BaseModel):
    """Pointer to the raw receipt/invoice. The bytes live in Blob; we pass a ref."""

    model_config = ConfigDict(extra="forbid")

    uri: str = Field(description="Blob URI or local path to the source document.")
    media_type: str = "image/jpeg"
    page_count: int = 1
    sha256: str | None = Field(default=None, description="Content hash for idempotency/dedupe.")


# ── Extracted expense ───────────────────────────────────────────────────────


class LineItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal = Field(description="Line total in the document's currency.")
    category: ExpenseCategory | None = None


class Expense(BaseModel):
    """The structured facts read off one expense document (original currency)."""

    model_config = ConfigDict(extra="ignore")

    expense_id: str = Field(description="Stable id for this claim (hash of source or provided).")
    submitter: str | None = Field(default=None, description="Employee submitting the claim.")
    cost_center: str | None = None

    vendor: str | None = None
    expense_date: date | None = None
    category: ExpenseCategory = ExpenseCategory.OTHER

    currency: str = Field(default="USD", description="ISO-4217 code as printed on the document.")
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    tip: Decimal | None = None
    total: Decimal | None = Field(default=None, description="Grand total in `currency`.")

    line_items: list[LineItem] = Field(default_factory=list)
    payment_method: str | None = None
    notes: str | None = None

    source: DocumentRef | None = None

    def items_sum(self) -> Decimal | None:
        """Sum of line-item amounts, or None if there are no items."""
        if not self.line_items:
            return None
        return sum((li.amount for li in self.line_items), Decimal("0"))


class ValidationIssue(BaseModel):
    """A concern the *Extractor* noticed while reading (data quality, not policy)."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="Machine code, e.g. 'missing_total', 'blurry', 'total_mismatch'.")
    severity: Severity = Severity.WARNING
    message: str


class ExtractionResult(BaseModel):
    """Extractor output — the first typed handoff (guide §5, Extractor)."""

    model_config = ConfigDict(extra="forbid")

    expense: Expense
    issues: list[ValidationIssue] = Field(default_factory=list)
    action: ExtractorAction = ExtractorAction.PROCEED
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    model: str | None = Field(default=None, description="Deployment that produced this result.")
    escalated: bool = Field(default=False, description="True if re-run on the larger model.")
    extracted_at: datetime = Field(default_factory=_utcnow)

    def has_error(self) -> bool:
        return any(i.severity is Severity.ERROR for i in self.issues)


# ── Risk assessment (Validator) ─────────────────────────────────────────────


class CheckResult(BaseModel):
    """Outcome of one deterministic check — the hard-logic fraud/quality gates."""

    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    detail: str = ""
    data: dict = Field(default_factory=dict)


class PolicyCitation(BaseModel):
    """A real passage from the expense policy index (RAG), so decisions are grounded."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(description="Policy doc id / section the passage came from.")
    passage: str
    score: float | None = None


class RiskFlag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: RiskLevel
    message: str
    data: dict = Field(default_factory=dict)


class RiskAssessment(BaseModel):
    """Validator output — risk + flags + citations (guide §5, Validator)."""

    model_config = ConfigDict(extra="forbid")

    risk: RiskLevel = RiskLevel.LOW
    flags: list[RiskFlag] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    policy_citations: list[PolicyCitation] = Field(default_factory=list)

    computed_total_base: Decimal | None = Field(
        default=None, description="Total converted to the company base currency."
    )
    base_currency: str = "USD"
    duplicate_of: str | None = Field(default=None, description="record id of a prior matching claim")

    model: str | None = None
    assessed_at: datetime = Field(default_factory=_utcnow)

    def add_flag(self, code: str, severity: RiskLevel, message: str, **data) -> None:
        self.flags.append(RiskFlag(code=code, severity=severity, message=message, data=data))


# ── Decision (Orchestrator) ─────────────────────────────────────────────────


class Decision(BaseModel):
    """Orchestrator output — the routing/approval outcome (guide §5, Orchestrator)."""

    model_config = ConfigDict(extra="forbid")

    approved: bool
    route: RouteDecision
    approver: str | None = Field(default=None, description="'auto' or the human approver id.")
    reason: str = ""
    posted: bool = False
    record_id: str | None = None
    decided_at: datetime = Field(default_factory=_utcnow)


# ── Persisted record (Cosmos) ───────────────────────────────────────────────


class ExpenseRecord(BaseModel):
    """The full audit trail for one claim as stored in Cosmos DB."""

    model_config = ConfigDict(extra="ignore")

    id: str
    partition_key: str = Field(description="Usually the submitter, for cheap per-person queries.")
    status: RecordStatus = RecordStatus.RECEIVED

    expense: Expense
    extraction: ExtractionResult | None = None
    risk: RiskAssessment | None = None
    decision: Decision | None = None

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
