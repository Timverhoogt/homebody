(() => {
  "use strict";
  const badge = document.getElementById("agent-connection-badge");
  const label = document.getElementById("agent-connection-label");
  const detail = document.getElementById("agent-connection-detail");
  const retry = document.getElementById("agent-connection-retry");
  let timer = 0;
  let deadline = 0;
  let currentLabel = "Agent";
  function update(body = {}) {
    window.clearTimeout(timer);
    const states = new Set(["connected", "checking", "unavailable", "unconfigured", "unknown"]);
    let state = states.has(body.state) ? body.state : "unknown";
    currentLabel = body.label === "OpenClaw" ? "OpenClaw" : body.label === "Hermes" ? "Hermes" : "Agent";
    const fresh = Number(body.fresh_for_ms);
    if (state === "connected" && (!Number.isFinite(fresh) || fresh <= 0)) state = "checking";
    const text = {
      connected: `${currentLabel} connected`, checking: "Checking connection…",
      unavailable: `${currentLabel} unavailable`, unconfigured: "No agent connected", unknown: "Connection unknown",
    }[state];
    badge.dataset.state = state;
    label.textContent = text;
    badge.setAttribute("aria-label", `${text}. Open agent connection settings.`);
    detail.textContent = typeof body.detail === "string" ? body.detail : "Waiting for a fresh connection check.";
    retry.disabled = state === "checking" || state === "unknown";
    deadline = state === "connected" ? Date.now() + Math.min(fresh, 25000) : 0;
    if (deadline) timer = window.setTimeout(expire, Math.min(fresh, 25000));
  }
  function expire() {
    if (deadline && Date.now() >= deadline) {
      update({state: "checking", label: currentLabel, detail: "Connection status is stale. Waiting for a fresh check."});
    }
  }
  badge.addEventListener("click", () => {
    document.getElementById("tab-settings").click();
    const section = document.getElementById("agent-connection-details");
    section.focus();
    section.scrollIntoView({block: "center", behavior: "auto"});
  });
  retry.addEventListener("click", async () => {
    update({state: "checking", label: currentLabel, detail: "Checking the current agent connection…"});
    try {
      const response = await fetch("/api/agent-connection/retry", {method: "POST"});
      if (!response.ok) throw new Error("Connection check unavailable");
      update(await response.json());
    } catch (_) {
      update({state: "unavailable", label: currentLabel, detail: "Cannot check the connection. Restore app access and try again."});
    }
  });
  document.addEventListener("visibilitychange", expire);
  window.HomebodyAgentConnection = {
    update,
    offline: () => update({state: "unavailable", label: currentLabel, detail: "The robot app is unreachable. Connection status cannot be verified."}),
  };
  update();
})();
