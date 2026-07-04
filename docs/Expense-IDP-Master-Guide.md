# Expense IDP — Master Project Guide

*An agentic Intelligent Document Processing pipeline on Azure, built with a vision-LLM extractor and a multi-agent workflow. Structured so you can build it step-by-step with **Claude Cowork**.*

> **How to use this guide with Cowork.** Each phase in §6 is a self-contained work package: goal, tasks, deliverables, done-criteria, and a ready-to-paste "Tell Cowork" prompt. Do one phase per Cowork session. Keep this file and the `expense_extractor/` code in your project folder so Cowork has the context every time.

---

## 0. Goal & definition of done

Ingest a receipt/invoice → extract structured data with a vision model → check it against expense policy → route for approval (auto-approving low-risk) → post to a system of record → surface analytics. Every agent step traced end-to-end.

**Done when:**
- A receipt dropped into a folder/email flows to "approved + recorded + notified" with no manual glue.
- Low-risk auto-approves; high-risk triggers a real human approval step.
- Every run is traceable in Application Insights (which agent, what it called, tokens, latency).
- Infra is reproducible from Bicep; repo has README + short demo video.
- Handles blurry photos, multi-page receipts, foreign currency, missing totals, duplicates, and injection-laced documents.

---

## 1. The short answer: yes, it all composes

Your vision-LLM extractor combines cleanly with the rest of the agents and models. Here's the mechanism, because understanding it is what lets you extend the pipeline confidently.

### Different model per agent — by design

Microsoft Agent Framework treats the model as a per-agent choice. The Extractor can be a vision model, the Validator a reasoning model, the Orchestrator mostly rules. You can even mix **providers** — the Foundry catalog has 11,000+ models across OpenAI, Anthropic, Meta, Google, and xAI, so Claude could do policy nuance while GPT-5.4-mini does vision. Recommended mix:

| Agent | Model | Why |
|---|---|---|
| **Extractor** | GPT-5.4-mini (vision) → GPT-5.4 on escalation | Vision reads non-standard layouts; escalate only hard docs. |
| **Validator** | Small reasoning model + AI Search (RAG) + deterministic tools | Cheap for routine checks; RAG cites real policy; hard logic catches fraud. |
| **Orchestrator** | Rules-first + small model for edge cases | Routing is mostly thresholds; model only for judgment calls. |
| **Insights** (optional) | Small model + Code Interpreter | Summaries/charts don't need a big model. |

### The glue is a typed handoff

Agents don't pass free text around — they pass validated objects. The Extractor emits `ExtractionResult` (which you've already built); the Validator receives `.expense` + `.action`, adds a risk score + flags; the Orchestrator receives that and decides routing. Because each contract is a Pydantic schema, a change in one agent can't silently corrupt the next.

### Two orchestration styles (you'll use both)

- **Workflow graph** (`WorkflowBuilder`) — a fixed pipeline: Extractor → Validator → Orchestrator, with a **conditional edge** at the Orchestrator (auto-approve vs escalate). This is your backbone. Supports checkpointing and native human-in-the-loop, so an approval that waits hours doesn't lose state.
- **Connected agents** — point-to-point calls where an agent invokes another as a tool (e.g. the Validator asks a "duplicate-check" sub-agent). Use sparingly, for on-demand sub-tasks.

### Deployment: local now, Foundry later, same code

Author and debug locally with `FoundryChatClient`; when you want the production story, deploy the same agents as **Foundry hosted agents** (managed memory, autoscale, Entra Agent ID, private networking) with a couple of extra lines. No rewrite.

---

## 2. Architecture (finalized)

```
                       ┌─────────────────────────────────────────────┐
   Receipt/Invoice     │            Logic App (Standard)             │
   (email / blob) ────▶│  Trigger → call workflow endpoint (HTTPS)   │
                       └───────────────────┬─────────────────────────┘
                                           │ Managed Identity, private endpoint
                                           ▼
        ┌──────────────────────────────────────────────────────────────────┐
        │        Agent Framework workflow  (local → Foundry hosted)         │
        │                                                                    │
        │   [Extractor]───▶[Validator]───▶[Orchestrator]                     │
        │   GPT-5.4-mini    small model     rules +                          │
        │   (vision)        + AI Search     small model                      │
        │       │           (policy RAG)        │                           │
        │       ▼               ▼               ├─ low risk ─▶ auto-approve   │
        │  ExtractionResult  RiskAssessment     └─ high risk ─▶ human loop    │
        │  + Cosmos          + duplicate-check                (HITL pause)    │
        └───────────────────┬───────────────────────────────┬──────────────┘
                            ▼                                 ▼
                  Post to ERP/GL ──▶ Notify submitter   Teams/email approval
                            │                            (resume) ──▶ Post/Notify
                            ▼
        Cosmos DB (records) ──▶ Insights agent / Power BI (analytics)

   Every span ──▶ Application Insights (OpenTelemetry) + Foundry Evaluations
   All resources in a VNet w/ private endpoints; identities via Entra.
```

---

## 3. Tech stack (2026)

| Layer | Choice |
|---|---|
| Ingestion | Azure Logic Apps (Standard) |
| **Extraction** | **Vision LLM — GPT-5.4-mini → GPT-5.4 escalation** (the `expense_extractor/` module) |
| Agents | Microsoft Agent Framework 1.0 (graph workflows) |
| Agent hosting | Foundry Agent Service (hosted agents) |
| Models | GPT-5.4-mini default; GPT-5.4 for hard reasoning; mix providers as needed |
| State / results | Azure Cosmos DB |
| Raw files | Azure Blob Storage |
| Knowledge / RAG | Azure AI Search (Foundry IQ) — indexed expense policy |
| Approvals | Logic Apps Approvals / Teams Adaptive Card |
| ERP/finance | Logic Apps connector (Dynamics 365 / SAP / stub API) |
| Observability | Foundry tracing → App Insights (OTel) + Foundry Evaluations (GA) |
| Guardrails | Foundry content safety + XPIA (cross-prompt-injection) |
| IaC / CI-CD | Bicep + GitHub Actions |

---

## 4. Repository structure

```
expense-idp/
├── infra/                     # Bicep templates (Phase 0)
│   ├── main.bicep
│   └── modules/               # foundry, cosmos, search, storage, logicapp, appinsights
├── expense_extractor/         # DONE — the vision extractor
│   ├── schemas.py
│   ├── extractor.py
│   ├── agent.py
│   └── README.md
├── agents/
│   ├── validator.py           # Phase 2
│   ├── orchestrator.py        # Phase 3
│   └── insights.py            # Phase 6 (optional)
├── workflow/
│   └── pipeline.py            # WorkflowBuilder wiring all agents
├── tools/
│   ├── policy_search.py       # AI Search RAG tool
│   ├── duplicate_check.py     # Cosmos lookup tool
│   └── erp_post.py            # ERP/GL write tool (stub ok for demo)
├── logicapps/                 # Logic App definitions (triggers + approvals)
├── tests/
│   └── samples/               # tricky receipts for regression
├── .github/workflows/         # CI/CD
└── README.md
```

---

## 5. The agents in detail

### Extractor — DONE ✅
Role: data processor. Model: GPT-5.4-mini vision → GPT-5.4 escalation. Input: document reference. Output: `ExtractionResult` (typed fields + validation issues + action). No tools, takes no actions (injection-safe). Already built in `expense_extractor/`.

### Validator — the compliance officer
Model: small reasoning model. Input: `ExtractionResult.expense`. Does:
- **Deterministic** (tools): total = sum(items)? within per-diem/category caps? currency converted right? **duplicate?** (Cosmos lookup: same amount + person + near date).
- **LLM + RAG**: query the AI Search policy index — "is alcohol reimbursable for this cost center?", "does this look personal?" — and **cite the policy passage**.
Output: `RiskAssessment` { risk: low|med|high, flags: [...], policy_citations: [...] }.
Rule: never let the LLM be the sole fraud gate — pair it with hard logic.

### Orchestrator / Approval — the manager's assistant
Model: rules-first + small model for edge cases. Input: `ExtractionResult` + `RiskAssessment`. Does: routes by company rules (auto-approve < $X and low-risk; else escalate). Escalation calls a Logic App/Approvals tool → Teams card/email → workflow **pauses (HITL)** → **resumes** on response → triggers ERP post + notifies submitter. Output: `Decision` { approved: bool, route, approver, posted: bool }.

### Insights (optional) — the analyst
Model: small model + Code Interpreter. Scheduled. Queries Cosmos for the period, produces spend-by-category summaries / anomaly counts → Power BI dataset.

---

## 6. Build plan — phased, Cowork-ready

Build in thin vertical slices. Each phase below is one Cowork session.

### Phase 0 — Provision (IaC)
**Goal:** the whole resource set stands up from `az deployment`.
**Tasks:** Bicep for Foundry project + Agent Service, Cosmos, Blob, AI Search, Logic App (Standard), App Insights, Managed Identities; wire VNet + private endpoints.
**Deliverable:** `infra/` that deploys clean.
**Done:** `az deployment sub create ...` succeeds; all resources visible in the portal.
> **Tell Cowork:** *"Using §3 and §4 of the master guide, write Bicep in `infra/` that provisions a Foundry project + Agent Service, Cosmos DB, Blob Storage, Azure AI Search, a Standard Logic App, and Application Insights, each with a Managed Identity, inside a VNet with private endpoints. Give me a single `main.bicep` with modules and a deploy command."*

### Phase 1 — Extraction — DONE ✅
The `expense_extractor/` module is built and logic-tested. **Your only remaining task: one real end-to-end test.** `az login`, set `AZURE_OPENAI_ENDPOINT`, deploy `gpt-5.4-mini` and `gpt-5.4`, run it against a real receipt, confirm the API version string matches your resource.
> **Tell Cowork:** *"Help me deploy gpt-5.4-mini and gpt-5.4 in my Foundry resource, set the env vars from expense_extractor/README.md, and run the extractor against tests/samples/receipt.jpg. Fix any auth or API-version errors."*

### Phase 2 — Validation
**Goal:** the Validator scores risk and cites real policy.
**Tasks:** index the expense policy in AI Search; build `agents/validator.py` with deterministic checks first (totals, caps, duplicates via `tools/duplicate_check.py`), then the RAG policy check via `tools/policy_search.py`. Emit `RiskAssessment`.
**Deliverable:** Validator agent + tools + tests that *should* fail (over-limit meal, duplicate, mismatched total) and are caught.
**Done:** feeding an `ExtractionResult` returns a correct risk + flags with citations.
> **Tell Cowork:** *"Build the Validator agent from §5 of the master guide. It takes the Extractor's ExtractionResult, runs deterministic checks (total=sum of items, per-diem caps, duplicate lookup in Cosmos), then a RAG check against my policy index in Azure AI Search, and returns a RiskAssessment with flags and policy citations. Add pytest cases for an over-limit meal, a duplicate claim, and a total mismatch."*

### Phase 3 — Orchestration + approvals
**Goal:** auto-approve the easy path; real human approval for escalations, with pause/resume.
**Tasks:** `agents/orchestrator.py` with tiered thresholds; wire the escalation to a Teams card / email via Logic Apps; get **HITL pause and resume** working.
**Deliverable:** Orchestrator + the approval Logic App.
**Done:** low-risk claim auto-approves; high-risk pauses, waits for a Teams approval, resumes correctly. (Trickiest phase — test the callback hard.)
> **Tell Cowork:** *"Build the Orchestrator agent and the approval flow. Auto-approve low-risk claims under a threshold. For escalations, send a Teams Adaptive Card via Logic Apps, pause the Agent Framework workflow (human-in-the-loop), and resume on the approve/reject response. Show me how the checkpointing keeps state across the wait."*

### Phase 4 — Post + notify
**Goal:** approved claims hit the system of record and the submitter is told.
**Tasks:** `tools/erp_post.py` (a stub Function is fine for a demo); notify submitter; mark Cosmos record `posted`.
**Done:** approval → GL write → notification → record updated.
> **Tell Cowork:** *"Add the post-to-ERP step (a stub Azure Function GL write is fine) and a submitter notification, and mark the Cosmos record posted. Wire it as the final workflow node after approval."*

### Phase 5 — Observability + evals
**Goal:** full end-to-end tracing + quality as a live signal.
**Tasks:** connect Foundry tracing to App Insights; confirm drill-down from "run failed" to the exact agent/tool/step; add **Foundry Evaluations** (tool-call accuracy, task adherence) + continuous monitoring.
**Done:** you can trace one receipt across all agents; evals score each run.
> **Tell Cowork:** *"Wire Foundry tracing to Application Insights and confirm I can trace one receipt across Extractor → Validator → Orchestrator. Then set up Foundry Evaluations for tool-call accuracy and task adherence with continuous monitoring."*

### Phase 6 — Polish
**Goal:** reproducible + demoable.
**Tasks:** Bicep in source control; GitHub Actions to deploy infra + publish agent definitions; optional manager UI / Power BI dashboard off Cosmos; record demo video (ingest → agent chain → approval → report).
> **Tell Cowork:** *"Set up GitHub Actions that deploy the Bicep and publish the agent definitions, and scaffold a simple Power BI dataset from Cosmos for monthly spend + policy violations."*

### Phase 7 — Feedback loop
When a human corrects a rejected/misread expense, log the correction and use it to refine a policy rule, sharpen a prompt, or add a labeled sample.

---

## 7. Cost

Main drivers: model tokens (biggest), Logic App runs, storage/search. Your extractor pays **model tokens only** — no per-page Document Intelligence charge — because you went vision-direct.

Keep it cheap: small models for routine steps (Validator/Orchestrator), big model only on escalation; rasterize PDFs at 150–200 DPI (higher DPI = more tokens); free/low tiers for AI Search + Cosmos in dev; autoscale to zero when idle; **budget alerts** in Cost Management; cap agent-loop iterations so a runaway can't burn credit. For a demo, realistic cost is **cents per report** — the thing that blows budgets is an infinite loop, not per-doc pricing.

---

## 8. Security & governance

- **Prompt injection:** the document is untrusted. The Extractor already treats page text as data, not instructions, and has no tools. Keep that discipline downstream: side-effectful actions (approvals, ERP writes) sit behind **deterministic gates**, never the model's say-so. Turn on **Foundry XPIA guardrails** on every LLM call.
- **Network:** private VNet + private endpoints for App Service, Functions, Search, Cosmos.
- **Identity:** Entra Agent ID + Managed Identities everywhere; Azure AD auth for Foundry. No API keys.
- **Content Safety** filters on generative outputs.
- **Data:** encrypt receipts at rest + in transit; keep in-region/tenant (receipts hold personal data).
- **Audit** everything via Azure Monitor; keep a record of every agent decision.
- Workflows **async + idempotent** (a retried receipt must not double-post).

---

## 9. Testing & evaluation

Throw the hard stuff at it: blurry/skewed scans; multi-page + itemized folios; foreign currency; missing/unreadable totals; total ≠ sum of items; duplicate submission; deliberate policy breach (must escalate); injected-instruction receipt (must be ignored). Watch latency (vision + escalation adds seconds). Use **Foundry Evaluations** as a regression gate so a prompt tweak that quietly breaks the Validator gets caught. Grow `tests/samples/` with every new edge case you hit.

---

## 10. Working with Claude Cowork

Cowork is built for exactly this kind of multi-step, multi-file build. How to get the most from it:

- **One phase per session.** Paste the phase's "Tell Cowork" prompt; let it work across the files, run code, and iterate. Phases are ordered so each builds on the last.
- **Keep the context in the folder.** This guide + `expense_extractor/` in your project folder means Cowork sees the contracts (`ExtractionResult`, etc.) and stays consistent across agents.
- **Let it run the tests.** After each phase, have Cowork run the pytest suite and fix failures before moving on.
- **Connectors:** if your policy docs live in Google Drive or your tasks in monday.com, Cowork can pull from those directly while building — point it at them.
- **Review the side-effect steps yourself.** Approvals, ERP writes, and anything that deletes or sends: read those before you let them run against real systems.

---

## Quick-start checklist

- [ ] `infra/` Bicep stands up all resources (Phase 0)
- [ ] gpt-5.4-mini + gpt-5.4 deployed; extractor passes a real receipt (Phase 1)
- [ ] Policy indexed in AI Search; Validator returns risk + citations (Phase 2)
- [ ] Deterministic duplicate + total-mismatch + cap checks passing
- [ ] Auto-approve path works; escalation pauses & resumes on Teams approval (Phase 3)
- [ ] Post-to-ERP + submitter notification (Phase 4)
- [ ] End-to-end trace in App Insights; Foundry Evaluations wired (Phase 5)
- [ ] XPIA guardrails on; extracted text never treated as instructions
- [ ] CI/CD deploys infra + agents; README + demo video (Phase 6)

---

### Sources
Product state verified against Microsoft Foundry blog (Agent Service GA, Mar 2026), Microsoft Agent Framework 1.0 GA (Apr 3, 2026), Microsoft Learn (Agent Framework workflows, Foundry Agent Service, model catalog), and 2026 Azure pricing references. Extractor design and per-agent model strategy developed in this project.
