"""Policy RAG — cite the *real* policy passage behind a decision (guide §5).

`PolicySearch` is the interface the Validator depends on. `LocalPolicySearch` does
keyword-overlap retrieval over an embedded policy corpus so the pipeline cites real
text offline; `AzureAiSearchPolicy` (Azure phase) queries the AI Search index with
the same interface.
"""

from __future__ import annotations

import re
from typing import Protocol

from expense_extractor.schemas import PolicyCitation

# A compact expense policy. In production this is indexed in Azure AI Search; here
# it stands in so the Validator can cite grounded passages during offline dev.
POLICY_CORPUS: list[tuple[str, str]] = [
    ("meals-per-diem", "Meals are reimbursable up to a per-meal cap of USD 75. Amounts above the "
                       "cap require director approval and an itemized receipt."),
    ("alcohol", "Alcohol (including wine, beer, cocktails, spirits, and bar tabs) is NOT reimbursable "
                "for standard cost centers. It may be reimbursed only for approved client-entertainment "
                "cost centers, and must be itemized separately."),
    ("receipts-required", "Any expense of USD 25 or more requires an itemized receipt. Missing "
                          "receipts must be escalated for manual approval."),
    ("personal-expenses", "Personal expenses (personal grooming, minibar, in-room movies, personal "
                          "travel) are never reimbursable and must be excluded."),
    ("lodging", "Lodging is reimbursable up to USD 350 per night excluding taxes. Room-service meals "
                "count against the meals per-diem."),
    ("airfare", "Airfare must be economy class for flights under 6 hours. Business class requires VP "
                "pre-approval."),
    ("entertainment", "Client entertainment requires the names and companies of attendees and a "
                      "documented business purpose."),
    ("duplicates", "Duplicate submissions of the same expense are prohibited and will be rejected."),
    ("submission-deadline", "Expenses must be submitted within 90 days of the transaction date. "
                            "Older receipts require manual finance approval and may be declined."),
]

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


class PolicySearch(Protocol):
    async def search(self, query: str, top: int = 3) -> list[PolicyCitation]: ...

    async def get(self, source: str) -> PolicyCitation | None: ...


class LocalPolicySearch:
    """Offline keyword-overlap retrieval over `POLICY_CORPUS`."""

    def __init__(self, corpus: list[tuple[str, str]] | None = None) -> None:
        self._corpus = corpus or POLICY_CORPUS

    async def get(self, source: str) -> PolicyCitation | None:
        for src, passage in self._corpus:
            if src == source:
                return PolicyCitation(source=src, passage=passage)
        return None

    async def search(self, query: str, top: int = 3) -> list[PolicyCitation]:
        q = _tokens(query)
        scored: list[tuple[float, str, str]] = []
        for source, passage in self._corpus:
            overlap = q & _tokens(passage)
            if not overlap:
                continue
            score = len(overlap) / (len(q) or 1)
            scored.append((score, source, passage))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [
            PolicyCitation(source=s, passage=p, score=round(sc, 3))
            for sc, s, p in scored[:top]
        ]


class AzureAiSearchPolicy:
    """Production `PolicySearch` backed by Azure AI Search (same interface as local).

    Fields expected on the index: `id` (key) and `passage` (searchable). Populate it
    with `scripts/index_policy.py`. Auth is Entra (DefaultAzureCredential) — no keys.
    """

    def __init__(self, endpoint: str, index_name: str, *, credential=None) -> None:
        self._endpoint = endpoint
        self._index_name = index_name
        self._credential = credential  # injected → caller owns it; else opened/closed per call

    async def _with_client(self, fn):
        """Open a SearchClient (+ credential) for one call and close both cleanly."""
        from azure.search.documents.aio import SearchClient

        if self._credential is not None:
            async with SearchClient(self._endpoint, self._index_name, credential=self._credential) as client:
                return await fn(client)

        from azure.identity.aio import DefaultAzureCredential

        async with DefaultAzureCredential() as cred, \
                SearchClient(self._endpoint, self._index_name, credential=cred) as client:
            return await fn(client)

    async def get(self, source: str) -> PolicyCitation | None:
        async def _do(client):
            try:
                doc = await client.get_document(key=source)
            except Exception:
                return None
            return PolicyCitation(source=source, passage=doc.get("passage", ""))

        return await self._with_client(_do)

    async def search(self, query: str, top: int = 3) -> list[PolicyCitation]:
        async def _do(client):
            results = await client.search(search_text=query, top=top)
            citations: list[PolicyCitation] = []
            async for doc in results:
                citations.append(
                    PolicyCitation(
                        source=doc.get("id", "policy"),
                        passage=doc.get("passage", ""),
                        score=doc.get("@search.score"),
                    )
                )
            return citations

        return await self._with_client(_do)
