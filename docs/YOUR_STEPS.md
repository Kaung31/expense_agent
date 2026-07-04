# YOUR STEPS — running & deploying the Expense IDP web app

This is *your* checklist. Everything code-side is done and tested; these are the parts
only you can run (local commands + anything needing your Azure login). Copy-paste in
order. Every command assumes you start in the project folder:

```bash
cd ~/expense_report_processor
```

---

## 1. Run the web app LOCALLY

```bash
source .venv/bin/activate
uvicorn webapp.main:app --port 8000
```

Open **http://localhost:8000** in your browser.

Look at the pill in the top-right corner:
- **`backend: foundry`** → it read your `.env` and will use your REAL Azure model
  (`gpt-5.4-mini`). Each upload costs a few cents of tokens and needs `az login` to be valid.
- **`backend: mock`** → offline mode, $0, no Azure needed.

To force offline mode for a session (your `.env` is not modified):

```bash
EXPENSE_MODEL_BACKEND=mock LOCAL_STORE_PATH=.localstore/web-mock.json uvicorn webapp.main:app --port 8000
```

Stop the server with `Ctrl+C`.

---

## 2. TEST locally with your 5 sample receipts

The app has 4 tabs: **Submit**, **Approval queue**, **History**, **Dashboard**.

### Step-by-step (use `backend: foundry` for real extraction)

1. **Submit tab** → drag one of your 5 receipts onto the dotted box (or click it and pick
   the file). A spinner runs for a few seconds while `gpt-5.4-mini` reads it.

2. **What correct looks like after extraction:** a result card appears showing
   - vendor, amount + currency, date, category (read from YOUR receipt — verify they match!)
   - the model used (and "(escalated)" if the big model was needed)
   - **Checks** — ✔/✘ lines for `total_vs_items`, `required_fields`, `category_cap`, `duplicate`
   - **Flags** — anything policy-relevant (over cap, alcohol, missing fields…)
   - Risk chip (`low` / `medium` / `high`) and a status chip.

3. **Outcomes to expect across your 5 receipts:**
   - Small clean receipt (≤ $75, no flags) → **✅ Auto-approved & posted**. Done instantly.
   - Big receipt (> $75) or one with flags (alcohol, over-cap, mismatch) →
     **⏸ Escalated — waiting in the approval queue**.
   - The same file dropped **twice** → the second one escalates with a
     `duplicate` flag (content-hash match). This is a feature — demo it!

4. **Approval queue tab** → escalated claims appear as cards with a red badge count in
   the tab. Each card shows the amount, risk, and the exact flag reasons.
   Click **Approve** on one, **Reject** on another.

5. **What correct looks like after deciding:** the card disappears from the queue.
   In **History**, the approved claim shows status `posted` (it went through the ERP-post
   step), the rejected one shows `rejected`. Click any row for full detail —
   line items, every check, policy citations, who decided and why.

6. **Dashboard tab** → after a few receipts you should see: total claims, the
   **approval rate** move as you approve/reject, **flagged** count, spend-by-category
   bars (approved claims only), and claims-by-status bars.

If anything errors, the result card shows the reason. Most common: `az login` token
expired → run `az login` and retry.

---

## 3. DEPLOY to Azure (Container Apps, scale-to-zero)

You already have: resource group `rg-expidp-dev`, Foundry + `gpt-5.4` models, AI Search
(and roles). The web app adds: a container registry (ACR) + a Container Apps environment
+ the app itself.

### 3.1 Log in and register the provider (once)

```bash
az login
az account set --subscription "<YOUR_SUBSCRIPTION_ID>"
az provider register -n Microsoft.App --wait          # one-time, ~1 min
```

### 3.2 Deploy the infrastructure with the web app enabled

```bash
az deployment sub create --name expidp --location eastus \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam deployWebApp=true deploySearch=true deployLogicApp=true
```

Notes:
- `deploySearch=true` only if you want Azure AI Search policy RAG (it **bills ~$75/mo
  while it exists** — leave it off to use the built-in local policy corpus instead).
- `deployLogicApp=true` only if you want the Teams-card approval callback. Without it,
  approvals happen in the web UI's queue — fully functional.
- The app starts with a public *placeholder* image — that's expected. Next step replaces it.

### 3.3 Build and ship the real container image (no Docker needed locally)

```bash
ACR=$(az deployment sub show -n expidp --query properties.outputs.acrName.value -o tsv)
APP=$(az deployment sub show -n expidp --query properties.outputs.containerAppName.value -o tsv)

az acr build --registry "$ACR" --image expense-idp:v1 .        # cloud build, ~3-5 min

az containerapp update -n "$APP" -g rg-expidp-dev \
  --image "$ACR.azurecr.io/expense-idp:v1"
```

### 3.4 Get your app's URL

```bash
az deployment sub show -n expidp --query properties.outputs.webAppUrl.value -o tsv
```

Open that URL in your browser. First load after idle takes ~10–20 s (cold start from
zero replicas) — that's the scale-to-zero working, not a bug.

### 3.5 Only-you role note

Your earlier role grants were to *your user*; the container runs as the **managed
identity**, which already got its roles from the Bicep (Foundry, Cosmos, Search,
Storage, AcrPull) — so normally **no extra grants needed**. If the deployed app errors
with 403 on Search, re-run the search role grants from `docs/AZURE_SETUP.md` §2a but
with the identity's principal id:

```bash
MI=$(az identity show -n id-expidp -g rg-expidp-dev --query principalId -o tsv)
```

### 3.6 (Optional) Teams approval callback

If you deployed the Logic App: authorize the Teams connection once in the portal
(resource group → API connection **teams-approvals** → Edit API connection → Authorize
→ Save), and set your team/channel ids in `infra/modules/logicapp.bicep` (the two
`REPLACE_WITH_…` defaults), then redeploy. When a claim escalates, a Teams card appears;
clicking Approve/Reject **calls back into the deployed app** and resumes the pipeline —
even if the app had scaled to zero (the callback wakes it; the decision is recovered
from the record store).

---

## 4. TEST the deployed version end-to-end

1. Open the app URL from 3.4 — pill should say **`backend: foundry`**.
2. Drag a real receipt in → extraction runs on your deployed `gpt-5.4-mini`.
3. Small clean receipt → auto-approved & posted. Check **History** + **Dashboard**.
4. Big/flagged receipt → lands in **Approval queue**. Approve it in the browser
   (and/or via the Teams card if you wired 3.6) → status becomes `posted`.
5. The scale-to-zero recovery test: wait ~15 min after an escalation (app scales to 0),
   then open the site again and approve the pending claim — it must still finalize
   correctly (status `posted`). This proves the store-recovery path.
6. Logs if anything misbehaves:
   ```bash
   az containerapp logs show -n "$APP" -g rg-expidp-dev --follow
   ```

---

## 5. COST + TEARDOWN

What bills what:

| Resource | Idle cost | Notes |
|---|---|---|
| Container App | **$0 idle** | `minReplicas: 0` — verify below |
| Container Apps environment | $0 | consumption plan |
| **ACR (Basic)** | **~$5/mo while it exists** | small but nonzero |
| **AI Search (Basic)** | **~$75/mo while it exists** | the big one — delete when not testing |
| Models / Cosmos / Logic App / Storage / App Insights | ~$0 idle | pay-per-use |

**Verify scale-to-zero is on:**

```bash
az containerapp show -n "$APP" -g rg-expidp-dev \
  --query properties.template.scale.minReplicas -o tsv        # must print 0
az containerapp replica list -n "$APP" -g rg-expidp-dev -o table   # empty after ~15 min idle
```

**Stop the monthly-billing pieces when done testing** (keeps everything else):

```bash
# AI Search (~$75/mo) — the pipeline falls back to the local policy corpus
SR=$(az search service list -g rg-expidp-dev --query "[0].name" -o tsv)
az search service delete -g rg-expidp-dev -n "$SR" --yes

# ACR (~$5/mo) — re-create later by redeploying with deployWebApp=true + acr build
az acr delete -n "$ACR" -g rg-expidp-dev --yes
```

**Nuke absolutely everything** (all of it is reproducible from `infra/`):

```bash
az group delete -n rg-expidp-dev --yes --no-wait
```

Re-deploying later is just §3.2–3.3 again.
