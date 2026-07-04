"""Record store — the persistence + duplicate-lookup surface (guide: Cosmos DB).

`RecordStore` is the async interface the pipeline depends on. `LocalRecordStore`
is a file-backed implementation used for local dev and tests; `CosmosRecordStore`
(added in the Azure phase) implements the same interface, so nothing upstream
changes when we switch backends.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol, runtime_checkable

from expense_extractor.schemas import ExpenseRecord


@runtime_checkable
class RecordStore(Protocol):
    async def get(self, record_id: str) -> ExpenseRecord | None: ...
    async def upsert(self, record: ExpenseRecord) -> ExpenseRecord: ...
    async def find_by_hash(self, sha256: str) -> ExpenseRecord | None: ...
    async def find_similar(
        self,
        submitter: str | None,
        total_base: Decimal | None,
        on_date: date | None,
        window_days: int = 3,
        amount_tolerance: Decimal = Decimal("0.01"),
    ) -> list[ExpenseRecord]: ...
    async def list_all(self) -> list[ExpenseRecord]: ...


class LocalRecordStore:
    """JSON-file-backed store. Not for production — for offline dev and tests."""

    def __init__(self, path: str | Path = ".localstore/records.json") -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text() or "{}")

    def _write(self, data: dict[str, dict]) -> None:
        self._path.write_text(json.dumps(data, indent=2, default=str))

    async def get(self, record_id: str) -> ExpenseRecord | None:
        async with self._lock:
            raw = self._read().get(record_id)
        return ExpenseRecord.model_validate(raw) if raw else None

    async def upsert(self, record: ExpenseRecord) -> ExpenseRecord:
        async with self._lock:
            data = self._read()
            data[record.id] = record.model_dump(mode="json")
            self._write(data)
        return record

    async def list_all(self) -> list[ExpenseRecord]:
        async with self._lock:
            data = self._read()
        return [ExpenseRecord.model_validate(v) for v in data.values()]

    async def find_by_hash(self, sha256: str) -> ExpenseRecord | None:
        for rec in await self.list_all():
            if rec.expense.source and rec.expense.source.sha256 == sha256:
                return rec
        return None

    async def find_similar(
        self,
        submitter: str | None,
        total_base: Decimal | None,
        on_date: date | None,
        window_days: int = 3,
        amount_tolerance: Decimal = Decimal("0.01"),
    ) -> list[ExpenseRecord]:
        """Same person + near-identical amount + near date = probable duplicate."""
        matches: list[ExpenseRecord] = []
        for rec in await self.list_all():
            if submitter and rec.expense.submitter != submitter:
                continue
            # Compare on the base-currency total when the validator computed one.
            rec_total = rec.risk.computed_total_base if rec.risk else None
            if total_base is not None and rec_total is not None:
                if abs(rec_total - total_base) > amount_tolerance:
                    continue
            elif total_base is not None and rec.expense.total is not None:
                if abs(rec.expense.total - total_base) > amount_tolerance:
                    continue
            if on_date and rec.expense.expense_date:
                if abs((rec.expense.expense_date - on_date).days) > window_days:
                    continue
            matches.append(rec)
        return matches


class CosmosRecordStore:
    """Production `RecordStore` backed by Azure Cosmos DB (SQL API). Same interface.

    Auth is Entra (DefaultAzureCredential) — no keys. Container is partitioned on
    `/partition_key` (the submitter), matching `infra/modules/cosmos.bicep`.
    """

    def __init__(self, endpoint: str, database: str, container: str, *, credential=None) -> None:
        from azure.cosmos.aio import CosmosClient

        if credential is None:
            from azure.identity.aio import DefaultAzureCredential

            credential = DefaultAzureCredential()
        self._client = CosmosClient(endpoint, credential=credential)
        self._container = self._client.get_database_client(database).get_container_client(container)

    async def get(self, record_id: str) -> ExpenseRecord | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        query = "SELECT * FROM c WHERE c.id = @id"
        params: list[dict[str, object]] = [{"name": "@id", "value": record_id}]
        try:
            async for item in self._container.query_items(query=query, parameters=params):
                return ExpenseRecord.model_validate(item)
        except CosmosResourceNotFoundError:
            return None
        return None

    async def upsert(self, record: ExpenseRecord) -> ExpenseRecord:
        await self._container.upsert_item(record.model_dump(mode="json"))
        return record

    async def _query(self, query: str, params: list[dict[str, object]]) -> list[ExpenseRecord]:
        return [
            ExpenseRecord.model_validate(item)
            async for item in self._container.query_items(query=query, parameters=params)
        ]

    async def list_all(self) -> list[ExpenseRecord]:
        return await self._query("SELECT * FROM c", [])

    async def find_by_hash(self, sha256: str) -> ExpenseRecord | None:
        rows = await self._query(
            "SELECT * FROM c WHERE c.expense.source.sha256 = @sha",
            [{"name": "@sha", "value": sha256}],
        )
        return rows[0] if rows else None

    async def find_similar(
        self,
        submitter: str | None,
        total_base: Decimal | None,
        on_date: date | None,
        window_days: int = 3,
        amount_tolerance: Decimal = Decimal("0.01"),
    ) -> list[ExpenseRecord]:
        # Narrow by submitter server-side; apply amount/date tolerance client-side
        # (keeps the query simple and the fuzzy logic identical to LocalRecordStore).
        rows = await self._query(
            "SELECT * FROM c WHERE c.expense.submitter = @s",
            [{"name": "@s", "value": submitter}],
        ) if submitter else await self.list_all()

        matches: list[ExpenseRecord] = []
        for rec in rows:
            rec_total = rec.risk.computed_total_base if rec.risk else rec.expense.total
            if total_base is not None and rec_total is not None:
                if abs(rec_total - total_base) > amount_tolerance:
                    continue
            if on_date and rec.expense.expense_date:
                if abs((rec.expense.expense_date - on_date).days) > window_days:
                    continue
            matches.append(rec)
        return matches


def build_store(settings=None) -> RecordStore:
    """Pick the store per backend: Cosmos when configured, else the local file store."""
    from expense_extractor.config import get_settings

    settings = settings or get_settings()
    if not settings.is_mock and settings.cosmos_endpoint:
        return CosmosRecordStore(settings.cosmos_endpoint, settings.cosmos_database, settings.cosmos_container)
    return LocalRecordStore(settings.local_store_path)
