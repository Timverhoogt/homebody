/* Owner-only conversation display. No browser storage, tool authority or microphone start. */
(() => {
  const byId = (id) => document.getElementById(id);
  let epoch = 0;
  let pending = false;
  let writing = false;
  let controller = null;
  let blocked = true;
  let showing = false;
  let signature = "";
  let voiceBusy = false;
  // Set only by HomebodyAssistantModes: 'workspace' once status confirms it; anything else fails closed.
  let mode = "unknown";
  const visible = () => !byId("panel-agent").hidden && !document.hidden;
  let connectFailed = false;
  let connecting = false;
  function controls() {
    byId("workspace-start").disabled = blocked || pending || showing;
    byId("workspace-start").hidden = showing;
    byId("workspace-clear").disabled = blocked || writing;
    if (byId("workspace-clear").disabled) byId("workspace-clear-confirm").hidden = true;
    // The Project pane reads one attribute; its Connect forwards to the header button (one dispatch).
    byId("panel-agent").dataset.connection = showing ? "on" : connecting ? "pending" : connectFailed ? "error" : "off";
    const connect = byId("workspace-project-connect");
    connect.disabled = byId("workspace-start").disabled;
    connect.lastChild.textContent = connecting ? "Connecting…" : connectFailed ? "Retry Connect" : "Connect";
    byId("workspace-project-connect-help").textContent = connecting ? "Connecting to your agent host…"
      : connectFailed ? byId("workspace-state").textContent
        : "Shows the conversation and loads your repositories. Nothing starts.";
    window.HomebodyAssistantModes?.sync();
  }
  function reset(message = "Not connected. Tap Connect to show this conversation.") {
    epoch += 1;
    window.HomebodyMotion?.clear(byId("workspace-start"));
    window.HomebodyNativeWorkspace?.reset();
    showing = false;
    signature = "";
    window.HomebodyWorkspaceThread?.setVoice(null);
    byId("workspace-badge").textContent = "Not connected";
    byId("workspace-state").textContent = message;
    byId("workspace-clear-confirm").hidden = true;
    controls();
  }
  function render(payload) {
    showing = payload.enabled === true;
    byId("workspace-badge").textContent = showing ? "Live" : "Not connected";
    const events = Array.isArray(payload.events) ? payload.events : [];
    const next = `${payload.generation}:${showing}:${events.map((event) => event.id).join(",")}`;
    if (signature !== next) {
      window.HomebodyWorkspaceThread?.setVoice(payload);
      signature = next;
    }
    byId("workspace-state").textContent = showing
      ? "Connected. Spoken and typed turns appear here."
      : "Not connected. Tap Connect to show this conversation.";
    // The native lane polls on its own cadence; this only tells it whether the display is on.
    window.HomebodyNativeWorkspace?.tick(showing, voiceBusy);
    controls();
  }
  async function request(action = "read") {
    if (blocked || document.querySelector("main").hidden) return;
    if (pending) {
      if (action === "read" || writing) return;
      // Clear must not wait for a slow poll; its old response cannot repaint the DOM.
      controller?.abort();
    }
    // Off screen nothing is read or rendered; the native lane refreshes as soon as the view returns.
    if (action === "read" && !visible()) { window.HomebodyNativeWorkspace?.away(); return; }
    pending = true;
    writing = action !== "read";
    connecting = action === "start";
    const abort = new AbortController();
    controller = abort;
    const timer = setTimeout(() => abort.abort(), 8000);
    const mine = epoch;
    // Only Connect gets button feedback, and it confirms only if the display really turned on.
    const feedback = action === "start" ? window.HomebodyMotion?.pending(byId("workspace-start")) : null;
    let outcome = "cleared";
    controls();
    try {
      const response = await fetch(`/api/agent/workspace${action === "read" ? "" : `/${action}`}`, {
        method: action === "read" ? "GET" : "POST", cache: "no-store",
        signal: abort.signal,
      });
      const payload = await response.json();
      if (mine !== epoch) return;
      if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
      if (action === "start") connectFailed = payload.enabled !== true;
      render(payload);
      if (payload.enabled === true) outcome = "confirmed";
      // Clear keeps its backend scope (stop + disconnect); say exactly what is needed to continue.
      if (action === "clear") {
        byId("workspace-state").textContent = "Cleared. Selected project work was asked to stop and the project "
          + "is disconnected. To continue, tap Connect, then Open project again.";
      }
    } catch (error) {
      if (mine !== epoch) return;
      outcome = "failed";
      if (action === "start") connectFailed = true;
      reset(action === "start"
        ? `Could not connect: ${String(error.message || error)}. Tap Retry Connect.`
        : "Conversation unavailable. Reconnect as owner to continue.");
      if (action !== "read") window.HomebodyNotifications?.show(String(error.message || error), {
        id: "workspace", title: "Conversation display", kind: "error",
      });
    } finally {
      clearTimeout(timer);
      window.HomebodyMotion?.settle(feedback, outcome);
      if (controller === abort) {
        controller = null;
        pending = false;
        writing = false;
        connecting = false;
        controls();
      }
    }
  }
  /** Phones show one pane (Chat, Work or Project); wider layouts show all three and ignore this. */
  function pane(name) {
    byId("agent-workspace-view").dataset.pane = name;
    document.querySelectorAll("[data-ws-pane]").forEach((button) => {
      button.setAttribute("aria-selected", String(button.dataset.wsPane === name));
    });
  }
  document.querySelectorAll("[data-ws-pane]").forEach((button) => {
    button.addEventListener("click", () => pane(button.dataset.wsPane));
  });
  // One tap from the Workspace tab: switch the voice route if needed, then connect. Only an explicit
  // tab click sets this; startup, history and #hash restore never do.
  let entering = false;
  function continueEntry() {
    if (!entering || blocked || mode !== "workspace") return;
    entering = false;
    if (!showing && !pending) request("start");
  }
  byId("workspace-start").addEventListener("click", () => request("start"));
  // The Project pane's Connect is the same action: it forwards one click to the header button.
  byId("workspace-project-connect").addEventListener("click", () => {
    if (!byId("workspace-start").disabled) byId("workspace-start").click();
  });
  // Clear is destructive beyond the display, so it states its scope and asks first.
  byId("workspace-clear").addEventListener("click", () => {
    const confirm = byId("workspace-clear-confirm");
    const reveal = confirm.hidden;
    confirm.hidden = false;
    if (reveal) window.HomebodyMotion?.enter(confirm);
    byId("workspace-clear-cancel").focus();
  });
  byId("workspace-clear-cancel").addEventListener("click", () => {
    byId("workspace-clear-confirm").hidden = true;
    byId("workspace-clear").focus();
  });
  byId("workspace-clear-confirm-button").addEventListener("click", () => {
    if (blocked || writing) return;
    reset("Clearing the conversation and disconnecting the project…");
    request("clear");
  });
  window.HomebodyWorkspace = {
    reset,
    /** Navigation only: opens the Workspace destination; connecting stays an explicit tap. */
    show: () => window.activateTab?.("agent", false, true),
    pane,
    /** Explicit Workspace tab tap. A failed route switch leaves Everyday and its error message as they are. */
    enter() {
      entering = true;
      if (mode === "workspace") continueEntry();
      else if (mode === "everyday" && !byId("agent-enable-button").disabled) byId("agent-enable-button").click();
    },
    /** Assistant-mode authority: 'workspace' only when confirmed. Leaving it fences everything. */
    gate(next) {
      const previous = mode;
      mode = next;
      if (next === "workspace") { continueEntry(); return; }
      if (next === "pending") return;
      entering = false;
      // The host clears the display on every profile change; drop rendered content and late replies.
      if (previous === "workspace" || showing) reset("Workspace is not active. Choose Workspace to continue.");
    },
    /** Display-only facts for the Workspace header. */
    state: () => ({ showing, view: byId("panel-agent").hidden ? "other" : "workspace" }),
    update(runtime) {
      const nextBlocked = Boolean(runtime.kids_mode?.active || runtime.kids_mode?.locked)
        || ["meeting", "sleep"].includes(runtime.power_mode);
      if (nextBlocked) reset("Conversation hidden in Kids or privacy mode.");
      blocked = nextBlocked;
      voiceBusy = ["listening", "thinking", "looking", "speaking"].includes(runtime.state);
      controls();
      window.HomebodyNativeWorkspace?.sync();
      continueEntry();
      if (!blocked) request();
    },
  };
  controls();
})();
