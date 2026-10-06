/* Shared native project lane; no storage, raw HTML, primary-session discovery or model consent. */
(() => {
  const byId = (id) => document.getElementById(id);
  let epoch = 0, enabled = false, pending = false, uncertain = false, payload = null, controller = null;
  const active = new Set(["submitting", "running", "stopping", "disconnected", "verifying", "submission_unknown"]);
  function reset() {
    epoch += 1;
    controller?.abort();
    controller = null;
    pending = false;
    enabled = false;
    uncertain = false;
    payload = null;
    byId("workspace-native-identity").textContent = "No native project selected.";
    byId("workspace-native-state").textContent = "Show the conversation to select an owner-registered session.";
    byId("workspace-native-timeline").replaceChildren();
    byId("workspace-native-scope").textContent = "";
    byId("workspace-native-request-summary").textContent = "";
    byId("workspace-native-receipt").textContent = "";
    byId("workspace-native-verification").hidden = true;
    byId("workspace-native-approval").hidden = true;
    byId("workspace-native-target").replaceChildren();
    byId("workspace-native-request").value = "";
    controls();
  }
  function controls() {
    const busy = active.has(payload?.run?.status);
    byId("workspace-native-bind").disabled = !enabled || pending || busy || !payload?.targets?.length;
    byId("workspace-native-prepare").disabled = !enabled || pending || busy || !payload?.binding;
    byId("workspace-native-approve").disabled = !enabled || pending || !payload?.pending;
    byId("workspace-native-stop").disabled = !enabled || (!busy && !pending && !uncertain);
    byId("workspace-native-target").disabled = !enabled || pending || busy;
    byId("workspace-native-request").disabled = !enabled || pending || busy || !payload?.binding;
  }
  function render(next) {
    payload = next;
    uncertain = false;
    const select = byId("workspace-native-target");
    const previous = select.value;
    select.replaceChildren();
    for (const target of next.targets || []) {
      const option = document.createElement("option");
      option.value = target.target_id;
      option.textContent = `${target.title} · ${target.backend} · ${target.session_id}`;
      select.append(option);
    }
    if ([...select.options].some((option) => option.value === previous)) select.value = previous;
    const binding = next.binding;
    byId("workspace-native-identity").textContent = binding
      ? `${binding.backend} · ${binding.project_id} · ${binding.session_id}` : "No native project selected.";
    const run = next.run || {};
    byId("workspace-native-verification").hidden = !run.receipt;
    byId("workspace-native-receipt").textContent = run.receipt ? JSON.stringify(run.receipt, null, 2) : "";
    byId("workspace-native-state").textContent = run.status === "completed" && run.ready_to_test === true && run.receipt?.verified === true
      && run.receipt.native_run_id === run.run_id && run.receipt.native_session_id === binding?.session_id
      ? `Ready to test · ${run.run_id} · fixed checks passed; artifact hashes recorded.`
      : run.status ? `${run.status} · ${run.run_id || "native admission unconfirmed"} · Not verified ready to test.`
        : next.targets?.length ? "Select an existing session. Sending work requires exact approval."
          : "Native sessions are not configured on the host. No coding permission has been enabled.";
    const timeline = byId("workspace-native-timeline");
    timeline.replaceChildren();
    for (const message of [...(next.messages || []), ...(next.events || [])]) {
      const row = document.createElement("li");
      row.className = `workspace-event workspace-${message.role === "user" ? "user" : "assistant"}`;
      const label = document.createElement("strong");
      label.textContent = message.role === "user" ? "You" : message.role === "activity" ? "Activity" : binding?.backend || "Agent";
      const body = document.createElement("p");
      body.textContent = String(message.text || "");
      row.append(label, body);
      timeline.append(row);
    }
    byId("workspace-native-approval").hidden = !next.pending;
    byId("workspace-native-request-summary").textContent = next.pending
      ? `Request: ${next.pending.text || "See the full approved scope below."}` : "";
    byId("workspace-native-scope").textContent = next.pending ? JSON.stringify(next.pending, null, 2) : "";
    controls();
  }
  async function request(action = "read", fields = {}) {
    if (!enabled || (pending && action === "read")) return;
    if (pending && action !== "cancel") return;
    controller?.abort();
    const abort = new AbortController();
    controller = abort;
    pending = true;
    const mine = epoch;
    controls();
    const timer = setTimeout(() => abort.abort(), 25000);
    try {
      const response = await fetch(`/api/agent/workspace/native/${action}`, {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(fields),
        cache: "no-store", signal: abort.signal,
      });
      const next = await response.json();
      if (mine !== epoch || controller !== abort) return;
      if (!response.ok) throw new Error(next.detail || `HTTP ${response.status}`);
      if (["read", "show", "bind"].includes(action)) render(next);
      else if (action === "prepare") render({...payload, pending: next});
      else render({...payload, run: next, pending: null});
    } catch (error) {
      if (mine !== epoch || controller !== abort) return;
      byId("workspace-native-timeline").replaceChildren();
      byId("workspace-native-approval").hidden = true;
      byId("workspace-native-scope").textContent = "";
    byId("workspace-native-request-summary").textContent = "";
      byId("workspace-native-receipt").textContent = "";
      byId("workspace-native-verification").hidden = true;
      byId("workspace-native-request").value = "";
      payload = null;
      uncertain = true;
      byId("workspace-native-state").textContent = "Connection unavailable. Native work may still be running; reconnect to reconcile.";
      if (action !== "read") window.HomebodyNotifications?.show(String(error.message || error), {
        id: "native-workspace", title: "Project session", kind: "error",
      });
    } finally {
      clearTimeout(timer);
      if (controller === abort) { controller = null; pending = false; controls(); }
    }
  }
  byId("workspace-native-bind").addEventListener("click", () => request("bind", {target_id: byId("workspace-native-target").value}));
  byId("workspace-native-prepare").addEventListener("click", () => request("prepare", {text: byId("workspace-native-request").value}));
  byId("workspace-native-approve").addEventListener("click", () => request("approve", {approval_id: payload?.pending?.approval_id}));
  byId("workspace-native-stop").addEventListener("click", () => request("cancel"));
  window.HomebodyNativeWorkspace = {
    reset,
    update(displayEnabled) {
      if (!displayEnabled) { if (enabled) reset(); return; }
      const first = !enabled;
      enabled = true;
      controls();
      if (!byId("panel-agent").hidden && !byId("agent-workspace-view").hidden) request(first ? "show" : "read");
    },
  };
  reset();
})();
