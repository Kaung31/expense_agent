/* Expense IDP frontend — plain JS, no dependencies, no browser storage.
   All state lives on the server; this file only renders API responses. */

"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

// ── Tabs ──────────────────────────────────────────────────────────────────
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    refresh(btn.dataset.tab);
  });
});

function refresh(tab) {
  if (tab === "queue") loadQueue();
  if (tab === "history") loadHistory();
  if (tab === "dashboard") loadDashboard();
}

// ── Submit (drag & drop) ──────────────────────────────────────────────────
const dropzone = $("#dropzone");
const fileInput = $("#file-input");

dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  if (e.dataTransfer.files.length) submitFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener("change", () => {
  if (fileInput.files.length) submitFile(fileInput.files[0]);
  fileInput.value = "";
});

async function submitFile(file) {
  const progress = $("#submit-progress");
  const result = $("#submit-result");
  progress.classList.remove("hidden");
  result.classList.add("hidden");
  $("#progress-text").textContent = `Extracting ${file.name} — vision model + checks + routing…`;

  const form = new FormData();
  form.append("file", file);
  form.append("submitter", $("#submitter").value || "web-user@corp.com");

  try {
    const res = await fetch("/api/expenses", { method: "POST", body: form });
    const data = await res.json();
    progress.classList.add("hidden");
    if (!res.ok) throw new Error(data.detail || res.statusText);
    result.innerHTML = renderSubmitResult(data);
    result.classList.remove("hidden");
    pollQueueBadge();
  } catch (err) {
    progress.classList.add("hidden");
    result.innerHTML = `<div class="card"><h4>❌ Failed</h4><p>${esc(err.message)}</p></div>`;
    result.classList.remove("hidden");
  }
}

function renderSubmitResult(data) {
  const r = data.record || {};
  const status = data.status;
  const headline = {
    auto_approved: "✅ Auto-approved & posted",
    pending_approval: "⏸ Escalated — waiting in the approval queue",
    rejected: "❌ Rejected",
    escalated_approved: "✅ Approved after review",
  }[status] || esc(status);

  const checks = (r.risk?.checks || []).map((c) =>
    `<li class="${c.passed ? "check-ok" : "check-fail"}">${c.passed ? "✔" : "✘"} ${esc(c.name)} — ${esc(c.detail)}</li>`
  ).join("");
  const flags = (r.risk?.flags || []).map((f) => `<li>⚑ [${esc(f.severity)}] ${esc(f.code)}: ${esc(f.message)}</li>`).join("");

  return `<div class="card">
    <h4>${headline}</h4>
    <div class="meta">
      ${esc(r.vendor || "unknown vendor")} · ${esc(r.total || "?")} ${esc(r.currency || "")} ·
      ${esc(r.category || "")} · ${esc(r.expense_date || "no date")} ·
      model: ${esc(r.extraction?.model || "?")}${r.extraction?.escalated ? " (escalated)" : ""}
    </div>
    <div>Risk: <span class="chip ${esc(r.risk?.level)}">${esc(r.risk?.level || "?")}</span>
         &nbsp; Status: <span class="chip ${esc(r.status)}">${esc(r.status)}</span></div>
    <h3>Checks</h3><ul class="flags">${checks || "<li>none</li>"}</ul>
    ${flags ? `<h3>Flags</h3><ul class="flags">${flags}</ul>` : ""}
    ${status === "pending_approval" ? '<p class="hint">Open the <strong>Approval queue</strong> tab to decide.</p>' : ""}
  </div>`;
}

// ── Approval queue ────────────────────────────────────────────────────────
async function loadQueue() {
  const res = await fetch("/api/approvals");
  const items = await res.json();
  const list = $("#queue-list");
  $("#queue-empty").classList.toggle("hidden", items.length > 0);
  list.innerHTML = items.map((r) => `
    <div class="card" data-id="${esc(r.id)}">
      <h4>${esc(r.vendor || "unknown")} — ${esc(r.total || "?")} ${esc(r.currency || "")}</h4>
      <div class="meta">${esc(r.submitter || "")} · ${esc(r.category || "")} · ${esc(r.expense_date || "")} ·
        risk <span class="chip ${esc(r.risk?.level)}">${esc(r.risk?.level)}</span></div>
      <ul class="flags">${(r.risk?.flags || []).map((f) => `<li>⚑ ${esc(f.code)}: ${esc(f.message)}</li>`).join("")}</ul>
      <div class="actions">
        <button class="approve" data-approve="1">Approve</button>
        <button class="reject" data-approve="0">Reject</button>
      </div>
    </div>`).join("");
  updateBadge(items.length);

  list.querySelectorAll("button[data-approve]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".card");
      card.querySelectorAll("button").forEach((b) => (b.disabled = true));
      try {
        const res = await fetch(`/api/approvals/${card.dataset.id}/decide`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ approved: btn.dataset.approve === "1", approver: "web-approver@corp.com" }),
        });
        if (!res.ok) {
          const err = await res.json();
          throw new Error(err.detail || res.statusText);
        }
        await loadQueue();
      } catch (err) {
        alert(`Decision failed: ${err.message}`);
        card.querySelectorAll("button").forEach((b) => (b.disabled = false));
      }
    });
  });
}

function updateBadge(n) {
  const badge = $("#queue-badge");
  badge.textContent = n;
  badge.classList.toggle("hidden", n === 0);
}

async function pollQueueBadge() {
  try {
    const res = await fetch("/api/approvals");
    updateBadge((await res.json()).length);
  } catch { /* server briefly unavailable — badge refreshes on next poll */ }
}

// ── History ───────────────────────────────────────────────────────────────
async function loadHistory() {
  const res = await fetch("/api/expenses");
  const items = await res.json();
  $("#history-empty").classList.toggle("hidden", items.length > 0);
  $("#history-body").innerHTML = items.map((r) => `
    <tr class="clickable" data-id="${esc(r.id)}">
      <td>${esc(r.vendor || "?")}</td>
      <td>${esc(r.expense_date || "")}</td>
      <td>${esc(r.category || "")}</td>
      <td>${esc(r.total || "?")} ${esc(r.currency || "")}</td>
      <td><span class="chip ${esc(r.risk?.level)}">${esc(r.risk?.level || "–")}</span></td>
      <td><span class="chip ${esc(r.status)}">${esc(r.status)}</span></td>
      <td>›</td>
    </tr>`).join("");
  document.querySelectorAll("#history-body tr").forEach((tr) => {
    tr.addEventListener("click", () => showDetail(tr.dataset.id));
  });
}

async function showDetail(id) {
  const res = await fetch(`/api/expenses/${id}`);
  if (!res.ok) return;
  const r = await res.json();
  const checks = (r.risk?.checks || []).map((c) =>
    `<li class="${c.passed ? "check-ok" : "check-fail"}">${c.passed ? "✔" : "✘"} ${esc(c.name)} — ${esc(c.detail)}</li>`).join("");
  const flags = (r.risk?.flags || []).map((f) => `<li>⚑ [${esc(f.severity)}] ${esc(f.code)}: ${esc(f.message)}</li>`).join("");
  const cits = (r.risk?.citations || []).map((c) =>
    `<div class="citation"><span class="src">[${esc(c.source)}]</span> ${esc(c.passage)}</div>`).join("");
  const items = (r.line_items || []).map((li) => `<li>${esc(li.description)} — ${esc(li.amount)}</li>`).join("");
  $("#detail-content").innerHTML = `
    <h2>${esc(r.vendor || "unknown")} <span class="chip ${esc(r.status)}">${esc(r.status)}</span></h2>
    <dl class="kv">
      <dt>Record</dt><dd>${esc(r.id)}</dd>
      <dt>Submitter</dt><dd>${esc(r.submitter || "")}</dd>
      <dt>Amount</dt><dd>${esc(r.total || "?")} ${esc(r.currency || "")}
        ${r.risk?.total_base ? `(≈ ${esc(r.risk.total_base)} ${esc(r.risk.base_currency)})` : ""}</dd>
      <dt>Date / category</dt><dd>${esc(r.expense_date || "?")} · ${esc(r.category || "")}</dd>
      <dt>Model</dt><dd>${esc(r.extraction?.model || "?")}${r.extraction?.escalated ? " (escalated)" : ""}
        · confidence ${esc(r.extraction?.confidence ?? "?")}</dd>
      ${r.decision ? `<dt>Decision</dt><dd>${r.decision.approved ? "approved" : "not approved"} by
        ${esc(r.decision.approver || "?")} — ${esc(r.decision.reason)} ${r.decision.posted ? "· posted to GL" : ""}</dd>` : ""}
    </dl>
    ${items ? `<h3>Line items</h3><ul class="flags">${items}</ul>` : ""}
    <h3>Checks</h3><ul class="flags">${checks || "<li>none</li>"}</ul>
    ${flags ? `<h3>Flags</h3><ul class="flags">${flags}</ul>` : ""}
    ${cits ? `<h3>Policy citations</h3>${cits}` : ""}`;
  $("#detail-modal").showModal();
}
$("#detail-close").addEventListener("click", () => $("#detail-modal").close());

// ── Dashboard ─────────────────────────────────────────────────────────────
async function loadDashboard() {
  const res = await fetch("/api/stats");
  const s = await res.json();
  $("#stat-total").textContent = s.total_claims;
  $("#stat-rate").textContent = s.approval_rate == null ? "–" : `${Math.round(s.approval_rate * 100)}%`;
  $("#stat-flagged").textContent = s.flagged_count;
  $("#stat-pending").textContent = s.by_status?.pending_approval || 0;
  renderBars("#category-bars", s.spend_by_category, (v) => `${v.toFixed(2)} ${s.base_currency}`);
  renderBars("#status-bars", s.by_status, (v) => `${v}`);
}

function renderBars(sel, obj, fmt) {
  const entries = Object.entries(obj || {});
  const max = Math.max(1, ...entries.map(([, v]) => v));
  $(sel).innerHTML = entries.length
    ? entries.sort((a, b) => b[1] - a[1]).map(([k, v]) => `
        <div class="bar-row">
          <span>${esc(k)}</span>
          <div class="bar-track"><div class="bar-fill" style="width:${(v / max) * 100}%"></div></div>
          <span class="bar-val">${fmt(v)}</span>
        </div>`).join("")
    : '<p class="hint">No data yet.</p>';
}

// ── Boot ──────────────────────────────────────────────────────────────────
(async function boot() {
  try {
    const res = await fetch("/health");
    const h = await res.json();
    $("#backend-pill").textContent = `backend: ${h.backend}`;
  } catch {
    $("#backend-pill").textContent = "backend: offline?";
  }
  pollQueueBadge();
  setInterval(pollQueueBadge, 10000); // keep the queue badge fresh (Teams callbacks land async)
})();
