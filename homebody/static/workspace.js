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
  function controls() {
    byId("workspace-start").disabled = blocked || pending || showing;
    byId("workspace-clear").disabled = blocked || writing;
  }
  function reset(message = "Conversation display is off.") {
    epoch += 1;
    window.HomebodyNativeWorkspace?.reset();
    showing = false;
    signature = "";
    byId("workspace-timeline").replaceChildren();
    byId("workspace-badge").textContent = "Not showing";
    byId("workspace-state").textContent = message;
    controls();
  }
  function render(payload) {
    showing = payload.enabled === true;
    window.HomebodyNativeWorkspace?.update(showing);
    byId("workspace-badge").textContent = showing ? "Showing live" : "Not showing";
    const events = Array.isArray(payload.events) ? payload.events : [];
    const next = `${payload.generation}:${events.map((event) => event.id).join(",")}`;
    if (signature !== next) {
      const timeline = byId("workspace-timeline");
      const atBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 70;
      timeline.replaceChildren();
      for (const event of events) {
        const row = document.createElement("li");
        const role = ["user", "assistant"].includes(event.role) ? event.role : "activity";
        row.className = `workspace-event workspace-${role}`;
        const label = document.createElement("strong");
        label.textContent = role === "user" ? "You" : role === "assistant" ? "Hermes" : "Activity";
        const body = document.createElement("p");
        body.textContent = String(event.text || "");
        row.append(label, body);
        if (event.truncated) {
          const note = document.createElement("small");
          note.textContent = "Display limit reached; this message is shortened.";
          row.append(note);
        }
        timeline.append(row);
      }
      if (!events.length) {
        const empty = document.createElement("li");
        empty.className = "workspace-empty";
        empty.textContent = showing
          ? "Ready for the next voice turn. Say “Hey Homebody” when Reachy is listening."
          : "Your conversation will appear here when you choose to show it.";
        timeline.append(empty);
      }
      if (atBottom) timeline.scrollTop = timeline.scrollHeight;
      signature = next;
    }
    byId("workspace-state").textContent = showing
      ? "Showing accepted voice messages. Replies may be interrupted during playback."
      : "Conversation display is off. Nothing is being retained here.";
    controls();
  }
  async function request(action = "read") {
    if (blocked || document.querySelector("main").hidden) return;
    if (pending) {
      if (action === "read" || writing) return;
      // Clear must not wait for a slow poll; its old response cannot repaint the DOM.
      controller?.abort();
    }
    if (action === "read" && (byId("panel-agent").hidden || byId("agent-workspace-view").hidden)) return;
    pending = true;
    writing = action !== "read";
    const abort = new AbortController();
    controller = abort;
    const timer = setTimeout(() => abort.abort(), 8000);
    const mine = epoch;
    controls();
    try {
      const response = await fetch(`/api/agent/workspace${action === "read" ? "" : `/${action}`}`, {
        method: action === "read" ? "GET" : "POST", cache: "no-store",
        signal: abort.signal,
      });
      const payload = await response.json();
      if (mine !== epoch) return;
      if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
      render(payload);
    } catch (error) {
      if (mine === epoch) reset("Conversation unavailable. Reconnect as owner to continue.");
      if (action !== "read") window.HomebodyNotifications?.show(String(error.message || error), {
        id: "workspace", title: "Conversation display", kind: "error",
      });
    } finally {
      clearTimeout(timer);
      if (controller === abort) {
        controller = null;
        pending = false;
        writing = false;
        controls();
      }
    }
  }
  function select(view) {
    byId("agent-companion-view").hidden = view !== "companion";
    byId("agent-workspace-view").hidden = view !== "workspace";
    for (const name of ["companion", "workspace"]) {
      byId(`agent-view-${name}`).setAttribute("aria-pressed", String(name === view));
    }
    if (view === "workspace") request();
  }
  byId("agent-view-companion").addEventListener("click", () => select("companion"));
  byId("agent-view-workspace").addEventListener("click", () => select("workspace"));
  byId("workspace-start").addEventListener("click", () => request("start"));
  byId("workspace-clear").addEventListener("click", () => {
    reset("Clearing this conversation…");
    request("clear");
  });
  window.HomebodyWorkspace = {
    reset,
    show: () => select("workspace"),
    update(runtime) {
      const nextBlocked = Boolean(runtime.kids_mode?.active || runtime.kids_mode?.locked)
        || ["meeting", "sleep"].includes(runtime.power_mode);
      if (nextBlocked) reset("Conversation hidden in Kids or privacy mode.");
      blocked = nextBlocked;
      controls();
      if (!blocked && runtime.agent?.pending_approval) select("workspace");
      if (!blocked) request();
    },
  };
  controls();
})();
