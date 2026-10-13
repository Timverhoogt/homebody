/* Shared native project lane; no storage, raw HTML, primary-session discovery or model consent. */
(() => {
  const byId = (id) => document.getElementById(id);
  // `pending` is a write or catalog refresh and locks the controls; `polling` is a read-only poll and never
  // touches them (disabling an open <select> closes it in Chromium, which made the picker flicker).
  let epoch = 0, enabled = false, pending = false, polling = false, uncertain = false, payload = null, controller = null;
  let repositories = null, repositoryChoice = "";
  const active = new Set(["submitting", "running", "stopping", "disconnected", "verifying", "submission_unknown"]);
  const feedbackButtons = { bind: "workspace-native-bind", prepare: "workspace-native-prepare", approve: "workspace-native-approve" };
  // Display-only bench (scene.js). It reads categorical fields of the current payload, never text or ids.
  const bench = window.HomebodyScene?.mount(byId("workspace-native-scene"), { variant: "strip", label: "Project session" }) || null;
  function verifiedReady(run, binding) {
    return run?.status === "completed" && run.ready_to_test === true && run.receipt?.verified === true
      && run.receipt.native_run_id === run.run_id && run.receipt.native_session_id === binding?.session_id;
  }
  function renderBench() {
    bench?.update(window.HomebodyScene.workbenchView({
      offline: window.HomebodyOwner?.isOwner() !== true,
      showing: enabled,
      configured: Boolean(byId("workspace-native-target").options.length),
      bound: Boolean(payload?.binding) && selectionMatchesBinding(),
      pending: Boolean(payload?.pending) && selectionMatchesBinding(),
      status: selectionMatchesBinding() ? payload?.run?.status || "" : "",
      verified: selectionMatchesBinding() && verifiedReady(payload?.run, payload?.binding),
      uncertain,
    }));
  }
  function reset() {
    Object.values(feedbackButtons).forEach((id) => window.HomebodyMotion?.clear(byId(id)));
    epoch += 1;
    controller?.abort();
    controller = null;
    pending = false;
    polling = false;
    enabled = false;
    uncertain = false;
    payload = null;
    repositories = null;
    repositoryChoice = "";
    byId("workspace-repository").replaceChildren();
    byId("workspace-repository-state").textContent = "Tap Show conversation above to load your repositories.";
    byId("workspace-native-identity").textContent = "No native project selected.";
    byId("workspace-native-state").textContent = "Tap Show conversation above, then choose a repository and Open project.";
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
  function selectedRepository() {
    return repositories?.repositories?.find((row) => row.repository === byId("workspace-repository").value);
  }
  function selectionMatchesBinding() {
    const repo = selectedRepository();
    if (repositories?.available === false || (repositoryChoice && !repo)) return false;
    return !repo || repo.target_ids.includes(payload?.binding?.target_id);
  }
  // Options are replaced only when the list really changed, so polls keep an open picker and its value.
  function syncOptions(select, rows) {
    const options = select.options;
    if (options.length === rows.length && rows.every(([value, label], i) => options[i].value === value && options[i].textContent === label)) return;
    const previous = select.value;
    select.replaceChildren(...rows.map(([value, label]) => {
      const option = document.createElement("option"); option.value = value; option.textContent = label; return option;
    }));
    if (rows.some(([value]) => value === previous)) select.value = previous;
  }
  function setDisabled(id, value) {
    if (byId(id).disabled !== value) byId(id).disabled = value;
  }
  function renderRepositories() {
    syncOptions(byId("workspace-repository"), [["", "Choose a repository…"], ...(repositories?.repositories || []).map((repo) => [repo.repository,
      `${repo.repository}${repo.private ? " · Private" : ""}${repo.archived ? " · Archived" : ""}${repo.target_ids.length ? " · Session available" : ""}`])]);
    if (repositoryChoice && [...byId("workspace-repository").options].some(o => o.value === repositoryChoice)
        && byId("workspace-repository").value !== repositoryChoice) byId("workspace-repository").value = repositoryChoice;
    const repo = selectedRepository();
    byId("workspace-repository-state").textContent = repo
      ? repo.target_ids.length ? `${repo.repository}: a project session is available. Tap Open project; no work starts until you review and approve a request.`
        : `${repo.repository}: listed on GitHub, but no project session is configured on the host yet. Selection does not clone code or enable tools.`
      : repositories?.available ? `${repositories.repositories.length} repositories from ${repositories.owner}. Checked ${new Date(repositories.checked_at * 1000).toLocaleTimeString()}.${repositories.possibly_truncated ? " List limit reached (500)." : ""}`
        : repositories?.error || "Enable Workspace and tap Show conversation above to load your repositories.";
  }
  function renderTargets() {
    const repo = selectedRepository();
    syncOptions(byId("workspace-native-target"), (payload?.targets || [])
      .filter((target) => repositories?.available !== false && (!repositoryChoice || repo)
        && (!repo || repo.target_ids.includes(target.target_id)))
      .map((target) => [target.target_id, `${target.title} · ${target.backend}`]));
  }
  function controls() {
    const busy = active.has(payload?.run?.status);
    const modeActive = byId("agent-enable-button").getAttribute("aria-pressed") === "true";
    byId("workspace-enable-mode").hidden = modeActive;
    byId("workspace-enable-mode").disabled = byId("agent-enable-button").disabled;
    byId("workspace-mode-help").textContent = !modeActive
      ? "Tap Enable Workspace here, or Workspace beside Everyday at the top of this screen."
      : !enabled ? "Workspace is enabled. Tap Show conversation above to load and select your projects."
        : "Choose a GitHub repository, then Open project. Selection does not start work.";
    setDisabled("workspace-repository", !enabled || pending || busy || !repositories?.available);
    setDisabled("workspace-repositories-reload", !enabled || pending || busy);
    setDisabled("workspace-native-bind", !enabled || pending || busy || !payload || !byId("workspace-native-target").options.length);
    setDisabled("workspace-native-prepare", !enabled || pending || busy || (!payload?.binding || !selectionMatchesBinding()));
    setDisabled("workspace-native-approve", !enabled || pending || (!payload?.pending || !selectionMatchesBinding()));
    setDisabled("workspace-native-stop", !enabled || (!busy && !pending && !uncertain));
    setDisabled("workspace-native-target", !enabled || pending || busy);
    setDisabled("workspace-native-request", !enabled || pending || busy || (!payload?.binding || !selectionMatchesBinding()));
    renderBindHelp();
    renderBench();
    window.HomebodyAssistantModes?.sync();
  }
  /** Why Open project is (un)available, next to the button; text only, so polls never touch the pickers. */
  function renderBindHelp() {
    const repo = selectedRepository();
    const help = !enabled ? "Show conversation first. Opening a project is a separate, explicit step."
      : uncertain ? "Project connection unconfirmed. Open project returns once Reachy reconnects."
        : !payload ? "Checking project sessions…"
          : repo && !repo.target_ids.length
            ? `Setup needed: ${repo.repository} has no project session on the agent host. Map it in `
              + "REACHY_GITHUB_PROJECT_MAP_JSON to a target in REACHY_NATIVE_TARGETS_JSON and restart the bridge. "
              + "Open project stays off until then."
            : !payload.targets?.length
              ? "Setup needed: no project session is configured on the agent host (REACHY_NATIVE_TARGETS_JSON). "
                + "Open project stays off until one is added."
              : payload.binding && selectionMatchesBinding()
                ? `Open: ${payload.binding.backend} · ${payload.binding.project_id}. Opening a project does not submit work.`
                : "Open project connects this page to the selected session; opening it does not submit work.";
    if (byId("workspace-native-bind-help").textContent !== help) byId("workspace-native-bind-help").textContent = help;
  }
  /** Display-only facts for the Workspace mode card; never fetches or grants anything. */
  function summary() {
    const binding = payload?.binding;
    return {
      repository: selectedRepository()?.repository || "",
      session: !enabled ? "unchecked" : uncertain ? "unconfirmed" : !payload ? "checking"
        : binding && selectionMatchesBinding() ? "bound" : binding ? "other" : "none",
      label: binding ? `${binding.backend} · ${binding.project_id}` : "",
      uncertain,
    };
  }
  function render(next) {
    payload = next;
    uncertain = false;
    renderTargets();
    const binding = next.binding;
    byId("workspace-native-identity").textContent = binding
      ? `${binding.backend} · ${binding.project_id} · ${binding.session_id}` : "No native project selected.";
    const run = next.run || {};
    byId("workspace-native-verification").hidden = !run.receipt;
    byId("workspace-native-receipt").textContent = run.receipt ? JSON.stringify(run.receipt, null, 2) : "";
    byId("workspace-native-state").textContent = verifiedReady(run, binding)
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
    const approval = byId("workspace-native-approval");
    const revealed = approval.hidden && Boolean(next.pending);
    approval.hidden = !next.pending;
    if (revealed) window.HomebodyMotion?.enter(approval);
    byId("workspace-native-request-summary").textContent = next.pending
      ? `Request: ${next.pending.text || "See the full approved scope below."}` : "";
    byId("workspace-native-scope").textContent = next.pending ? JSON.stringify(next.pending, null, 2) : "";
    controls();
  }
  async function request(action = "read", fields = {}) {
    if (["prepare", "approve"].includes(action) && !selectionMatchesBinding()) return;
    if (action === "bind" && ![...byId("workspace-native-target").options].some(o => o.value === fields.target_id)) return;
    const poll = ["read", "show"].includes(action);
    if (!enabled || (poll && (pending || polling))) return;
    if (pending && action !== "cancel") return;
    // A write or refresh supersedes an in-flight poll; the poll's late answer is fenced out below.
    controller?.abort();
    const abort = new AbortController();
    controller = abort;
    polling = poll;
    pending = !poll;
    const mine = epoch;
    // Button feedback follows the server's answer to this exact request; stale answers just clear it.
    const feedback = feedbackButtons[action] ? window.HomebodyMotion?.pending(byId(feedbackButtons[action])) : null;
    let outcome = "cleared";
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
      if (action === "repositories") { repositories = next; renderRepositories(); renderTargets(); }
      else if (["read", "show", "bind"].includes(action)) render(next);
      else if (action === "prepare") render({...payload, pending: next});
      else render({...payload, run: next, pending: null});
      outcome = "confirmed";
    } catch (error) {
      if (mine !== epoch || controller !== abort) return;
      outcome = "failed";
      if (action === "repositories") {
        repositories = {available: false, repositories: [], error: "GitHub repositories unavailable. Reload to try again."};
        renderRepositories(); renderTargets(); return;
      }
      byId("workspace-native-timeline").replaceChildren();
      byId("workspace-native-approval").hidden = true;
      byId("workspace-native-scope").textContent = "";
      byId("workspace-native-request-summary").textContent = "";
      byId("workspace-native-receipt").textContent = "";
      byId("workspace-native-verification").hidden = true;
      byId("workspace-native-request").value = "";
      payload = null;
      uncertain = true;
      // Nothing from the last answer is still confirmed: no session identity or openable target remains.
      byId("workspace-native-identity").textContent = "Project session unconfirmed. Reconnecting…";
      renderTargets();
      byId("workspace-native-state").textContent = "Connection unavailable. Native work may still be running; reconnect to reconcile.";
      if (action !== "read") window.HomebodyNotifications?.show(String(error.message || error), {
        id: "native-workspace", title: "Project session", kind: "error",
      });
    } finally {
      clearTimeout(timer);
      window.HomebodyMotion?.settle(feedback, mine === epoch ? outcome : "cleared");
      if (controller === abort) {
        controller = null; pending = false; polling = false; controls();
        if (outcome === "confirmed" && !repositories && ["show", "read"].includes(action)) {
          queueMicrotask(() => { if (mine === epoch && enabled) request("repositories"); });
        }
      }
    }
  }
  byId("workspace-enable-mode").addEventListener("click", () => byId("agent-enable-button").click());
  byId("workspace-repository").addEventListener("change", () => {
    repositoryChoice = byId("workspace-repository").value;
    renderRepositories(); renderTargets(); controls();
  });
  byId("workspace-repositories-reload").addEventListener("click", () => request("repositories"));
  byId("workspace-native-bind").addEventListener("click", () => request("bind", {target_id: byId("workspace-native-target").value}));
  byId("workspace-native-prepare").addEventListener("click", () => request("prepare", {text: byId("workspace-native-request").value}));
  byId("workspace-native-approve").addEventListener("click", () => request("approve", {approval_id: payload?.pending?.approval_id}));
  byId("workspace-native-stop").addEventListener("click", () => request("cancel"));
  window.HomebodyNativeWorkspace = {
    reset,
    summary,
    /** Repaint the bench from current state (owner/display changes); never fetches. */
    sync: renderBench,
    update(displayEnabled) {
      if (!displayEnabled || byId("agent-enable-button").getAttribute("aria-pressed") !== "true") { if (enabled) reset(); else renderBench(); return; }
      const first = !enabled;
      enabled = true;
      controls();
      if (!byId("panel-agent").hidden && !byId("agent-workspace-view").hidden) request(first ? "show" : "read");
    },
  };
  reset();
})();
