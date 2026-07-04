# Expense IDP — Intelligent Workflow Automation Agent

An agentic **Intelligent Document Processing** pipeline for expense reports: a receipt/invoice
flows through a vision-LLM **Extractor** → a compliance **Validator** → a routing **Orchestrator**
that auto-approves low-risk claims and routes the rest to a human (Teams/email) with pause/resume,
then posts to the ledger and notifies the submitter — every step typed and traceable.

Built on **Azure OpenAI (vision)** + **Microsoft Agent Framework** graph workflows, per the
[master guide](docs/Expense-IDP-Master-Guide.md). Designed **local-first, cloud-ready**: the whole
pipeline runs and is tested offline with a deterministic mock model (no Azure, no tokens), and the
*same code* runs against Azure by flipping one env var.

```
Ingest ─▶ Validate ─▶ Decide ─┬─ auto_approve ─▶ post to ERP ─▶ notify
  (vision   (checks +          ├─ reject        ─▶ notify
  extract)   policy RAG)       └─ escalate      ─▶ Teams/email approval (HITL pause) ─▶ resume ─▶ post/notify
```

## Why some things differ from the guide (engineering notes)

- **The model is a config value** (`FOUNDRY_MODEL`, `FOUNDRY_MODEL_ESCALATION`), never hardcoded — so
  "which model" is a deployment detail, not a code change. Defaults are the guide's `gpt-5.4-mini` (vision)
  → `gpt-5.4` (escalation), which are GenerallyAvailable in Azure as of 2026-03. The older `gpt-4o` /
  `gpt-4.1` families are now in *Deprecating* status and can't be freshly deployed — pick a GA model
  (`az cognitiveservices model list --location <region>`).
- **Deterministic gates, not model say-so.** Totals, caps, FX, and duplicate detection are pure
  Python functions (fully unit-tested). The LLM is never the sole fraud gate (guide §8).
- **Injection-safe by construction.** The Extractor has *no tools*, treats the document as data, and
  a jailbroken receipt can't change the transcribed total or the routing decision.

## Quick start (offline, no Azure needed)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[agents,dev]"

pytest -q          # 40 tests, all green, no cloud
python demo.py     # watch the full agent chain across 5 scenarios
```

`python demo.py path/to/receipt.jpg` extracts a single document (mock unless Azure is configured).

## Repository layout

| Path | What | Guide phase |
|---|---|---|
| `expense_extractor/schemas.py` | Typed handoffs: `ExtractionResult`, `RiskAssessment`, `Decision`, … | §1 contracts |
| `expense_extractor/extractor.py` | Provider-agnostic, injection-safe extraction logic + `MockVisionModel` | Phase 1 |
| `expense_extractor/agent.py` | Real Agent Framework `Agent` + Foundry vision model | Phase 1 |
| `tools/checks.py` | Deterministic checks: total==sum, caps, FX | Phase 2 |
| `tools/duplicate_check.py` | Cosmos-style duplicate lookup | Phase 2 |
| `tools/policy_search.py` | Policy RAG (offline keyword + AI Search interface) | Phase 2 |
| `tools/stores.py` | `RecordStore` (Cosmos-shaped) + local file impl | Phase 4 |
| `tools/erp_post.py`, `tools/notify.py` | ERP/GL post + submitter notification | Phase 4 |
| `agents/validator.py` | Compliance officer → `RiskAssessment` (+ LLM policy judge) | Phase 2 |
| `agents/orchestrator.py` | Rules-first routing + HITL approval contract | Phase 3 |
| `workflow/pipeline.py` | **The `WorkflowBuilder` graph** wiring it all, with switch-case + HITL | Phase 3–4 |
| `infra/` | Bicep (Foundry, Cosmos, Search, Storage, Logic App, App Insights) | Phase 0 |
| `tests/` | 40 offline tests incl. the guide's required failing cases | §9 |

## Going live on Azure

1. `az login`, then provision infra: `az deployment sub create ...` (see [`infra/README.md`](infra/README.md)).
2. Deploy a vision model (e.g. `gpt-4o-mini`) and a bigger one (`gpt-4o`) in your Foundry project.
3. Copy `.env.example` → `.env`, set `EXPENSE_MODEL_BACKEND=foundry` + the endpoints. **No API keys** —
   auth is `DefaultAzureCredential` (az login locally, Managed Identity in Azure).
4. `python demo.py tests/samples/receipt.jpg` now runs the real vision model.

Auth uses Entra/Managed Identity throughout; the LLM policy judge and vision extractor are the only
components that talk to Azure — everything else (checks, routing, records) is deterministic.

## Status vs. the guide's checklist

- [x] Typed contracts (`ExtractionResult` → `RiskAssessment` → `Decision`)
- [x] Vision Extractor with escalation; injection-safe; offline-testable (Phase 1)
- [x] Validator: deterministic checks + policy RAG + citations (Phase 2)
- [x] Deterministic duplicate + total-mismatch + cap checks passing (Phase 2)
- [x] Auto-approve path; escalation pauses & resumes on approval (Phase 3)
- [x] Post-to-ERP + submitter notification + record status (Phase 4)
- [x] `WorkflowBuilder` graph backbone with conditional edge
- [ ] Bicep provisions all resources (Phase 0) — *in progress*
- [ ] App Insights tracing + Foundry Evaluations (Phase 5)
- [ ] CI/CD + demo video (Phase 6)

## Testing

`pytest -q` runs the full suite offline. It includes the guide's §9 hard cases: over-limit meal,
duplicate submission, total ≠ sum of items, foreign currency, blurry→escalate, non-receipt→reject,
and an injection-laced document that must be ignored. Grow `tests/samples/` with every new edge case.
