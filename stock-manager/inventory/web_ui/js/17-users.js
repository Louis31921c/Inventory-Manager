"use strict";

// ---- users and roles ----------------------------------------------------------

function drawUsers(data) {
  const roles = data.roles ?? [];
  const keep = $("us-role").value;
  $("us-role").replaceChildren(...roles.map((role) => el("option", { value: role, textContent: role })));
  $("us-role").value = roles.includes(keep) ? keep : "buyer";
  renderTable($("table-users"), [
    { label: "Name", cell: (row) => td(row.user + (row.user === data.me ? " (you)" : "")) },
    { label: "Role", cell: (row) => {
      if (row.first) return td("admin (first account)");
      const pick = el("select", { className: "cell" }, roles.map((role) => el("option", { value: role, textContent: role })));
      pick.value = row.role;
      pick.addEventListener("change", async () => {
        try {
          drawUsers(await api(`/api/users/${encodeURIComponent(row.user)}/role`, putJson({ role: pick.value })));
          notice($("us-note"), "ok", `${row.user} is now ${pick.value}.`);
        } catch (e) { notice($("us-note"), "bad", e.message); }
      });
      return pick;
    } },
    { label: "Since", cell: (row) => td((row.created_at ?? "").slice(0, 10)) },
    { label: "", cell: (row) => {
      const fresh = el("input", { className: "cell", type: "password", size: 12, placeholder: "new password" });
      const reset = el("button", { className: "mini", type: "button", textContent: "set password" });
      reset.addEventListener("click", async () => {
        try {
          drawUsers(await api(`/api/users/${encodeURIComponent(row.user)}/password`, putJson({ password: fresh.value })));
          notice($("us-note"), "ok", `${row.user} has a new password.`);
        } catch (e) { notice($("us-note"), "bad", e.message); }
      });
      const parts = [fresh, reset];
      if (!row.first && row.user !== data.me) {
        parts.push(confirmButton("Remove", () => api(`/api/users/${encodeURIComponent(row.user)}`, { method: "DELETE" })));
      }
      return el("span", { className: "row tight" }, parts);
    } },
  ], data.users ?? [], "");
}

async function loadUsers() { try { drawUsers(await api("/api/users")); } catch {} }

$("us-add").addEventListener("click", async () => {
  try {
    drawUsers(await api("/api/users", postJson({ user: $("us-name").value, role: $("us-role").value, password: $("us-pass").value })));
    notice($("us-note"), "ok", `${$("us-name").value.toUpperCase()} can sign in now.`);
    $("us-name").value = "";
    $("us-pass").value = "";
  } catch (e) { notice($("us-note"), "bad", e.message); }
});

$("mp-save").addEventListener("click", async () => {
  try {
    await api("/api/account/password", postJson({ old: $("mp-old").value, new: $("mp-new").value }));
    $("mp-old").value = "";
    $("mp-new").value = "";
    notice($("mp-note"), "ok", "Your password is changed.");
  } catch (e) { notice($("mp-note"), "bad", e.message); }
});

async function refresh() {
  try {
    const data = await api("/api/data");
    notes = data.notes;
    lists = data.lists ?? [];
    flatten();

    const user = data.user ? String(data.user) : "";
    $("who").textContent = user ? `User ${user}` : "";
    if ($("status-user")) $("status-user").textContent = user;
    if ($("headline-count")) {
      $("headline-count").textContent =
        `${notes.length} note(s) · ${rows.length} line(s) · ${lists.length} purchase order(s)`;
    }
    $("tree-lines").textContent = `(${rows.length})`;
    $("tree-orders").textContent = `(${lists.length})`;
    say("Ready");

    renderSiteTree();
    renderDeliveries();
    renderOrders();
    renderExport();
    await Promise.all([
      renderStorage(), renderChecks(), renderModels(), renderErrors(), renderBackups(),
      renderAccuracy(), renderVocabulary(), renderAudit(), loadQueue(), loadDdmrp(), loadGate(),
      loadBills(), loadMailSettings(), loadBaskets(), loadStock(), loadTables(),
      loadQuotes(), loadRoute(), loadContacts(), loadWaiting(), loadMatch(), loadStockControl(), loadLeadTimes(), loadDashboard(), loadCalendar(), loadBillTo(), loadRequests(), loadConnections(), loadReceiving(), loadCounting(), loadUsers(), loadPolicies(), loadCharters(), loadJudge(), loadWallet(),
      loadPacks(), loadComing(),
    ]);
    notice($("global-notice"), "", "");
  } catch (e) {
    notice($("global-notice"), "bad", `Server unreachable: ${e.message}`);
    say(`Server unreachable: ${e.message}`, "bad");
  }
}
