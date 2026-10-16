/* Companion card: truthful state from /api/status, one explicit look per press, every reply fenced.
   Fences: an action generation advances on owner loss, Stop, offline, privacy/Meeting/Sleep, Kids and
   profile change. A reply admitted under an older generation is never rendered, and private text
   (answers, draft question) leaves the page on every teardown. Rendering never enables Workspace. */
(() => {
  const byId = (id) => document.getElementById(id);
  const card = byId("companion-card");
  if (!card) return;
  let actionGeneration = 0;      // advanced by every teardown; pending replies compare against it
  let ownerActive = false;       // mirrors the owner session; nothing renders or enables without it
  let lookPending = false;
  let lookAvailable = false;
  let contextLive = false;
  let editingLevel = false;
  let lastProfile = null;
  let lastPrivate = null;
  let lastKids = null;
  let catalog = null;
  let enrollPending = false;
  let lastRuntimeGeneration = null;
  let lastCameraPermission = null;
  let privateReadPending = false;
  let privateReadAt = 0;
  let privateExpiryTimer = null;

  const LEVEL_SETTINGS = {
    off: { initiative_policy_enabled: false, contextual_offers_enabled: false },
    gentle: { initiative_policy_enabled: true, initiative_mode: "balanced", contextual_offers_enabled: true },
    more_often: { initiative_policy_enabled: true, initiative_mode: "engaged", contextual_offers_enabled: true },
  };
  const ACTIVITY = {
    quiet: "Quiet", acknowledged: "Acknowledged", listening: "Listening", looking: "Looking",
    replying: "Replying", follow_up: "Follow-up open", unavailable: "Unavailable",
  };

  function setMessage(text, kind) {
    byId("companion-message").textContent = text;
    byId("companion-message").className = kind ? `message ${kind}` : "message";
  }

  function clearPrivate() {
    clearTimeout(privateExpiryTimer);
    privateExpiryTimer = null;
    byId("companion-private-list").replaceChildren();
    byId("companion-private-details").hidden = true;
    byId("companion-answer").hidden = true;
    byId("companion-answer").textContent = "";
    byId("companion-answer-meta").hidden = true;
    byId("companion-answer-meta").textContent = "";
    byId("companion-question").value = "";
  }

  function teardown(reason) {
    actionGeneration += 1;  // every held look/stop/enrollment reply from before this point is discarded
    lookPending = false;
    contextLive = false;
    clearPrivate();
    if (reason) setMessage(reason, "");
    renderControls();
  }

  function presenceText(presence) {
    const occupancy = String(presence?.occupancy || "unknown");
    const health = String(presence?.source_health || "none");
    if (occupancy === "present") return "Someone is here";
    if (occupancy === "empty") return "Room is empty";
    if (health === "stale" || health === "unavailable") return "Unknown · sensor not reporting";
    if (health === "none") return "Unknown · no presence source";
    return "Unknown";
  }

  function mediaText(media) {
    const permitted = media?.permitted === true ? "permitted" : "not permitted";
    const inUse = media?.in_use === true ? "in use now" : "not in use";
    return `${inUse} · ${permitted}`;
  }

  function renderControls() {
    const usable = ownerActive && !lookPending;
    byId("companion-look").disabled = !usable || !lookAvailable;
    byId("companion-follow-up").disabled = !usable || !contextLive;
    byId("companion-question").disabled = !ownerActive || lookPending;
    byId("companion-suggestions-level").disabled = !ownerActive;
    byId("companion-enroll-save").disabled = !ownerActive || enrollPending || !catalog?.available;
    byId("companion-enroll-clear").disabled = !ownerActive || enrollPending
      || !(catalog?.available || catalog?.removal_available);
  }

  function update(payload) {
    if (window.HomebodyOwner?.isOwner() !== true) { owner(false); return; }
    ownerActive = true;
    const runtime = payload?.runtime || {};
    const companion = runtime.companion;
    if (!companion || typeof companion !== "object") {
      offline("Companion state not reported");
      return;
    }
    // Lifecycle transitions observed through status: each one is a real teardown of private state.
    const profile = String(runtime.agent?.profile || "");
    const privateMode = ["meeting", "sleep"].includes(String(runtime.power_mode || ""));
    const kids = Boolean(runtime.kids_mode?.active || runtime.kids_mode?.locked);
    const cameraPermission = companion.camera?.permitted === true;
    const runtimeGeneration = Number(companion.look?.generation || 0);
    if ((lastProfile !== null && profile !== lastProfile) || (lastPrivate === false && privateMode)
        || (lastKids === false && kids) || (lastCameraPermission === true && !cameraPermission)
        || (lastRuntimeGeneration !== null && runtimeGeneration !== lastRuntimeGeneration)) {
      teardown(privateMode ? "Reachy is private; the last look was discarded." : "The last look was discarded.");
    }
    lastProfile = profile; lastPrivate = privateMode; lastKids = kids;
    lastCameraPermission = cameraPermission; lastRuntimeGeneration = runtimeGeneration;
    const activity = String(companion.activity || "unavailable");
    byId("companion-activity").textContent = ACTIVITY[activity] || "Unavailable";
    byId("companion-activity").dataset.status = ["looking", "listening", "replying"].includes(activity) ? "completed" : "idle";
    byId("companion-presence").textContent = presenceText(companion.presence);
    byId("companion-camera").textContent = mediaText(companion.camera);
    byId("companion-microphone").textContent = mediaText(companion.microphone);
    const look = companion.look || {};
    lookAvailable = look.available === true && cameraPermission && !privateMode && !kids;
    contextLive = look.context_live === true && cameraPermission && !privateMode && !kids;
    if (!contextLive && !lookPending && !byId("companion-answer").hidden && look.context_live === false) {
      // The runtime dropped its context (Stop, TTL, privacy): the page must not keep the private text.
      clearPrivate();
    }
    const reason = String(look.blocked_reason || "");
    byId("companion-look-reason").textContent = reason
      ? `Look unavailable: ${reason}`
      : contextLive
        ? `Follow-up open for ${Number(look.context_seconds_remaining || 0)}s (${Number(look.follow_ups_remaining || 0)} left); a new Look takes one more picture.`
        : "One explicit look takes one picture; no background sampling or upload.";
    const suggestions = companion.suggestions || {};
    if (!editingLevel) byId("companion-suggestions-level").value = String(suggestions.level || "off");
    byId("companion-why").textContent = `Why: ${suggestions.paused_reason || "suggestions are available"}`;
    const receipts = Array.isArray(companion.receipts) ? companion.receipts : [];
    const list = byId("companion-receipts");
    list.replaceChildren(...(receipts.length ? receipts.slice(-8).reverse().map((item) => {
      const li = document.createElement("li");
      li.textContent = `${String(item.event || "")} · ${String(item.detail || "")}`;
      return li;
    }) : [Object.assign(document.createElement("li"), { textContent: "No companion activity yet" })]));
    renderControls();
    if (!privateMode && !kids && card.offsetParent !== null && performance.now() - privateReadAt > 4000) {
      loadPrivateDetails();
    }
  }

  async function loadPrivateDetails() {
    if (!ownerActive || privateReadPending) return;
    const generation = actionGeneration;
    privateReadPending = true;
    privateReadAt = performance.now();
    try {
      const response = await fetch("/api/companion/status", { cache: "no-store" });
      const body = await response.json();
      if (!current(generation)) return;
      if (!response.ok) throw new Error("Private details unavailable");
      const details = (Array.isArray(body.private_details) ? body.private_details : [])
        .filter((item) => Number(item.seconds_remaining) > 0).slice(0, 4);
      byId("companion-private-list").replaceChildren(...details.map((item) => {
        const li = document.createElement("li");
        li.textContent = String(item.detail || "");
        return li;
      }));
      byId("companion-private-details").hidden = details.length === 0;
      clearTimeout(privateExpiryTimer);
      if (details.length) privateExpiryTimer = setTimeout(() => {
        byId("companion-private-list").replaceChildren();
        byId("companion-private-details").hidden = true;
      }, Math.min(...details.map((item) => Number(item.seconds_remaining))) * 1000);
    } catch (_) {
      if (!current(generation)) return;
      byId("companion-private-list").replaceChildren();
      byId("companion-private-details").hidden = true;
    } finally {
      privateReadPending = false;
    }
  }

  function offline(detail) {
    // Reachy unreachable: nothing is claimed, controls are off and private text does not linger.
    teardown("");
    lookAvailable = false;
    byId("companion-activity").textContent = "Unavailable";
    byId("companion-activity").dataset.status = "idle";
    byId("companion-presence").textContent = "Unknown · Reachy unreachable";
    byId("companion-camera").textContent = "Unknown";
    byId("companion-microphone").textContent = "Unknown";
    byId("companion-look-reason").textContent = detail || "Look unavailable while live status is unknown.";
    renderControls();
  }

  function owner(isOwner) {
    if (isOwner) { ownerActive = true; renderControls(); return; }
    ownerActive = false;
    lookAvailable = false;
    catalog = null;
    teardown("");
    renderCatalog();
    for (const id of ["companion-device-value", "companion-device-offer", "companion-device-accepted"]) {
      byId(id).value = "";
    }
    for (const id of ["companion-calendar", "companion-device"]) {
      byId(id).replaceChildren(Object.assign(document.createElement("option"), { value: "", textContent: "None" }));
    }
    byId("companion-enroll-message").textContent = "";
    byId("companion-activity").textContent = "Unavailable";
    byId("companion-look-reason").textContent = "Pair an owner device to use the Companion card.";
    renderControls();
  }

  function current(generation) {
    return generation === actionGeneration && ownerActive && window.HomebodyOwner?.isOwner() === true;
  }

  async function look(followUp) {
    if (lookPending || !ownerActive) return;
    const question = byId("companion-question").value.trim();
    if (!question) { setMessage("Type what you want Reachy to look at or read.", "error"); return; }
    lookPending = true;
    renderControls();
    const generation = actionGeneration;
    byId("companion-answer").hidden = true;
    byId("companion-answer-meta").hidden = true;
    setMessage(followUp ? "Asking about the last look…" : "Reachy is taking one look…");
    try {
      const response = await fetch(followUp ? "/api/companion/look/follow-up" : "/api/companion/look", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }),
      });
      const body = await response.json();
      // Admitted under an older generation (owner loss, Stop, privacy, offline): never render.
      if (!current(generation)) return;
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
      byId("companion-answer").textContent = String(body.answer || "");
      byId("companion-answer").hidden = false;
      const captures = Number(body.captures || 0);
      byId("companion-answer-meta").textContent =
        `${captures} capture${captures === 1 ? "" : "s"} · ${String(body.processing || (followUp ? "text follow-up" : "unknown"))}`;
      byId("companion-answer-meta").hidden = false;
      setMessage("", "");
    } catch (error) {
      if (!current(generation)) return;
      setMessage(`Look failed: ${error.message}`, "error");
    } finally {
      if (generation === actionGeneration) {
        lookPending = false;
        renderControls();
        if (ownerActive && typeof refreshStatus === "function") refreshStatus();
      }
    }
  }

  async function stop() {
    if (!ownerActive) return;
    // Stop is itself a teardown: pending look replies are discarded and private text leaves the page.
    teardown("");
    const generation = actionGeneration;
    try {
      const response = await fetch("/api/companion/stop", { method: "POST" });
      const body = await response.json();
      if (!current(generation)) return;
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
      setMessage(`Stopped: ${(body.stopped || []).join(", ") || "nothing active"}. Project work is untouched.`, "ok");
    } catch (error) {
      if (!current(generation)) return;
      setMessage(`Stop failed: ${error.message}`, "error");
    } finally {
      if (current(generation) && typeof refreshStatus === "function") refreshStatus();
    }
  }

  async function setLevel() {
    if (!ownerActive) return;
    const level = byId("companion-suggestions-level").value;
    const settings = LEVEL_SETTINGS[level];
    if (!settings) return;
    const generation = actionGeneration;
    try {
      const response = await fetch("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(settings),
      });
      const body = await response.json();
      if (!current(generation)) return;
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
      setMessage(level === "off"
        ? "Unsolicited suggestions are off. Conversation, looking and requested reminders still work."
        : "Suggestions enabled for enrolled sources only.", "ok");
    } catch (error) {
      if (!current(generation)) return;
      setMessage(`Could not change suggestions: ${error.message}`, "error");
    } finally {
      editingLevel = false;
      if (current(generation) && typeof refreshStatus === "function") refreshStatus();
    }
  }

  // --- source enrollment (host-owned catalog; exact already-authorized identifiers only) -----------
  function fillSelect(select, items, valueKey) {
    const keep = select.value;
    select.replaceChildren(Object.assign(document.createElement("option"), { value: "", textContent: "None" }));
    for (const item of items) {
      const option = document.createElement("option");
      option.value = String(item[valueKey] || "");
      option.textContent = `${String(item.label || option.value)} (${option.value})`;
      select.appendChild(option);
    }
    select.value = [...select.options].some((o) => o.value === keep) ? keep : "";
  }

  function renderCatalog() {
    const status = byId("companion-sources-status");
    const needs = byId("companion-setup-needed");
    const form = byId("companion-enroll");
    if (!catalog) {
      status.textContent = "Source catalog not loaded yet.";
      needs.replaceChildren();
      form.hidden = true;
      renderControls();
      return;
    }
    const setup = Array.isArray(catalog.setup_needed) ? catalog.setup_needed : [];
    needs.replaceChildren(...setup.map((text) => Object.assign(document.createElement("li"), { textContent: String(text) })));
    const enrollment = catalog.enrollment || {};
    const enrolled = [...(enrollment.calendar || []).map((c) => `calendar: ${c.label} (${c.calendar_entity_id}, ${c.disclosure})`),
                      ...(enrollment.home_assistant || []).map((h) => `Home Assistant: ${h.label} (${h.entity_id} = ${h.value})`)];
    status.textContent = catalog.available === true
      ? (enrolled.length ? `Enrolled: ${enrolled.join("; ")}.` : "Nothing enrolled. Suggestions stay silent until you enroll a source.")
      : "Setup needed on the Hermes host before sources can be enrolled.";
    if (catalog.enrollment_error) status.textContent += ` Host enrollment file problem: ${catalog.enrollment_error}`;
    form.hidden = !(catalog.available === true || catalog.removal_available === true);
    fillSelect(byId("companion-calendar"), catalog.calendars || [], "calendar_entity_id");
    fillSelect(byId("companion-device"), catalog.devices || [], "entity_id");
    const cal = (enrollment.calendar || [])[0];
    if (cal) {
      byId("companion-calendar").value = cal.calendar_entity_id;
      byId("companion-calendar-lead").value = String(cal.lead_minutes);
      byId("companion-calendar-disclosure").value = cal.disclosure;
    }
    const dev = (enrollment.home_assistant || [])[0];
    if (dev) {
      byId("companion-device").value = dev.entity_id;
      byId("companion-device-value").value = dev.value;
      byId("companion-device-offer").value = dev.offer_text || "";
      byId("companion-device-accepted").value = dev.accepted_text || "";
    }
    renderControls();
  }

  async function loadCatalog() {
    if (!ownerActive) return;
    const generation = actionGeneration;
    try {
      const response = await fetch("/api/companion/sources", { cache: "no-store" });
      const body = await response.json();
      if (!current(generation)) return;
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
      catalog = body;
    } catch (error) {
      if (!current(generation)) return;
      catalog = { available: false, setup_needed: [`Catalog unavailable: ${error.message}`], enrollment: {} };
    }
    renderCatalog();
  }

  function slug(value) {
    return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32) || "source";
  }

  async function saveEnrollment(clear) {
    if (!ownerActive || enrollPending || !(catalog?.available || (clear && catalog?.removal_available))) return;
    const generation = ++actionGeneration;
    clearPrivate();
    const body = { home_assistant: [], calendar: [] };
    if (!clear) {
      const calendarId = byId("companion-calendar").value;
      if (calendarId) {
        const item = (catalog.calendars || []).find((c) => c.calendar_entity_id === calendarId);
        body.calendar.push({ id: slug(item?.label || calendarId), label: String(item?.label || calendarId).slice(0, 48),
          calendar_entity_id: calendarId, lead_minutes: Number(byId("companion-calendar-lead").value || 10),
          disclosure: byId("companion-calendar-disclosure").value });
      }
      const entityId = byId("companion-device").value;
      if (entityId) {
        const item = (catalog.devices || []).find((d) => d.entity_id === entityId);
        body.home_assistant.push({ id: slug(item?.label || entityId), label: String(item?.label || entityId).slice(0, 48),
          entity_id: entityId, condition: "state_equals", value: byId("companion-device-value").value.trim() || "on",
          offer_text: byId("companion-device-offer").value.trim(),
          accepted_text: byId("companion-device-accepted").value.trim(), min_interval_seconds: 3600 });
      }
    }
    enrollPending = true;
    renderControls();
    const message = byId("companion-enroll-message");
    message.textContent = clear ? "Removing sources…" : "Saving enrollment on the Hermes host…";
    message.className = "message";
    try {
      const response = await fetch("/api/companion/sources", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      const result = await response.json();
      if (!current(generation)) return;
      if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
      catalog = result;
      renderCatalog();
      message.textContent = clear ? "All sources removed; suggestions are silent." : "Enrollment saved on the host.";
      message.className = "message ok";
    } catch (error) {
      if (!current(generation)) return;
      message.textContent = `Enrollment failed: ${error.message}`;
      message.className = "message error";
    } finally {
      if (generation === actionGeneration) { enrollPending = false; renderControls(); }
    }
  }

  byId("companion-look").addEventListener("click", () => look(false));
  byId("companion-follow-up").addEventListener("click", () => look(true));
  byId("companion-stop").addEventListener("click", stop);
  byId("companion-suggestions-level").addEventListener("focus", () => { editingLevel = true; });
  byId("companion-suggestions-level").addEventListener("blur", () => { editingLevel = false; });
  byId("companion-suggestions-level").addEventListener("change", setLevel);
  byId("companion-question").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); look(false); }
  });
  byId("companion-details").addEventListener("toggle", () => { if (byId("companion-details").open) loadCatalog(); });
  byId("companion-enroll-save").addEventListener("click", () => saveEnrollment(false));
  byId("companion-enroll-clear").addEventListener("click", () => saveEnrollment(true));

  window.HomebodyCompanion = { update, offline, owner, loadCatalog, loadPrivateDetails };
})();
