"""FastAPI app — the HTTP surface over PipelineService.

    uvicorn webapp.main:app --reload --port 8000     # then open http://localhost:8000

Endpoints:
    POST /api/expenses                       upload a receipt (multipart) → run pipeline
    GET  /api/expenses                       history
    GET  /api/expenses/{record_id}           one record's full detail
    GET  /api/approvals                      escalated claims awaiting a human
    POST /api/approvals/{record_id}/decide   browser Approve/Reject → resume pipeline
    POST /api/approvals/{record_id}/callback Logic App (Teams click) → resume pipeline
    GET  /api/stats                          spend dashboard data
    GET  /health                             liveness (also ACA's wake-up probe target)
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from webapp.service import PipelineService

_STATIC = Path(__file__).parent / "static"

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB is plenty for a receipt photo


class DecideBody(BaseModel):
    approved: bool
    approver: str = "web-approver"
    comment: str = ""


class CallbackBody(BaseModel):
    """Payload the Teams Logic App POSTs when the human clicks Approve/Reject."""

    decision: str          # "approve" | "reject"
    approver: str = "teams-approver"
    respondedAt: str = ""  # noqa: N815 — matches the Logic App JSON field name


def create_app(service: PipelineService | None = None) -> FastAPI:
    svc = service or PipelineService()
    app = FastAPI(title="Expense IDP", version="1.0")

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "backend": svc.settings.expense_model_backend.value}

    @app.post("/api/expenses")
    async def submit_expense(file: UploadFile, submitter: str = Form("web-user@corp.com")) -> dict:
        data = await file.read()
        if not data:
            raise HTTPException(400, "Empty file.")
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File too large (max 10 MB).")
        return await svc.submit(data, file.filename or "receipt.jpg", submitter)

    @app.get("/api/expenses")
    async def list_expenses() -> list[dict]:
        return await svc.history()

    @app.get("/api/expenses/{record_id}")
    async def get_expense(record_id: str) -> dict:
        record = await svc._record_json(record_id)
        if record is None:
            raise HTTPException(404, f"No record {record_id}.")
        return record

    @app.get("/api/approvals")
    async def list_approvals() -> list[dict]:
        return await svc.pending_approvals()

    @app.post("/api/approvals/{record_id}/decide")
    async def decide(record_id: str, body: DecideBody) -> dict:
        return await _decide(svc, record_id, body.approved, body.approver, body.comment)

    @app.post("/api/approvals/{record_id}/callback")
    async def approval_callback(
        record_id: str,
        body: CallbackBody,
        x_callback_token: str | None = Header(default=None),
    ) -> dict:
        expected = svc.settings.approval_callback_token
        if expected and x_callback_token != expected:
            raise HTTPException(401, "Bad callback token.")
        decision = body.decision.strip().lower()
        if decision not in ("approve", "reject"):
            raise HTTPException(422, f"decision must be 'approve' or 'reject', got {body.decision!r}.")
        comment = f"via Teams card{f' at {body.respondedAt}' if body.respondedAt else ''}"
        return await _decide(svc, record_id, decision == "approve", body.approver, comment)

    @app.get("/api/stats")
    async def stats() -> dict:
        return await svc.stats()

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=_STATIC), name="static")
    return app


async def _decide(svc: PipelineService, record_id: str, approved: bool, approver: str, comment: str) -> dict:
    try:
        return await svc.decide(record_id, approved, approver, comment)
    except KeyError as err:
        raise HTTPException(404, str(err)) from err
    except ValueError as err:
        raise HTTPException(409, str(err)) from err


app = create_app()
