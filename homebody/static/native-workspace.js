/* Shared native project lane; no storage, raw HTML, primary-session discovery or model consent. */
(() => {
  const byId = (id) => document.getElementById(id);
  // `pending` is a write or catalog refresh and locks the controls; `polling` is a read-only poll and never
  // touches them (disabling an open <select> closes it in Chromium, which made the picker flicker).
  let epoch = 0, enabled = false, pending = false, polling = false, uncertain = false, payload = null, controller = null;
  let repositories = null, repositoryChoice = "", sessionKey = "";
  // One poll controller: the next read is due at `nextPollAt`; visibility returns and voice turns ending
  // make it due now. No interval timers exist, so nothing can stack across navigation.
  let nextPollAt = 0, wasVisible = false, wasVoiceBusy = false, payloadSignature = "";
  const active = new Set(["submitting", "running", "stopping", "disconnected", "verifying", "submission_unknown"]);
  const feedbackButtons = { bind: "workspace-native-bind", prepare: "workspace-native-prepare", approve: "workspace-native-approve" };
  // Display-only bench (scene.js). It reads categorical fields of the current payload, never text or ids.
  const bench = window.HomebodyScene?.mount(byId("workspace-native-scene"), { variant: "strip", label: "Project session" }) || null;
  const visible = () => !byId("panel-agent").hidden && !document.hidden;
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
  function clearWork() {
    byId("workspace-native-scope").textContent = "";
    byId("workspace-native-request-summary").textContent = "";
    byId("workspace-native-review").replaceChildren();
    byId("workspace-native-receipt").textContent = "";
    byId("workspace-native-checks").replaceChildren();
    byId("workspace-native-verification").hidden = true;
    byId("workspace-native-approval").hidden = true;
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
    payloadSignature = "";
    sessionKey = "";
    nextPollAt = 0;
    repositories = null;
    repositoryChoice = "";
    byId("workspace-repository").replaceChildren();
    byId("workspace-repository-state").textContent = "Connect to load your repositories.";
    byId("workspace-native-identity").textContent = "No project open.";
    byId("workspace-native-state").textContent = "Connect, then choose a repository to open its project session.";
    clearWork();
    byId("workspace-native-target").replaceChildren();
    byId("workspace-native-request").value = "";
    window.HomebodyWorkspaceThread?.setNative(null);
    window.HomebodyWorkspaceThread?.setStale(false);
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
      ? repo.target_ids.length ? `${repo.repository}: a project session is available.`
        : `${repo.repository}: listed on GitHub, but no project session is configured on the agent host yet.`
      : repositories?.available ? `${repositories.repositories.length} repositories from ${repositories.owner}. Checked ${new Date(repositories.checked_at * 1000).toLocaleTimeString()}.${repositories.possibly_truncated ? " List limit reached (500)." : ""}`
        : repositories?.error || "Connect to load your repositories.";
  }
  function renderTargets() {
    const repo = selectedRepository();
    syncOptions(byId("workspace-native-target"), (payload?.targets || [])
      .filter((target) => repositories?.available !== false && (!repositoryChoice || repo)
        && (!repo || repo.target_ids.includes(target.target_id)))
      .map((target) => [target.target_id, `${target.title} · ${target.backend}`]));
    // A single session needs no second picker; the Open button stays the explicit step.
    byId("workspace-native-target-row").hidden = byId("workspace-native-target").options.length < 2;
  }
  function controls() {
    const busy = active.has(payload?.run?.status);
    byId("workspace-enable-mode").disabled = byId("agent-enable-button").disabled;
    // Route truth belongs to AssistantModes, not the legacy switch button's render order.
    // This hint describes only the project/connection step.
    byId("workspace-mode-help").textContent = !enabled ? "Connect to load your repositories."
      : "Choose a repository, then open it. Opening never starts work.";
    setDisabled("workspace-repository", !enabled || pending || busy || !repositories?.available);
    setDisabled("workspace-repositories-reload", !enabled || pending || busy);
    setDisabled("workspace-native-bind", !enabled || pending || busy || uncertain || !payload || !byId("workspace-native-target").options.length);
    setDisabled("workspace-native-prepare", !enabled || pending || busy || uncertain || (!payload?.binding || !selectionMatchesBinding()));
    setDisabled("workspace-native-approve", !enabled || pending || uncertain || (!payload?.pending || !selectionMatchesBinding()));
    setDisabled("workspace-native-stop", !enabled || (!busy && !pending && !uncertain));
    setDisabled("workspace-native-target", !enabled || pending || busy);
    setDisabled("workspace-native-request", !enabled || pending || busy || (!payload?.binding || !selectionMatchesBinding()));
    // One attribute for the Project pane's state blocks; derived only from what is already known.
    byId("panel-agent").dataset.project = !enabled ? "none"
      : !repositories ? "loading"
        : repositories.available === false ? "error"
          : payload?.binding && selectionMatchesBinding() ? "open"
            : !(repositories.repositories || []).length ? "empty" : "ready";
    renderBindHelp();
    renderBench();
    window.HomebodyAssistantModes?.sync();
  }
  /** Why Open project is (un)available, next to the button; text only, so polls never touch the pickers. */
  function renderBindHelp() {
    const repo = selectedRepository();
    const help = !enabled ? "Connect first."
      : uncertain ? "Project connection unconfirmed. Open project returns once Reachy reconnects."
        : !payload ? "Checking project sessions…"
          : repo && !repo.target_ids.length
            ? `Setup needed: ${repo.repository} has no project session on the agent host yet. Open project stays off until then.`
            : !payload.targets?.length
              ? "Setup needed: no project session is configured on the agent host. Open project stays off until one is added."
              : payload.binding && selectionMatchesBinding()
                ? `Open: ${payload.binding.backend} · ${payload.binding.project_id}. `
                  + (payload.binding.voice_discussion === true ? "Spoken Workspace requests go to this session."
                    : "Voice discussion is off for this session on the agent host; spoken requests are not sent to it.")
                : "Open project connects this page to the selected session; opening it does not submit work.";
    if (byId("workspace-native-bind-help").textContent !== help) byId("workspace-native-bind-help").textContent = help;
  }
  /** Display-only facts for the Workspace header; never fetches or grants anything. */
  function summary() {
    const binding = payload?.binding;
    const run = payload?.run || {};
    return {
      repository: selectedRepository()?.repository || "",
      session: !enabled ? "unchecked" : uncertain ? "unconfirmed" : !payload ? "checking"
        : binding && selectionMatchesBinding() ? "bound" : binding ? "other" : "none",
      label: binding ? `${binding.backend} · ${binding.project_id}` : "",
      // Host-reported: does a spoken Workspace turn reach this session? Never inferred from display or Awake.
      voice: binding && selectionMatchesBinding() ? (binding.voice_discussion === true ? "native" : "off") : "none",
      run: verifiedReady(run, binding) ? "ready" : String(run.status || ""),
      uncertain,
    };
  }
  function definition(list, term, value) {
    const row = document.createElement("div");
    const dt = document.createElement("dt"); dt.textContent = term;
    const dd = document.createElement("dd");
    if (Array.isArray(value)) {
      const items = document.createElement("ul");
      value.forEach((entry) => { const li = document.createElement("li"); li.textContent = entry; items.append(li); });
      dd.append(items);
    } else dd.textContent = value;
    row.append(dt, dd);
    list.append(row);
  }
  /** Human review of the exact draft; the full draft (worker input included) stays in the disclosure. */
  function renderReview(draft) {
    const list = byId("workspace-native-review");
    list.replaceChildren();
    if (!draft) return;
    definition(list, "Request", String(draft.request || draft.text || "See the full scope below."));
    if (draft.project_root) definition(list, "Project folder", String(draft.project_root));
    if (Array.isArray(draft.verification_commands) && draft.verification_commands.length) {
      definition(list, "Checks afterwards", draft.verification_commands.map((command) => (Array.isArray(command) ? command : [command]).join(" ")));
    }
    if (Array.isArray(draft.artifacts) && draft.artifacts.length) definition(list, "Artifacts recorded", draft.artifacts.map(String));
    if (draft.notice) definition(list, "Note", String(draft.notice));
  }
  function renderReceipt(receipt) {
    const list = byId("workspace-native-checks");
    list.replaceChildren();
    if (!receipt) return;
    for (const check of receipt.checks || []) {
      const li = document.createElement("li");
      li.dataset.ok = String(check.exit_code === 0);
      li.textContent = `${check.exit_code === 0 ? "Passed" : `Failed (exit ${check.exit_code})`} · ${(check.argv || []).join(" ")}`;
      list.append(li);
    }
    for (const artifact of receipt.artifacts || []) {
      const li = document.createElement("li");
      li.dataset.ok = "true";
      li.textContent = `Artifact · ${artifact.path} · sha256 ${String(artifact.sha256 || "").slice(0, 12)}…`;
      list.append(li);
    }
    if (!list.children.length) {
      const li = document.createElement("li");
      li.dataset.ok = String(receipt.verified === true);
      li.textContent = receipt.verified === true ? "Verified" : "No passing checks were recorded.";
      list.append(li);
    }
  }
  function render(next) {
    const key = next.binding ? `${next.binding.target_id}:${next.binding.session_id}` : "";
    // A different session never inherits the previous one's messages, draft or receipt.
    // On phones the pane follows the task: choose a project, then talk; review opens Work (below).
    if (key !== sessionKey || !payload) window.HomebodyWorkspace?.pane(key ? "chat" : "project");
    if (key !== sessionKey) { sessionKey = key; payloadSignature = ""; clearWork(); }
    payload = next;
    uncertain = false;
    window.HomebodyWorkspaceThread?.setStale(false);
    renderTargets();
    const binding = next.binding;
    byId("workspace-native-identity").textContent = binding
      ? `${binding.backend} · ${binding.project_id} · session ${binding.session_id}` : "No project open.";
    const run = next.run || {};
    byId("workspace-native-verification").hidden = !run.receipt;
    byId("workspace-native-receipt").textContent = run.receipt ? JSON.stringify(run.receipt, null, 2) : "";
    renderReceipt(run.receipt);
    byId("workspace-native-state").textContent = verifiedReady(run, binding)
      ? `Ready to test · ${run.run_id} · fixed checks passed; artifact hashes recorded.`
      : run.status ? `${run.status} · ${run.run_id || "native admission unconfirmed"} · Not verified ready to test.`
        : binding ? "No work running. Describe a request below to review it before anything starts."
          : next.targets?.length ? "Choose a repository and open its project session."
            : "No project sessions are configured on the agent host.";
    byId("workspace-native-state").dataset.status = verifiedReady(run, binding) ? "ready" : String(run.status || "idle");
    // Messages and events only repaint when they changed; the thread keeps its nodes either way.
    const signature = JSON.stringify([key, next.messages, next.events]);
    if (signature !== payloadSignature) { payloadSignature = signature; window.HomebodyWorkspaceThread?.setNative(next); }
    const approval = byId("workspace-native-approval");
    const revealed = approval.hidden && Boolean(next.pending);
    approval.hidden = !next.pending;
    if (revealed) { window.HomebodyWorkspace?.pane("work"); window.HomebodyMotion?.enter(approval); }
    byId("workspace-native-request-summary").textContent = next.pending
      ? `Request: ${next.pending.request || next.pending.text || "See the full approved scope below."}` : "";
    byId("workspace-native-scope").textContent = next.pending ? JSON.stringify(next.pending, null, 2) : "";
    renderReview(next.pending);
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
      if (!response.ok) throw Object.assign(new Error(next.detail || `HTTP ${response.status}`), { status: response.status });
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
      if (!poll && error.status && error.status < 500 && payload) {
        // A refused write (busy, expired approval, wrong session) changes nothing; say why and keep the view.
        window.HomebodyNotifications?.show(String(error.message || error), { id: "native-workspace", title: "Project session", kind: "error" });
        return;
      }
      // Nothing from the last answer is still confirmed: actions wait, Stop stays available, and the
      // last messages stay visible but marked stale until a read succeeds again.
      uncertain = true;
      byId("workspace-native-approval").hidden = true;
      window.HomebodyWorkspaceThread?.setStale(true);
      if (!payload) byId("workspace-native-identity").textContent = "Project session unconfirmed. Reconnecting…";
      renderTargets();
      byId("workspace-native-state").textContent = "Connection unavailable. Native work may still be running; reconnect to reconcile.";
      byId("workspace-native-state").dataset.status = "uncertain";
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
  /** Poll cadence: fast while work or a voice turn is live, slow otherwise, none while hidden. */
  function due(voiceBusy) {
    const now = Date.now();
    const shown = visible();
    const returned = shown && !wasVisible;
    const turnEnded = wasVoiceBusy && !voiceBusy;
    wasVisible = shown;
    wasVoiceBusy = voiceBusy;
    if (!shown) return false;
    if (returned || turnEnded || now >= nextPollAt) {
      nextPollAt = now + (active.has(payload?.run?.status) || voiceBusy || uncertain ? 2000 : 6000);
      return true;
    }
    return false;
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
    /** Display state from each voice-display read; fetches only when the poll controller says so. */
    tick(displayEnabled, voiceBusy = false) {
      if (!displayEnabled || byId("agent-enable-button").getAttribute("aria-pressed") !== "true") { if (enabled) reset(); else renderBench(); return; }
      const first = !enabled;
      enabled = true;
      if (first) { controls(); wasVisible = visible(); nextPollAt = Date.now() + 6000; request("show"); return; }
      if (due(voiceBusy)) request("read");
    },
    /** The Workspace view is off screen; the next tick after it returns reads immediately. */
    away() { wasVisible = false; },
    /** Explicit refresh (display just enabled, or a caller needs fresh state now). */
    update(displayEnabled) {
      if (!displayEnabled || byId("agent-enable-button").getAttribute("aria-pressed") !== "true") { if (enabled) reset(); else renderBench(); return; }
      const first = !enabled;
      enabled = true;
      controls();
      if (visible()) { nextPollAt = Date.now() + 6000; request(first ? "show" : "read"); }
    },
  };
  reset();
})();
