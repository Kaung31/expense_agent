"""Create the Azure AI Search policy index and upload the expense policy (Phase 2).

    python scripts/index_policy.py

Reads AZURE_SEARCH_ENDPOINT / AZURE_SEARCH_INDEX from the environment, creates the
index if needed, and uploads `POLICY_CORPUS`. Auth is Entra (DefaultAzureCredential) —
run `az login` first. This is the production counterpart to `LocalPolicySearch`.
"""

from __future__ import annotations

from expense_extractor.config import get_settings
from tools.policy_search import POLICY_CORPUS


def main() -> None:
    from azure.identity import DefaultAzureCredential
    from azure.search.documents import SearchClient
    from azure.search.documents.indexes import SearchIndexClient
    from azure.search.documents.indexes.models import (
        SearchableField,
        SearchField,
        SearchFieldDataType,
        SearchIndex,
        SimpleField,
    )

    settings = get_settings()
    endpoint = settings.azure_search_endpoint
    index_name = settings.azure_search_index
    if not endpoint:
        raise SystemExit("Set AZURE_SEARCH_ENDPOINT (and run `az login`).")

    credential = DefaultAzureCredential()

    index = SearchIndex(
        name=index_name,
        fields=[
            SimpleField(name="id", type=SearchFieldDataType.String, key=True),  # type: ignore[arg-type]  # azure stub mistypes the str-enum member
            SearchableField(name="passage", type=SearchFieldDataType.String),
            SearchField(name="source", type=SearchFieldDataType.String, filterable=True),
        ],
    )
    SearchIndexClient(endpoint, credential).create_or_update_index(index)
    print(f"Index '{index_name}' ready.")

    docs = [{"id": source, "source": source, "passage": passage} for source, passage in POLICY_CORPUS]
    result = SearchClient(endpoint, index_name, credential).upload_documents(documents=docs)
    print(f"Uploaded {sum(1 for r in result if r.succeeded)}/{len(docs)} policy passages.")


if __name__ == "__main__":
    main()
