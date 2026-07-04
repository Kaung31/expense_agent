# Azure setup — your steps

Do these, then tell me and I'll take it from there (real end-to-end test, fixes, Foundry Evaluations).

Two tiers. **Tier 1** gets the *real vision model* running with the least setup (records/policy still
use local fallbacks). **Tier 2** turns on the rest of Azure (Cosmos, AI Search, Logic App). Start with Tier 1.

---

## 0. Prerequisites (once)

```bash
# Install Azure CLI (macOS)
brew install azure-cli

az login
az account set --subscription "<YOUR_SUBSCRIPTION_ID>"
az account show --query '{sub:name, user:user.name}' -o table
```

You need **Owner** (or Contributor **+ User Access Administrator**) on the subscription, because the
Bicep creates role assignments.

---

## Tier 1 — minimum to run the real vision extractor

Here only the **models** must exist in Azure. The validator's policy-RAG and the record store fall
back to local automatically when `AZURE_SEARCH_ENDPOINT` / `COSMOS_ENDPOINT` are unset.

### 1a. Deploy just the Foundry account + 2 model deployments

The default params deploy **only** identity + monitoring + Foundry + models (no Cosmos/Search/Logic App),
so this sidesteps the WorkflowStandard quota and Cosmos capacity limits:

```bash
az deployment sub create \
  --name expidp \
  --location eastus \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam
```

> **Model availability:** the template deploys `gpt-4.1-mini` and `gpt-4.1` (version `2025-04-14`) as
> `GlobalStandard`. If your region rejects those versions, do a **two-step**: first create just the
> account with `--parameters infra/main.bicepparam deployModels=false`, then list what's offered and
> tell me — I'll set the exact versions:
> ```bash
> AIF=$(az resource list -g rg-expidp-dev --resource-type Microsoft.CognitiveServices/accounts --query "[0].name" -o tsv)
> az cognitiveservices account list-models -n "$AIF" -g rg-expidp-dev \
>   --query "[?contains(name,'gpt-4')].{model:name, version:version, sku:skus[0].name}" -o table
> ```

### 1b. Grant *your* user data-plane access to Foundry

Locally the app authenticates as **your `az login` identity** (DefaultAzureCredential), so your user —
not just the managed identity — needs the inference role:

```bash
ME=$(az ad signed-in-user show --query id -o tsv)
AIF=$(az deployment sub show -n expidp --query properties.outputs.foundryEndpoint.value -o tsv)
AIF_ID=$(az resource list --resource-type Microsoft.CognitiveServices/accounts \
  -g rg-expidp-dev --query "[0].id" -o tsv)

az role assignment create --assignee "$ME" \
  --role "Cognitive Services OpenAI User" --scope "$AIF_ID"
```

### 1c. Generate `.env` and run

```bash
source .venv/bin/activate                 # or: python3.12 -m venv .venv && pip install -e ".[agents,dev,observability]"
bash scripts/gen_env.sh expidp            # writes .env from the deployment outputs
# then flip these two in .env for Tier 1 so records/policy stay local:
#   COSMOS_ENDPOINT=            (leave empty)
#   AZURE_SEARCH_ENDPOINT=      (leave empty)

python demo.py tests/samples/receipt.png  # now hits the real gpt-5.4-mini vision model
```

Drop a real receipt at `tests/samples/receipt.png` (or pass any path). **Tell me once this runs** — I'll
verify the extraction, fix any auth/API-version errors, and tune the prompt against your real docs.

---

## Approval Logic App (Consumption — works on Azure for Students)

The Logic App is **Consumption tier** (`Microsoft.Logic/workflows`): fully serverless, **no
WorkflowStandard quota needed**, so it deploys on student subscriptions. Enable it with the
`deployLogicApp` toggle. The model defaults are baked into `main.bicep`, so you can deploy straight
from the template (no `.bicepparam` needed) and just pass the toggles:

```bash
az deployment sub create --name expidp --location eastus \
  --template-file infra/main.bicep \
  --parameters deployLogicApp=true
```

> Note: each resource has its own toggle now — `deployStorage`, `deployCosmos`, `deploySearch`,
> `deployLogicApp` (all default `false`). Passing only `deployLogicApp=true` adds the Logic App and
> leaves records/policy on local fallbacks. Deploys are **incremental** — nothing already deployed
> (Foundry, models, App Insights) is touched or deleted.

**Get the trigger URL → `APPROVAL_LOGIC_APP_URL`.** The Logic App module outputs `triggerUrl` (the HTTP
callback the Orchestrator POSTs to). Read it from the nested `logicapp` deployment and put it in `.env`:

```bash
az deployment group show -g rg-expidp-dev -n logicapp \
  --query properties.outputs.triggerUrl.value -o tsv
# paste the result into .env as:  APPROVAL_LOGIC_APP_URL=<that url>
```

`gen_env.sh` leaves `APPROVAL_LOGIC_APP_URL` blank; set it from the command above once the Logic App
is deployed. When it's set, the pipeline's `LogicAppNotifier` POSTs approval/notification payloads to it.

---

## Tier 2 — full cloud data services (Cosmos + AI Search)

Enable whichever data services you're testing (each toggle is independent). If East US Cosmos
capacity errors again, change `--location` to `eastus2` or `westus3`. Example — Search + Cosmos:

```bash
az deployment sub create --name expidp --location eastus \
  --template-file infra/main.bicep \
  --parameters deploySearch=true deployCosmos=true deployStorage=true
```

Deploys are incremental, so this adds only the data services and leaves Foundry/models untouched.
**Delete them again when you're done testing** so they don't bill while idle (see below).

### 2a. Grant your user the remaining data-plane roles

```bash
ME=$(az ad signed-in-user show --query id -o tsv)
RG=rg-expidp-dev

# Storage (receipts)
ST_ID=$(az storage account list -g $RG --query "[0].id" -o tsv)
az role assignment create --assignee "$ME" --role "Storage Blob Data Contributor" --scope "$ST_ID"

# AI Search (create + read the policy index)
SR_ID=$(az search service list -g $RG --query "[0].id" -o tsv)
az role assignment create --assignee "$ME" --role "Search Service Contributor"      --scope "$SR_ID"
az role assignment create --assignee "$ME" --role "Search Index Data Contributor"   --scope "$SR_ID"

# Cosmos (data-plane role is a Cosmos SQL role, not standard RBAC).
# Guarded: skips cleanly if you didn't deploy Cosmos (records use the local fallback).
COSMOS=$(az cosmosdb list -g $RG --query "[0].name" -o tsv)
if [ -n "$COSMOS" ]; then
  az cosmosdb sql role assignment create -g $RG -a "$COSMOS" \
    --role-definition-id 00000000-0000-0000-0000-000000000002 \
    --principal-id "$ME" --scope "/"
else
  echo "No Cosmos account deployed — skipping (records use the local store)."
fi
```

### 2b. Index the expense policy into AI Search

```bash
bash scripts/gen_env.sh expidp     # regenerate .env with all endpoints populated
python scripts/index_policy.py     # creates the index + uploads the policy passages
```

### 2c. Run fully on Azure

```bash
python demo.py tests/samples/receipt.png
```

With `.env` fully populated, the extractor + policy judge run on Azure OpenAI, records persist in Cosmos,
duplicate lookups + policy citations come from Azure, and traces flow to Application Insights.

---

## After you've done Tier 1 (and optionally Tier 2)

Ping me with the result. Then I'll:
- run/verify a real receipt end-to-end and fix any errors,
- wire the **Logic App approval flow** (Teams card) to the HITL pause point,
- add **Foundry Evaluations** (tool-call accuracy + task adherence) as a regression gate.

To tear it all down: `az group delete -n rg-expidp-dev --yes --no-wait`.
