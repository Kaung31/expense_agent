"""The backbone workflow graph (guide §1 "Workflow graph", Phases 3-4).

A fixed pipeline built with Microsoft Agent Framework's `WorkflowBuilder`:

    Ingest ─▶ Validate ─▶ Decide ─┬─ auto_approve ─▶ ApproveNode ─▶ (post+notify)
                                  ├─ reject       ─▶ RejectNode
                                  └─ escalate     ─▶ ApprovalNode  (HITL pause/resume)

The conditional edge is a real `add_switch_case_edge_group`. Escalations pause the
graph via `ctx.request_info` and resume on the human's `ApprovalOutcome`
(`@response_handler`) — so an approval that waits hours doesn't lose state.

The custom executors call our provider-agnostic agent classes, so the entire graph
runs offline in mock mode (no Azure needed) yet is the exact production topology.
"""

# NOTE: no `from __future__ import annotations` here — the Agent Framework's
# @response_handler validator inspects raw annotations and needs real types, not
# stringized ones. All types below are defined/imported before use.

from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Never

from agent_framework import (
    Case,
    Default,
    Executor,
    WorkflowBuilder,
    WorkflowContext,
    handler,
    response_handler,
)

from agents.orchestrator import (
    ApprovalOutcome,
    ApprovalRequest,
    Orchestrator,
    RouteKind,
    build_approval_request,
)
from agents.validator import Validator, build_validator
from expense_extractor.agent import build_extractor_models
from expense_extractor.config import Settings, get_settings
from expense_extractor.extractor import ImageInput, extract, load_image
from expense_extractor.schemas import (
    Decision,
    ExpenseRecord,
    ExtractionResult,
    RecordStatus,
    RiskAssessment,
)
from tools.erp_post import post_to_erp
from tools.notify import LocalNotifier, LogicAppNotifier, Notification, Notifier
from tools.stores import RecordStore, build_store

# ── Messages passed along the graph edges ────────────────────────────────────


@dataclass
class PipelineInput:
    document: str | ImageInput      # path/URI or a preloaded image
    submitter: str | None = None


@dataclass
class Claim:
    """The working object threaded through the graph (one per receipt)."""

    record_id: str
    extraction: ExtractionResult
    risk: RiskAssessment | None = None
    route: RouteKind | None = None
    reason: str = ""


def _now() -> datetime:
    return datetime.now(UTC)


# ── Executors ─────────────────────────────────────────────────────────────────


class IngestExecutor(Executor):
    """Vision-extract the document and open its audit record."""

    def __init__(self, primary, escalation, store: RecordStore, settings: Settings) -> None:
        super().__init__(id="ingest")
        self._primary = primary
        self._escalation = escalation
        self._store = store
        self._settings = settings

    @handler
    async def run(self, inp: PipelineInput, ctx: WorkflowContext[Claim]) -> None:
        image = inp.document if isinstance(inp.document, ImageInput) else load_image(inp.document)
        extraction = await extract(
            image, self._primary, escalation_model=self._escalation, submitter=inp.submitter
        )
        expense = extraction.expense
        record = ExpenseRecord(
            id=expense.expense_id,
            partition_key=expense.submitter or "unknown",
            status=RecordStatus.EXTRACTED,
            expense=expense,
            extraction=extraction,
        )
        await self._store.upsert(record)
        await ctx.send_message(Claim(record_id=record.id, extraction=extraction))


class ValidateExecutor(Executor):
    """Score risk against deterministic checks + policy RAG."""

    def __init__(self, validator: Validator, store: RecordStore) -> None:
        super().__init__(id="validate")
        self._validator = validator
        self._store = store

    @handler
    async def run(self, claim: Claim, ctx: WorkflowContext[Claim]) -> None:
        risk = await self._validator.validate(claim.extraction, self._store)
        claim.risk = risk
        record = await self._store.get(claim.record_id)
        if record:
            record.risk = risk
            record.status = RecordStatus.VALIDATED
            record.updated_at = _now()
            await self._store.upsert(record)
        await ctx.send_message(claim)


class DecideExecutor(Executor):
    """Rules-first routing decision (no side effects here)."""

    def __init__(self, orchestrator: Orchestrator) -> None:
        super().__init__(id="decide")
        self._orch = orchestrator

    @handler
    async def run(self, claim: Claim, ctx: WorkflowContext[Claim]) -> None:
        assert claim.risk is not None
        route, reason = self._orch.propose(claim.extraction, claim.risk)
        claim.route = route
        claim.reason = reason
        await ctx.send_message(claim)


async def apply_decision(
    decision: Decision,
    claim: Claim,
    store: RecordStore,
    notifier: Notifier,
    settings: Settings,
) -> Decision:
    """Apply a finalized Decision: post to ERP, notify the submitter, update the record.

    Module-level so the web layer's approval callback can finish a claim through the
    exact same code path as the graph's terminal nodes (no duplicated logic), even when
    the paused in-memory workflow no longer exists (e.g. after a container restart).
    """
    record = await store.get(claim.record_id)
    submitter = claim.extraction.expense.submitter or "unknown"

    if decision.approved:
        if record:
            record.decision = decision
            record.status = RecordStatus.APPROVED
        result = await post_to_erp(record, settings.erp_post_url) if record else None
        if result and result.posted:
            decision.posted = True
            if record:
                record.status = RecordStatus.POSTED
            await notifier.notify(Notification(
                to=submitter, kind="approved",
                subject="Expense approved & posted",
                body=f"Your claim {claim.record_id} was approved ({decision.approver}) and posted "
                     f"as {result.voucher_id}.",
            ))
    else:
        if record:
            record.decision = decision
            record.status = RecordStatus.REJECTED
        await notifier.notify(Notification(
            to=submitter, kind="rejected",
            subject="Expense rejected",
            body=f"Your claim {claim.record_id} was rejected: {decision.reason}",
        ))

    if record:
        record.updated_at = _now()
        await store.upsert(record)
    return decision


class _TerminalBase(Executor):
    """Shared __init__ for terminal nodes (post/notify)."""

    def __init__(self, id: str, orchestrator: Orchestrator, store: RecordStore, notifier: Notifier,
                 settings: Settings) -> None:
        super().__init__(id=id)
        self._orch = orchestrator
        self._store = store
        self._notifier = notifier
        self._settings = settings


class ApproveExecutor(_TerminalBase):
    """Auto-approve path: approve, post to ERP, notify. Terminal."""

    @handler
    async def run(self, claim: Claim, ctx: WorkflowContext[Never, Decision]) -> None:
        decision = self._orch.finalize(RouteKind.AUTO_APPROVE, claim.reason, record_id=claim.record_id)
        decision = await apply_decision(decision, claim, self._store, self._notifier, self._settings)
        await ctx.yield_output(decision)


class RejectExecutor(_TerminalBase):
    """Reject path: mark rejected, notify submitter. Terminal."""

    @handler
    async def run(self, claim: Claim, ctx: WorkflowContext[Never, Decision]) -> None:
        decision = self._orch.finalize(RouteKind.REJECT, claim.reason, record_id=claim.record_id)
        decision = await apply_decision(decision, claim, self._store, self._notifier, self._settings)
        await ctx.yield_output(decision)


class ApprovalExecutor(_TerminalBase):
    """Escalation path: pause for a human (HITL), then finalize on their response."""

    @handler
    async def request(self, claim: Claim, ctx: WorkflowContext[Never, Decision]) -> None:
        record = await self._store.get(claim.record_id)
        if record:
            record.status = RecordStatus.PENDING_APPROVAL
            record.updated_at = _now()
            await self._store.upsert(record)
        request = build_approval_request(claim.record_id, claim.extraction, claim.risk)  # type: ignore[arg-type]
        # Pause the graph until a human responds with an ApprovalOutcome.
        await ctx.request_info(request_data=request, response_type=ApprovalOutcome)

    @response_handler
    async def on_response(
        self, request: ApprovalRequest, outcome: ApprovalOutcome, ctx: WorkflowContext[Never, Decision]
    ) -> None:
        decision = self._orch.finalize(
            RouteKind.ESCALATE, "human-in-the-loop decision", record_id=request.record_id, approval=outcome
        )
        # Rebuild a minimal Claim from the persisted record so _apply can post/notify.
        record = await self._store.get(request.record_id)
        claim = Claim(record_id=request.record_id, extraction=record.extraction, risk=record.risk)  # type: ignore
        decision = await apply_decision(decision, claim, self._store, self._notifier, self._settings)
        await ctx.yield_output(decision)


# ── Builder + runner ──────────────────────────────────────────────────────────


def build_pipeline(
    *,
    settings: Settings | None = None,
    store: RecordStore | None = None,
    notifier: Notifier | None = None,
    models: tuple | None = None,
):
    """Assemble the workflow graph. Returns (workflow, store, notifier).

    `models` (primary, escalation) lets tests inject specific mock vision models;
    otherwise they are built from settings.
    """
    settings = settings or get_settings()
    store = store or build_store(settings)
    if notifier is None:
        notifier = (
            LogicAppNotifier(settings.approval_logic_app_url)
            if settings.approval_logic_app_url
            else LocalNotifier()
        )

    primary, escalation = models if models is not None else build_extractor_models(settings)
    validator = build_validator(settings)
    orchestrator = Orchestrator(settings)

    ingest = IngestExecutor(primary, escalation, store, settings)
    validate = ValidateExecutor(validator, store)
    decide = DecideExecutor(orchestrator)
    approve = ApproveExecutor("approve", orchestrator, store, notifier, settings)
    reject = RejectExecutor("reject", orchestrator, store, notifier, settings)
    approval = ApprovalExecutor("approval", orchestrator, store, notifier, settings)

    def is_route(kind: RouteKind):
        return lambda m: isinstance(m, Claim) and m.route is kind

    workflow = (
        WorkflowBuilder(start_executor=ingest)
        .add_edge(ingest, validate)
        .add_edge(validate, decide)
        .add_switch_case_edge_group(
            decide,
            [
                Case(condition=is_route(RouteKind.AUTO_APPROVE), target=approve),
                Case(condition=is_route(RouteKind.REJECT), target=reject),
                Default(target=approval),
            ],
        )
        .build()
    )
    return workflow, store, notifier


async def run_pipeline(
    workflow,
    pipeline_input: PipelineInput,
    *,
    approver: Callable[[ApprovalRequest], ApprovalOutcome] | None = None,
) -> Decision | None:
    """Drive the workflow, answering any HITL approval request via `approver`.

    Mirrors the Agent Framework HITL pattern: run with stream=True, capture
    `request_info` events, resume with `run(responses=..., stream=True)`.
    """
    decision: Decision | None = None
    stream: AsyncIterable = workflow.run(pipeline_input, stream=True)

    while True:
        pending: list[tuple[str, ApprovalRequest]] = []
        async for event in stream:
            if event.type == "request_info":
                pending.append((event.request_id, event.data))
            elif event.type == "output":
                decision = event.data

        if not pending:
            return decision

        responses = {}
        for request_id, request in pending:
            outcome = (
                approver(request)
                if approver
                else ApprovalOutcome(approved=False, approver="system", comment="no approver configured")
            )
            responses[request_id] = outcome
        stream = workflow.run(stream=True, responses=responses)
