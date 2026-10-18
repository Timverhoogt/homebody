/* One-time household setup. Owner-only; explicit choices; saving never captures or listens. */
(() => {
  const byId = id => document.getElementById(id);
  const CHOICES = ["camera_on_request", "following_and_movement", "voice_power_controls", "kids_ispy_camera"];
  let owner = false;
  let kids = false;
  let generation = 0;
  let busy = false;
  let status = null;

  function say(text) { byId("household-message").textContent = text; }

  function controls() {
    const version = status && Number.isInteger(status.disclosure?.version) ? status.disclosure.version : null;
    const camera = byId("household-camera_on_request").checked;
    const kidsBox = byId("household-kids_ispy_camera");
    kidsBox.disabled = busy || !camera;
    if (!camera) kidsBox.checked = false;
    for (const name of CHOICES.slice(0, 3)) byId(`household-${name}`).disabled = busy || version === null;
    byId("household-ack").disabled = busy || version === null;
    byId("household-approve").disabled = busy || version === null || !byId("household-ack").checked;
    byId("household-revoke").disabled = busy || status?.completed !== true;
    byId("household-camera-off").disabled = busy || status?.camera_enabled !== true;
  }

  function clear() {
    status = null;
    if (byId("household-credential")) byId("household-credential").value = "";
    if (byId("household-url")) byId("household-url").value = "";
    if (byId("household-connection-message")) byId("household-connection-message").textContent = "";
    byId("household-badge").textContent = "Checking…";
    byId("household-state").textContent = "Checking the saved household choices…";
    byId("household-camera-state").textContent = "Camera access not reported.";
    byId("household-disclosure").replaceChildren();
    byId("household-excluded").textContent = "";
    for (const name of CHOICES) byId(`household-${name}`).checked = false;
    byId("household-ack").checked = false;
    byId("household-review").open = false;
    controls();
  }

  function render(body) {
    const valid = body && typeof body === "object" && typeof body.completed === "boolean";
    status = valid ? body : null;
    if (!valid) {
      clear();
      byId("household-badge").textContent = "Not reported";
      byId("household-state").textContent = "Household setup status is not reported. Nothing was changed.";
      return;
    }
    const done = body.completed === true;
    byId("household-badge").textContent = done ? "Approved" : "Not set up";
    const when = done && Number(body.completed_at) > 0 ? ` on ${new Date(body.completed_at * 1000).toLocaleDateString()}` : "";
    byId("household-state").textContent = done
      ? `Approved${when}. Reachy uses these choices without asking again in each conversation.`
      : "Not set up. Reachy only looks when asked with phrases like “look at this”. Pairing this phone did not approve anything.";
    byId("household-camera-state").textContent = body.camera_enabled === true
      ? "Camera access: on. Looks happen only for a request, one still frame at a time."
      : "Camera access: off. Reachy takes no pictures, whatever else is approved.";
    const items = Array.isArray(body.disclosure?.items) ? body.disclosure.items : [];
    byId("household-disclosure").replaceChildren(...items.filter(item => typeof item?.text === "string").map(item => {
      const li = document.createElement("li");
      li.textContent = item.text;
      return li;
    }));
    const excluded = Array.isArray(body.disclosure?.excluded) ? body.disclosure.excluded.filter(x => typeof x === "string") : [];
    byId("household-excluded").textContent = excluded.length ? `Never included: ${excluded.join(", ")}.` : "";
    const choices = body.choices && typeof body.choices === "object" ? body.choices : {};
    for (const name of CHOICES) byId(`household-${name}`).checked = done && choices[name] === true;
    byId("household-ack").checked = false;
    controls();
  }

  async function request(path, init) {
    const mine = ++generation;
    busy = true;
    controls();
    try {
      const response = await fetch(path, init);
      const body = await response.json().catch(() => ({}));
      if (mine !== generation || !owner) return null;
      if (!response.ok) {
        say(typeof body.detail === "string" ? body.detail : "That did not work. Nothing was changed.");
        return null;
      }
      render(body);
      return body;
    } catch {
      if (mine === generation) say("Reachy could not be reached. Nothing was changed.");
      return null;
    } finally {
      if (mine === generation) { busy = false; controls(); }
    }
  }

  function post(path, payload) {
    return request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload || {}) });
  }

  function connectionMessage(body) {
    if (!body.connected) return "External tools disconnected.";
    return body.tools_active === true
      ? "Connected — tools active within current adult permissions."
      : "Connected — tools inactive. Start an approved adult Everyday session, then reconnect explicitly.";
  }

  async function load() {
    if (!owner || kids) return null;
    const result = await request("/api/household/setup", { headers: { Accept: "application/json" } });
    const mine = generation;
    try {
      const response = await fetch("/api/household/tools/status");
      const body = await response.json();
      if (response.ok && mine === generation && owner && !kids && typeof body.url === "string") {
        byId("household-url").value = body.url;
        byId("household-durable").checked = body.enabled === true;
        byId("household-connection-message").textContent = connectionMessage(body);
      }
    } catch { /* Keep setup independent of connection status. */ }
    return result;
  }

  function visible() {
    byId("household-card").hidden = !owner || kids;
  }

  byId("household-camera_on_request").addEventListener("change", controls);
  byId("household-ack").addEventListener("change", controls);
  byId("household-approve").addEventListener("click", async () => {
    if (!status || !byId("household-ack").checked) return;
    const payload = { disclosure_version: status.disclosure.version, acknowledged: true };
    for (const name of CHOICES) payload[name] = byId(`household-${name}`).checked;
    say("Saving the household choices…");
    if (await post("/api/household/setup", payload)) {
      say("Saved. No picture was taken and listening did not start.");
      byId("household-review").open = false;
    }
  });
  byId("household-revoke").addEventListener("click", async () => {
    say("Revoking…");
    if (await post("/api/household/revoke")) say("Household approval revoked. Explicit phrases are needed again.");
  });
  byId("household-camera-off").addEventListener("click", async () => {
    say("Turning the camera off…");
    if (await post("/api/household/camera-off")) say("Camera access is off. Turn it on again in Settings.");
  });

  async function connectionAction(action) {
    if (!owner || kids) return;
    const mine = generation;
    const payload = action === "configure" ? {
      url: byId("household-url").value.trim(),
      robot_credential: byId("household-credential").value,
      enabled: byId("household-durable").checked,
      acknowledged: byId("household-connection-ack").checked,
    } : {};
    byId("household-credential").value = "";
    try {
      const response = await fetch(`/api/household/tools/${action}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      const body = await response.json().catch(() => ({}));
      if (mine !== generation || !owner || kids) return;
      byId("household-connection-message").textContent = response.ok
        ? ({configure: "Connection saved. No listening or capture started.", validate: "Household service validated; shared speech services were not checked.", connect: connectionMessage(body), disconnect: "External tools disconnected."})[action]
        : (typeof body.detail === "string" ? body.detail : "Connection action refused. Check Owner approval and saved host settings.");
    } catch {
      if (mine === generation && owner && !kids) byId("household-connection-message").textContent = "Household connection unavailable.";
    }
  }
  for (const [button, action] of [["save", "configure"], ["validate", "validate"], ["connect", "connect"], ["disconnect", "disconnect"]]) {
    byId(`household-connection-${button}`)?.addEventListener("click", () => connectionAction(action));
  }

  window.HomebodyHousehold = {
    owner(value) {
      const next = value === true;
      if (next === owner) return;
      owner = next;
      generation += 1;  // owner loss drops any reply already in flight
      busy = false;
      clear();
      visible();
      if (owner) load();
    },
    update(payload) {
      const kidsMode = payload?.runtime?.kids_mode;
      const next = Boolean(kidsMode && (kidsMode.active === true || kidsMode.locked === true));
      if (next === kids) return;
      kids = next;
      generation += 1;
      if (kids && byId("household-credential")) byId("household-credential").value = "";
      visible();
      if (!kids) load();
    },
    reload: load,
  };
  clear();
  if (window.HomebodyOwner?.isOwner?.() === true) window.HomebodyHousehold.owner(true);
})();
