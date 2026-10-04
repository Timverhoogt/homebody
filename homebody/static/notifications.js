/* Ephemeral, text-only feedback. No dependencies, storage, or robot authority. */
(() => {
  "use strict";
  const MAX_VISIBLE = 3;
  const MAX_ITEMS = 12;
  const items = new Map();
  const recent = new Map();
  const suppressed = new Set();
  let serial = 0;
  let region;
  let polite;
  let urgent;
  let queued;

  function ensureRegion() {
    if (region) return;
    region = document.createElement("section");
    region.id = "notifications";
    region.setAttribute("aria-label", "Notifications");
    region.className = "notifications";
    // Announce text separately; dismiss controls should not be read as message content.
    polite = document.getElementById("notification-status") || document.createElement("span");
    urgent = document.getElementById("notification-alert") || document.createElement("span");
    for (const [node, role] of [[polite, "status"], [urgent, "alert"]]) {
      node.className = "sr-only";
      node.setAttribute("role", role);
      node.setAttribute("aria-atomic", "true");
      if (!node.isConnected) document.body.appendChild(node);
    }
    queued = document.createElement("p");
    queued.className = "notification-queued";
    region.appendChild(queued);
    document.body.appendChild(region);
    placeRegion();
  }

  function pause(item) {
    if (!item.timer) return;
    clearTimeout(item.timer);
    item.timer = null;
    item.remaining = Math.max(0, item.remaining - (performance.now() - item.started));
  }

  function resume(item) {
    if (item.timer || item.remaining === null || item.node.hidden || item.hovered || item.focused || document.hidden) return;
    item.started = performance.now();
    item.timer = setTimeout(() => dismiss(item.id), item.remaining);
  }

  function render() {
    const ordered = [...items.values()];
    ordered.forEach((item, index) => {
      item.node.hidden = index >= MAX_VISIBLE;
      if (item.node.hidden) pause(item);
      else {
        if (item.announced !== item.text) {
          (item.kind === "error" ? urgent : polite).textContent = item.text;
          item.announced = item.text;
        }
        resume(item);
      }
    });
    if (!ordered.some(item => !item.node.hidden && item.kind === "error")) urgent.textContent = "";
    if (!ordered.some(item => !item.node.hidden && item.kind !== "error")) polite.textContent = "";
    const waiting = Math.max(0, items.size - MAX_VISIBLE);
    queued.hidden = !waiting;
    queued.textContent = `${waiting} more — dismiss a notification to see them`;
    region.appendChild(queued);
    region.hidden = !items.size;
  }

  function dismiss(id) {
    const item = items.get(id);
    if (!item) return;
    pause(item);
    const hadFocus = item.node.contains(document.activeElement);
    item.node.remove();
    items.delete(id);
    render();
    if (hadFocus) {
      const next = [...items.values()].find(entry => !entry.node.hidden);
      if (next) next.button.focus();
      else if (item.returnFocus?.isConnected) item.returnFocus.focus();
    }
  }

  function show(text, { id, kind = "info" } = {}) {
    if (suppressed.size) return null;
    text = String(text || "").trim();
    if (!text) return null;
    kind = ["pending", "ok", "error", "info"].includes(kind) ? kind : "info";
    ensureRegion();
    const signature = `${kind}:${text}`;
    // Repeated status polls and retries must not spawn or re-time the same notice.
    if (id && items.get(id)?.text === text && items.get(id)?.kind === kind) return id;
    if (!id && performance.now() - (recent.get(signature) ?? -Infinity) < 5000) return null;
    id ||= `notice-${++serial}`;
    let item = items.get(id);
    if (!item) {
      if (items.size >= MAX_ITEMS) {
        const expendable = [...items.values()].find(entry => entry.kind === "ok" || entry.kind === "info");
        if (!expendable) return null; // Inline feedback remains; never evict a pending operation or error.
        dismiss(expendable.id);
      }
      const node = document.createElement("div");
      const content = document.createElement("span");
      content.className = "notification-text";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "notification-dismiss";
      button.textContent = "×";
      button.setAttribute("aria-label", "Dismiss notification");
      button.addEventListener("click", () => dismiss(id));
      node.append(content, button);
      item = { id, node, content, button, timer: null, remaining: null, hovered: false, focused: false, returnFocus: document.activeElement };
      node.addEventListener("pointerenter", () => { item.hovered = true; pause(item); });
      node.addEventListener("pointerleave", () => { item.hovered = false; resume(item); });
      node.addEventListener("focusin", () => { item.focused = true; pause(item); });
      node.addEventListener("focusout", event => {
        if (!node.contains(event.relatedTarget)) { item.focused = false; resume(item); }
      });
      items.set(id, item);
      region.appendChild(node);
    }
    pause(item);
    item.kind = kind;
    item.text = text;
    item.content.textContent = text;
    item.node.className = `notification notification-${kind}`;
    item.remaining = kind === "error" || kind === "pending" ? null : 6000;
    recent.set(signature, performance.now());
    if (recent.size > 40) recent.delete(recent.keys().next().value);
    render();
    return id;
  }

  function clear() {
    for (const item of items.values()) { pause(item); item.node.remove(); }
    items.clear();
    recent.clear();
    if (region) region.hidden = true;
    if (polite) polite.textContent = "";
    if (urgent) urgent.textContent = "";
  }

  document.addEventListener("visibilitychange", () => {
    for (const item of items.values()) document.hidden ? pause(item) : resume(item);
  });
  function placeRegion() {
    if (!region) return;
    const parent = document.fullscreenElement || document.body;
    for (const node of [region, polite, urgent]) {
      if (node.parentElement !== parent) parent.appendChild(node);
    }
  }
  document.addEventListener("fullscreenchange", placeRegion);
  window.addEventListener("pagehide", clear);
  function setSuppressed(reason, value) {
    if (value) { suppressed.add(reason); clear(); }
    else suppressed.delete(reason);
  }
  window.HomebodyNotifications = { show, dismiss, clear, setSuppressed };
})();
