# Infrastructure (Bicep) — Phase 0

Provisions the whole resource set with a shared **user-assigned managed identity** and
RBAC — no API keys anywhere (guide §8). Validate offline, then deploy.

## What it creates

| Module | Resource | Purpose |
|---|---|---|
| `identity` | User-assigned managed identity | One identity for the workflow → all services (Entra Agent ID pattern) |
| `monitoring` | Log Analytics + Application Insights | Tracing/eval sink (Phase 5) |
| `storage` | Storage account + `receipts` container | Raw receipts (Blob) |
| `cosmos` | Cosmos DB (serverless) `expenses/records` | Expense records + duplicate lookup |
| `search` | Azure AI Search (basic) | Expense-policy RAG index |
| `foundry` | AIServices account + project + 2 model deployments | Vision extractor + escalation model |
| `logicapp` | Logic App (Standard) + plan + storage | Ingestion trigger + approval callbacks |

Every data service has `disableLocalAuth: true` and a role assignment to the managed
identity (Blob Data Contributor, Cosmos Data Contributor, Search Index Data
Contributor, Cognitive Services OpenAI User).

## Validate (no Azure needed)

```bash
az bicep build --file infra/main.bicep --stdout > /dev/null   # type-checks the whole graph
```

## Deploy

```bash
az login
az account set --subscription <your-sub-id>

az deployment sub create \
  --location eastus \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam
```

The deployment outputs the endpoints you copy into `.env`
(`foundryProjectEndpoint`, `cosmosEndpoint`, `searchEndpoint`, `blobEndpoint`,
`appInsightsConnectionString`) and the `managedIdentityClientId`.

## Notes & production hardening

- **Model versions/regions.** `modelName`/`escalationModelName` map the guide's fictional
  `gpt-5.4*` to real deployments (default `gpt-4o-mini` / `gpt-4o`). Adjust the versions in
  `modules/foundry.bicep` to what your region offers (`az cognitiveservices account list-models`).
- **Private networking.** This template uses RBAC + `disableLocalAuth` and public endpoints for a
  clean dev deploy. For prod, add a VNet + private endpoints for Cosmos/Search/Storage/Foundry and
  set `publicNetworkAccess: 'Disabled'` — the guide's §8 target. Kept out of the default path so the
  first deploy succeeds without VNet plumbing.
- **Cost.** Serverless Cosmos, basic Search, and GlobalStandard model SKUs keep dev cost to cents per
  report. Add budget alerts in Cost Management and keep `MAX_AGENT_ITERATIONS` capped (guide §7).
