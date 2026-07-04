"""PipelineService — the server-side state and pipeline driver behind the API.

Runs the *existing* WorkflowBuilder graph per submission. Escalations pause via the
graph's request_info; the pause is held server-side in `self._pending` (workflow object
+ request id) so a browser click or a Logic App callback can resume it. If the process
restarted meanwhile (e.g. Container Apps scaled to zero), `decide()` falls back to
finalizing from the persisted record via the pipeline's own `apply_decision` — same
code path as the graph's terminal nodes, no duplicated logic.

No browser storage anywhere: every piece of state lives here or in the RecordStore.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import urllib.request
from dataclasses import dataclass
from typing import Any

from agents.orchestrator import ApprovalOutcome, ApprovalRequest, Orchestrator, RouteKind, _logic_app_payload
from expense_extractor.config import ModelBackend, Settings, get_settings
from expense_extractor.extractor import ImageInput, RawExtraction, RawLineItem, _media_type_for
from expense_extractor.schemas import Decision, ExpenseRecord, RecordStatus
from tools.notify import LocalNotifier, Notifier
from tools.stores import RecordStore, build_store
from workflow.pipeline import Claim, PipelineInput, apply_decision, build_pipeline

logger = logging.getLogger("expense_idp.webapp")


def image_from_bytes(data: bytes, filename: str) -> ImageInput:
    """Build the extractor's ImageInput from an uploaded file (no temp file needed)."""
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ".jpg"
    media = _media_type_for(suffix)
    return ImageInput(
        data_uri=f"data:{media};base64,{base64.b64encode(data).decode()}",
        media_type=media,
        sha256=hashlib.sha256(data).hexdigest(),
        uri=f"upload://{filename}",
    )


class DemoVisionModel:
    """Offline demo model: derives a *varied* but deterministic receipt from the file hash.

    Only used when EXPENSE_MODEL_BACKEND=mock so the local web demo shows different
    vendors/amounts/currencies per file (some auto-approve, some escalate) without
    touching the canned `MockVisionModel` the tests rely on.
    """

    _VENDORS = ["Bistro Nine", "Grand Hotel", "City Cabs", "Office Depot", "Steakhouse 21", "Cafe Roma"]
    _CATEGORIES = ["meals", "lodging", "transport", "supplies", "entertainment", "meals"]

    def __init__(self, name: str = "demo-mock") -> None:
        self.name = name

    async def run(self, system: str, user: str, image: ImageInput) -> str:
        h = int(image.sha256[:8], 16)
        idx = h % len(self._VENDORS)
        amount = round(12 + (h % 9000) / 100, 2)          # 12.00 – 101.99
        currency = "EUR" if h % 5 == 0 else "USD"
        day = 1 + (h % 27)
        items = [RawLineItem(description=f"{self._CATEGORIES[idx]} purchase", amount=amount)]
        if h % 7 == 0:  # occasionally include alcohol so the policy path shows up
            wine = round(amount * 0.3, 2)
            items = [
                RawLineItem(description="Dinner", amount=round(amount - wine, 2)),
                RawLineItem(description="Glass of wine", amount=wine),
            ]
        raw = RawExtraction(
            vendor=self._VENDORS[idx],
            expense_date=f"2026-06-{day:02d}",
            category=self._CATEGORIES[idx],
            currency=currency,
            total=amount,
            line_items=items,
            confidence=0.95,
        )
        return raw.model_dump_json()


@dataclass
class PendingApproval:
    """A paused workflow waiting for a human decision."""

    workflow: Any
    request_id: str
    request: ApprovalRequest


class PipelineService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        store: RecordStore | None = None,
        notifier: Notifier | None = None,
        models: tuple | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.store = store or build_store(self.settings)
        self.notifier = notifier or LocalNotifier(echo=True)
        self._models: tuple | None
        if models is not None:
            self._models = models
        elif self.settings.expense_model_backend is ModelBackend.MOCK:
            demo = DemoVisionModel()
            self._models = (demo, DemoVisionModel(name="demo-mock-escalation"))
        else:
            self._models = None  # build_pipeline builds the real Foundry models
        self._pending: dict[str, PendingApproval] = {}
        self._orchestrator = Orchestrator(self.settings)

    # ── Submit ──────────────────────────────────────────────────────────────

    async def submit(self, data: bytes, filename: str, submitter: str) -> dict:
        """Run one receipt through the graph until a decision or a HITL pause."""
        image = image_from_bytes(data, filename)
        kwargs: dict[str, Any] = {"settings": self.settings, "store": self.store, "notifier": self.notifier}
        if self._models is not None:
            kwargs["models"] = self._models
        workflow, _, _ = build_pipeline(**kwargs)

        decision: Decision | None = None
        paused: PendingApproval | None = None
        stream = workflow.run(PipelineInput(document=image, submitter=submitter), stream=True)
        async for event in stream:
            if event.type == "request_info" and isinstance(event.data, ApprovalRequest):
                paused = PendingApproval(workflow=workflow, request_id=event.request_id, request=event.data)
            elif event.type == "output" and isinstance(event.data, Decision):
                decision = event.data

        record_id = image.sha256[:16]
        if paused is not None:
            self._pending[paused.request.record_id] = paused
            await self._notify_logic_app(paused.request)
            return {"status": "pending_approval", "record": await self._record_json(paused.request.record_id)}
        if decision is not None:
            return {"status": decision.route.value, "record": await self._record_json(record_id)}
        return {"status": "error", "record": await self._record_json(record_id)}

    # ── Approvals ───────────────────────────────────────────────────────────

    async def pending_approvals(self) -> list[dict]:
        """Escalated claims awaiting a decision (from the store — survives restarts)."""
        out = []
        for rec in await self.store.list_all():
            if rec.status is RecordStatus.PENDING_APPROVAL:
                item = _record_to_json(rec)
                item["resumable_in_memory"] = rec.id in self._pending
                out.append(item)
        out.sort(key=lambda r: r["updated_at"], reverse=True)
        return out

    async def decide(self, record_id: str, approved: bool, approver: str, comment: str = "") -> dict:
        """Resume a paused claim with the human's decision (UI click or Logic App callback)."""
        outcome = ApprovalOutcome(approved=approved, approver=approver, comment=comment)
        pending = self._pending.pop(record_id, None)

        if pending is not None:
            # Primary path: resume the actual paused graph (same as demo.py/run_pipeline).
            decision: Decision | None = None
            stream = pending.workflow.run(stream=True, responses={pending.request_id: outcome})
            async for event in stream:
                if event.type == "output" and isinstance(event.data, Decision):
                    decision = event.data
            if decision is None:
                raise RuntimeError(f"Workflow for {record_id} resumed but yielded no decision.")
            return {"status": decision.route.value, "record": await self._record_json(record_id)}

        # Fallback: the paused workflow is gone (process restarted / scaled to zero).
        # Finalize from the persisted record through the pipeline's own apply_decision.
        record = await self.store.get(record_id)
        if record is None:
            raise KeyError(f"No record {record_id}.")
        if record.status is not RecordStatus.PENDING_APPROVAL:
            raise ValueError(f"Claim {record_id} is not pending approval (status={record.status.value}).")
        logger.info("Resuming %s from store (in-memory workflow not present).", record_id)
        decision = self._orchestrator.finalize(
            RouteKind.ESCALATE, "human-in-the-loop decision (recovered from store)",
            record_id=record_id, approval=outcome,
        )
        claim = Claim(record_id=record_id, extraction=record.extraction, risk=record.risk)  # type: ignore[arg-type]
        decision = await apply_decision(decision, claim, self.store, self.notifier, self.settings)
        return {"status": decision.route.value, "record": await self._record_json(record_id)}

    # ── History / stats ─────────────────────────────────────────────────────

    async def history(self) -> list[dict]:
        records = [_record_to_json(r) for r in await self.store.list_all()]
        records.sort(key=lambda r: r["updated_at"], reverse=True)
        return records

    async def stats(self) -> dict:
        records = await self.store.list_all()
        by_category: dict[str, float] = {}
        by_status: dict[str, int] = {}
        flagged = 0
        approved = rejected = 0
        for rec in records:
            by_status[rec.status.value] = by_status.get(rec.status.value, 0) + 1
            if rec.risk and rec.risk.flags:
                flagged += 1
            if rec.status in (RecordStatus.APPROVED, RecordStatus.POSTED):
                approved += 1
                has_base = rec.risk is not None and rec.risk.computed_total_base is not None
                amount = rec.risk.computed_total_base if has_base else rec.expense.total  # type: ignore[union-attr]
                if amount is not None:
                    cat = rec.expense.category.value
                    by_category[cat] = round(by_category.get(cat, 0.0) + float(amount), 2)
            elif rec.status is RecordStatus.REJECTED:
                rejected += 1
        decided = approved + rejected
        return {
            "total_claims": len(records),
            "by_status": by_status,
            "spend_by_category": by_category,
            "approval_rate": round(approved / decided, 3) if decided else None,
            "flagged_count": flagged,
            "base_currency": self.settings.base_currency,
        }

    # ── Helpers ─────────────────────────────────────────────────────────────

    async def _record_json(self, record_id: str) -> dict | None:
        rec = await self.store.get(record_id)
        return _record_to_json(rec) if rec else None

    async def _notify_logic_app(self, request: ApprovalRequest) -> None:
        """Fire-and-forget: ask the Teams-card Logic App to collect the approval and call
        us back at /api/approvals/{id}/callback (Stage 2 async pattern). No-op locally."""
        url = self.settings.approval_logic_app_url
        base = self.settings.public_base_url.rstrip("/")
        if not url or not base:
            return
        payload = _logic_app_payload(
            request,
            callbackUrl=f"{base}/api/approvals/{request.record_id}/callback",
            callbackToken=self.settings.approval_callback_token,
        )

        def _post() -> None:
            req = urllib.request.Request(url, data=payload,
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=30).close()  # noqa: S310

        try:
            await asyncio.to_thread(_post)
            logger.info("Approval card requested via Logic App for %s.", request.record_id)
        except Exception as exc:  # never fail the submission because Teams is down
            logger.warning("Logic App approval notification failed for %s: %s", request.record_id, exc)


def _record_to_json(rec: ExpenseRecord) -> dict:
    """Flatten a record for the frontend (strings only where JSON needs them)."""
    exp = rec.expense
    return {
        "id": rec.id,
        "status": rec.status.value,
        "submitter": exp.submitter,
        "vendor": exp.vendor,
        "expense_date": exp.expense_date.isoformat() if exp.expense_date else None,
        "category": exp.category.value,
        "currency": exp.currency,
        "total": str(exp.total) if exp.total is not None else None,
        "line_items": [
            {"description": li.description, "amount": str(li.amount)} for li in exp.line_items
        ],
        "extraction": {
            "model": rec.extraction.model if rec.extraction else None,
            "escalated": rec.extraction.escalated if rec.extraction else False,
            "confidence": rec.extraction.confidence if rec.extraction else None,
            "action": rec.extraction.action.value if rec.extraction else None,
            "issues": [
                {"code": i.code, "severity": i.severity.value, "message": i.message}
                for i in (rec.extraction.issues if rec.extraction else [])
            ],
        },
        "risk": {
            "level": rec.risk.risk.value if rec.risk else None,
            "total_base": str(rec.risk.computed_total_base) if rec.risk and rec.risk.computed_total_base else None,
            "base_currency": rec.risk.base_currency if rec.risk else None,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in (rec.risk.checks if rec.risk else [])
            ],
            "flags": [
                {"code": f.code, "severity": f.severity.value, "message": f.message}
                for f in (rec.risk.flags if rec.risk else [])
            ],
            "citations": [
                {"source": c.source, "passage": c.passage}
                for c in (rec.risk.policy_citations if rec.risk else [])
            ],
        },
        "decision": {
            "approved": rec.decision.approved,
            "route": rec.decision.route.value,
            "approver": rec.decision.approver,
            "reason": rec.decision.reason,
            "posted": rec.decision.posted,
        } if rec.decision else None,
        "created_at": rec.created_at.isoformat(),
        "updated_at": rec.updated_at.isoformat(),
    }
