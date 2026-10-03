const $ = (id) => document.getElementById(id);
const fields = [
  "bridge_url", "api_key", "model", "conversation_mode", "language", "stt_provider", "stt_model",
  "tts_provider", "tts_model", "tts_voice", "continuous_conversation",
  "motion_enabled", "barge_in_enabled", "camera_enabled", "camera_feed_enabled", "camera_controls_enabled", "camera_controls_handedness", "face_tracking_enabled", "face_tracking_weight",
  "doa_enabled", "robot_tools_enabled", "home_assistant_enabled", "home_assistant_controls_enabled",
  "home_assistant_camera_enabled", "home_assistant_assist_enabled", "home_assistant_port",
  "realtime_model", "realtime_voice", "realtime_reasoning_effort",
  "local_vision_enabled", "local_vision_url", "local_vision_model", "local_ai_accelerator",
  "mcp_enabled", "mcp_vision_enabled",
  "end_silence_seconds", "max_utterance_seconds", "vad_min_rms", "vad_noise_multiplier",
  "wake_keyword_threshold", "wake_keyword_score",
];
let loaded = false;
let currentConfig = null;
let voiceOptions = { stt: [], tts: [] };
let manualActionPending = false;
let poseRefreshPending = false;
let powerTransitionPending = false;
let statusRefreshPending = false;
let statusRefreshHealthy = false;
let agentActivityPending = false;
let currentPowerMode = "unknown";
let lastMotorAnnouncement = "";
let deferredInstallPrompt = null;
let announcementRequestPending = false;
let lastAnnouncementLiveText = "";
let kidsRequestPending = false;
let selectedKidsActivity = "buddy";
let bluetoothRefreshPending = false;
let bluetoothState = null;
let agentRequestPending = false;
let agentProfileActive = false;
let runtimeAgentActivity = [];
let brokerAgentActivity = [];
let pendingAgentApproval = null;
let agentRunRequestPending = false;
let presenceRequestPending = false;
let initiativeRequestPending = false;
let initiativeEditActive = false;
let currentContextualOfferToken = 0;
let presentationRequestPending = false;
let presentationEditActive = false;
let currentAgentRun = null;

// Carry browser settings saved before the Homebody rename over once, then drop the old keys.
[
  ["localStorage", "reachy-hermes-tab", "homebody-tab"],
  ["localStorage", "reachy-hermes-kids-profile", "homebody-kids-profile"],
  ["sessionStorage", "reachy-hermes-agent-run-id", "homebody-agent-run-id"],
  ["sessionStorage", "reachy-hermes-announcement-draft", "homebody-announcement-draft"],
].forEach(([area, legacyKey, key]) => {
  try {
    const storage = window[area];
    const legacy = storage.getItem(legacyKey);
    if (legacy !== null && storage.getItem(key) === null) storage.setItem(key, legacy);
    storage.removeItem(legacyKey);
  } catch (error) {
    // Storage can be unavailable (private mode, blocked site data); nothing to migrate then.
  }
});
let agentRunId = window.sessionStorage.getItem("homebody-agent-run-id") || "";

function renderAgentRun() {
  const run = currentAgentRun;
  const badge = $("agent-run-badge");
  const steps = $("agent-run-steps");
  steps.replaceChildren();
  if (!run) {
    badge.textContent = "No plan";
    badge.dataset.status = "idle";
    $("agent-run-budget").textContent = "No active run.";
    steps.appendChild(Object.assign(document.createElement("li"), {
      textContent: "Enter a goal to preview every exact step before anything runs.",
    }));
  } else {
    const status = String(run.status || "unknown");
    badge.textContent = status.replaceAll("_", " ");
    badge.dataset.status = status;
    const budgets = run.budgets || {};
    $("agent-run-budget").textContent = `${String(run.summary || "Run ready")} · ${Number(run.tool_calls_used || 0)}/${Number(budgets.max_tool_calls || 5)} tool calls · ${Number(run.side_effects_used || 0)}/${Number(budgets.max_side_effects || 2)} side effects · ${Number(budgets.max_seconds || 120)}s limit`;
    (run.steps || []).forEach((step) => {
      const row = document.createElement("li");
      row.dataset.status = String(step.status || "queued");
      const heading = document.createElement("strong");
      heading.textContent = String(step.title || step.capability_id || "Plan step");
      const meta = document.createElement("span");
      meta.className = "agent-step-meta";
      const labels = [String(step.status || "queued").replaceAll("_", " ")];
      if (step.requires_approval) labels.push("phone approval");
      if (step.verified) labels.push("verified");
      meta.textContent = labels.join(" · ");
      row.append(heading, meta);
      const exactArguments = JSON.stringify(step.arguments || {});
      if (exactArguments !== "{}") {
        const args = document.createElement("code");
        args.textContent = exactArguments;
        row.appendChild(args);
      }
      if ((step.evidence_sources || []).length) {
        const evidence = document.createElement("small");
        evidence.textContent = `Evidence: ${step.evidence_sources.join(", ")}`;
        row.appendChild(evidence);
      }
      steps.appendChild(row);
    });
  }
  const status = run?.status || "";
  const activeStep = (run?.steps || []).find((step) => step.step_id === run.active_step_id);
  $("agent-run-preview-button").disabled = !agentProfileActive || agentRunRequestPending || (run && !["completed", "partial", "failed", "cancelled"].includes(status));
  $("agent-run-start-button").disabled = agentRunRequestPending || status !== "preview";
  $("agent-run-pause-button").disabled = agentRunRequestPending || !["running", "waiting_approval"].includes(status);
  $("agent-run-resume-button").disabled = agentRunRequestPending || status !== "paused" || run?.resumable !== true;
  $("agent-run-cancel-button").disabled = agentRunRequestPending || !run || ["completed", "partial", "failed", "cancelled"].includes(status);
  $("agent-run-approve-button").hidden = status !== "waiting_approval" || activeStep?.status !== "waiting_approval";
  $("agent-run-approve-button").textContent = activeStep
    ? `Approve step ${String(activeStep.step_id).replace("step-", "")} exactly`
    : "Approve waiting step exactly";
}

function renderAgentActivity() {
  const activity = $("agent-activity");
  activity.replaceChildren();
  const combined = [...runtimeAgentActivity, ...brokerAgentActivity]
    .sort((left, right) => Number(left.timestamp || 0) - Number(right.timestamp || 0))
    .slice(-20);
  (combined.length ? combined : [{ event: "No activity yet" }]).forEach((item) => {
    const row = document.createElement("li");
    const capability = item.capability_id ? `${String(item.capability_id).replaceAll("_", " ")} · ` : "";
    const result = item.result_class && item.result_class !== "running" ? ` · ${item.result_class}` : "";
    row.textContent = `${capability}${String(item.event || "activity").replaceAll("_", " ")}${result}`;
    activity.appendChild(row);
  });
}

const kidsActivityLabels = {
  buddy: "Buddy chat",
  story: "Story maker",
  quiz: "Quiz quest",
  riddles: "Riddle box",
  calm: "Calm corner",
  ispy: "I Spy",
};

const announcementText = $("announcement-text");
announcementText.value = window.sessionStorage.getItem("homebody-announcement-draft") || "";
$("announcement-count").textContent = `${announcementText.value.length.toLocaleString()} / 15,000`;

document.querySelectorAll(".manual-control, [data-power]").forEach((button) => { button.disabled = true; });
$("emotion-select").disabled = true;
$("announcement-send").disabled = true;
$("announcement-stop").disabled = true;
$("kids-start-button").disabled = true;
$("kids-stop-button").disabled = true;
$("agent-conversation-button").disabled = true;
$("agent-enable-button").disabled = true;
$("agent-stop-button").disabled = true;
$("agent-approve-button").disabled = true;
[
  "agent-run-preview-button", "agent-run-start-button", "agent-run-pause-button",
  "agent-run-resume-button", "agent-run-cancel-button", "agent-run-approve-button",
].forEach((id) => { $(id).disabled = true; });
renderAgentRun();
[
  "bluetooth-scan-button", "bluetooth-pair-button", "bluetooth-connect-button",
  "bluetooth-disconnect-button", "bluetooth-remove-button", "gamepad-enabled",
].forEach((id) => { $(id).disabled = true; });
const GPIO_CONTROL_IDS = ["gpio-enabled", "gpio-green-pin", "gpio-red-pin", "gpio-long-press", "gpio-save-button"];
GPIO_CONTROL_IDS.forEach((id) => { $(id).disabled = true; });
let gpioRefreshPending = false;
let gpioFormDirty = false;

try {
  const savedKidsProfile = JSON.parse(window.localStorage.getItem("homebody-kids-profile") || "{}");
  if (kidsActivityLabels[savedKidsProfile.activity]) selectedKidsActivity = savedKidsProfile.activity;
  if (typeof savedKidsProfile.nickname === "string") $("kids-nickname").value = savedKidsProfile.nickname.slice(0, 32);
  if (["4-6", "7-9", "10-12"].includes(savedKidsProfile.age_band)) $("kids-age-band").value = savedKidsProfile.age_band;
  if ([15, 30, 45, 60].includes(Number(savedKidsProfile.duration_minutes))) $("kids-duration").value = String(savedKidsProfile.duration_minutes);
  if (["en", "nl"].includes(savedKidsProfile.language)) $("kids-language").value = savedKidsProfile.language;
  if (typeof savedKidsProfile.motion_enabled === "boolean") $("kids-motion-enabled").checked = savedKidsProfile.motion_enabled;
} catch (error) {
  window.localStorage.removeItem("homebody-kids-profile");
}
$("local-vision-test-button").addEventListener("click", async () => {
  const button = $("local-vision-test-button");
  const message = $("local-vision-message");
  button.disabled = true;
  message.textContent = "Contacting the vision server…";
  message.className = "message";
  try {
    const response = await fetchWithTimeout("/api/vision/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        local_vision_url: $("local_vision_url").value.trim() || null,
        local_vision_model: $("local_vision_model").value.trim() || null,
        current_api_key: $("current_api_key").value.trim() || null,
      }),
    }, 15000);
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    message.textContent = body.model_listed
      ? `Connected. ${body.model} is available.`
      : `Connected, but ${body.model} is not listed. Available: ${(body.models || []).join(", ") || "none"}.`;
    message.className = body.model_listed ? "message ok" : "message error";
  } catch (error) {
    message.textContent = String(error.message || error);
    message.className = "message error";
  } finally {
    button.disabled = false;
  }
});

$("mcp-endpoint").value = `${window.location.origin}/mcp`;

async function refreshMcpStatus() {
  try {
    const response = await fetchWithTimeout("/api/mcp/status", { cache: "no-store" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    const message = $("mcp-message");
    if (message.classList.contains("ok") || message.classList.contains("error")) return;
    const state = !body.enabled ? "Agent access is off." : body.token_configured ? "Agent access is on." : "Agent access is on, but no token exists yet.";
    const usage = body.calls ? ` ${body.calls} request(s) so far; last: ${body.last_tool} (${body.last_result}).` : "";
    message.textContent = state + usage;
  } catch (error) {
    // Status is informational; the settings form reports real errors.
  }
}

async function mcpTokenAction(path, pendingText) {
  const message = $("mcp-message");
  message.textContent = pendingText;
  message.className = "message";
  ["mcp-token-button", "mcp-revoke-button"].forEach((id) => { $(id).disabled = true; });
  try {
    const response = await fetchWithTimeout(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_api_key: $("current_api_key").value.trim() }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    return body;
  } catch (error) {
    message.textContent = String(error.message || error);
    message.className = "message error";
    return null;
  } finally {
    ["mcp-token-button", "mcp-revoke-button"].forEach((id) => { $(id).disabled = false; });
  }
}

$("mcp-token-button").addEventListener("click", async () => {
  if (!window.confirm("Create a new agent token? Agents using the old token will lose access.")) return;
  const body = await mcpTokenAction("/api/mcp/token", "Creating a token…");
  if (!body) return;
  $("mcp-token").value = body.token;
  $("mcp-token-row").hidden = false;
  $("mcp-token").select();
  $("mcp-message").textContent = "Token created. Copy it now; it will not be shown again.";
  $("mcp-message").className = "message ok";
});

$("mcp-revoke-button").addEventListener("click", async () => {
  if (!window.confirm("Revoke the agent token? Every connected agent loses access.")) return;
  const body = await mcpTokenAction("/api/mcp/token/revoke", "Revoking…");
  if (!body) return;
  $("mcp-token").value = "";
  $("mcp-token-row").hidden = true;
  $("mcp-message").textContent = "Token revoked. No agent can reach Reachy until you create a new one.";
  $("mcp-message").className = "message ok";
});

refreshMcpStatus();

$("local-vision-ask").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("local-vision-ask-button");
  const answer = $("local-vision-answer");
  const question = $("local-vision-question").value.trim() || "What do you see?";
  button.disabled = true;
  answer.textContent = "Looking…";
  answer.classList.remove("error");
  try {
    // The key stays in this field only; it is never stored by the page.
    const key = $("local-vision-key").value.trim();
    const response = await fetchWithTimeout("/api/vision/describe", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` },
      body: JSON.stringify({ question }),
    }, 90000);
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    answer.textContent = `${body.answer} (${body.model}, ${(body.latency_ms / 1000).toFixed(1)} s, on your hardware)`;
  } catch (error) {
    answer.textContent = String(error.message || error);
    answer.classList.add("error");
  } finally {
    button.disabled = false;
  }
});

document.querySelectorAll("[data-kids-activity]").forEach((button) => {
  const selected = button.dataset.kidsActivity === selectedKidsActivity;
  button.classList.toggle("selected", selected);
  button.setAttribute("aria-checked", String(selected));
});
$("kids-activity-badge").textContent = kidsActivityLabels[selectedKidsActivity];
$("kids-ispy-consent-row").hidden = selectedKidsActivity !== "ispy";

function visibleTabButtons() {
  return [...document.querySelectorAll("[data-tab]")].filter((button) => !button.hidden);
}

function activateTab(name, focus = false, recordHistory = false) {
  // A Kids-locked UI hides every tab except Kids; arrows, history and #hash must not reveal them.
  const requested = document.querySelector(`[data-tab="${name}"]`);
  const target = requested && !requested.hidden ? requested : visibleTabButtons()[0];
  if (!target) return;
  document.querySelectorAll("[data-tab]").forEach((button) => {
    const active = button === target;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  if (target.dataset.tab !== "robot" && window.ReachyCamera?.isActive()) {
    window.ReachyCamera.stop("Camera stopped when leaving the Robot tab.");
  }
  document.querySelectorAll("[data-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.panel !== target.dataset.tab;
  });
  window.localStorage.setItem("homebody-tab", target.dataset.tab);
  if (recordHistory && window.location.hash !== `#${target.dataset.tab}`) {
    window.history.pushState(null, "", `#${target.dataset.tab}`);
  }
  if (target.dataset.tab === "robot") {
    refreshRobotPose();
    refreshBluetooth();
    refreshGpio();
  }
  if (focus) target.focus();
}

const tabButtons = [...document.querySelectorAll("[data-tab]")];
tabButtons.forEach((button, index) => {
  button.addEventListener("click", () => activateTab(button.dataset.tab, false, true));
  button.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const visible = visibleTabButtons();
    const position = Math.max(0, visible.indexOf(button));
    let next = position;
    if (event.key === "ArrowLeft") next = (position - 1 + visible.length) % visible.length;
    if (event.key === "ArrowRight") next = (position + 1) % visible.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = visible.length - 1;
    activateTab(visible[next].dataset.tab, true, true);
  });
});
const initialTab = window.location.hash.slice(1) || window.localStorage.getItem("homebody-tab") || "dashboard";
activateTab(initialTab);
window.addEventListener("popstate", () => activateTab(window.location.hash.slice(1) || "dashboard"));

function runningStandalone() {
  return window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
}

function updateInstallUi() {
  const button = $("install-button");
  const status = $("install-status");
  const message = $("install-message");
  const help = $("install-help");
  if (runningStandalone()) {
    button.hidden = true;
    status.textContent = "Installed";
    message.textContent = "Running as the Homebody home-screen app.";
    message.className = "message ok";
    help.hidden = true;
    return;
  }
  help.hidden = false;
  if (deferredInstallPrompt) {
    button.hidden = false;
    status.textContent = "Ready";
    message.textContent = "Chrome is ready to install the standalone app.";
    message.className = "message ok";
    return;
  }
  button.hidden = true;
  status.textContent = window.isSecureContext ? "Web app" : "Shortcut";
  message.textContent = window.isSecureContext
    ? "Chrome will enable Install app when its PWA checks are complete."
    : "This LAN address uses HTTP. Use Chrome's ⋮ menu and Add to Home screen, or open the dashboard through trusted HTTPS for full app installation.";
  message.className = "message";
}

window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  deferredInstallPrompt = event;
  updateInstallUi();
});

window.addEventListener("appinstalled", () => {
  deferredInstallPrompt = null;
  updateInstallUi();
});

$("install-button").addEventListener("click", async () => {
  if (!deferredInstallPrompt) return;
  const prompt = deferredInstallPrompt;
  deferredInstallPrompt = null;
  await prompt.prompt();
  const choice = await prompt.userChoice;
  if (choice.outcome === "accepted") {
    $("install-button").hidden = true;
    $("install-status").textContent = "Installing";
    $("install-help").hidden = true;
    $("install-message").textContent = "Installation accepted. Homebody is being added to your home screen.";
    $("install-message").className = "message ok";
  } else {
    updateInstallUi();
    $("install-message").textContent = "Installation dismissed. You can try again from Chrome's menu.";
  }
});

async function registerPwa() {
  updateInstallUi();
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  try {
    await navigator.serviceWorker.register("/service-worker.js", { scope: "/" });
  } catch (error) {
    $("install-message").textContent = `App installation support could not start: ${String(error)}`;
    $("install-message").className = "message error";
  }
}
registerPwa();

function setMessage(text, kind = "") {
  const el = $("form-message");
  el.textContent = text;
  el.className = `message ${kind}`;
}

function fillConfig(config) {
  if (loaded || !config) return;
  fields.forEach((name) => {
    const el = $(name);
    if (!el || !(name in config)) return;
    if (el.type === "checkbox") el.checked = Boolean(config[name]);
    else el.value = config[name] ?? "";
  });
  loaded = true;
  toggleModePanels();
  toggleHomeAssistantOptions(false);
}

function toggleHomeAssistantOptions(clearWhenDisabled = true) {
  const enabled = $("home_assistant_enabled").checked;
  ["home_assistant_controls_enabled", "home_assistant_camera_enabled", "home_assistant_assist_enabled", "home_assistant_port"].forEach((name) => {
    const element = $(name);
    element.disabled = !enabled;
    if (!enabled && clearWhenDisabled && element.type === "checkbox") element.checked = false;
  });
}

function toggleModePanels() {
  const realtime = $("conversation_mode").value === "realtime";
  $("realtime-settings").hidden = !realtime;
  $("voice-provider").textContent = realtime
    ? "OpenAI Realtime streams speech in both directions; pipeline STT/TTS selectors are ignored."
    : "Wake detection stays local. Selected STT and TTS run through the authenticated Hermes-host bridge.";
}

$("conversation_mode").addEventListener("change", toggleModePanels);
$("home_assistant_enabled").addEventListener("change", () => toggleHomeAssistantOptions(true));

let hostShutdownLabel = "robot computer";
const ACCELERATOR_NAMES = {
  TensorrtExecutionProvider: "TensorRT",
  CUDAExecutionProvider: "CUDA",
  CoreMLExecutionProvider: "Core ML",
  DmlExecutionProvider: "DirectML",
};

function renderHost(hostInfo) {
  if (!hostInfo || typeof hostInfo !== "object") return;
  const acceleration = (hostInfo.onnx_providers || []).find((name) => ACCELERATOR_NAMES[name]);
  const label = hostInfo.label || "Computer";
  const model = String(hostInfo.model || "");
  const name = model && model !== label ? (model.startsWith(label) ? model : `${label} (${model})`) : label;
  $("host-summary").textContent = `${name} · local AI on ${acceleration ? ACCELERATOR_NAMES[acceleration] : "CPU"}`;
  hostShutdownLabel = hostInfo.label || "robot computer";
  $("shutdown-button").hidden = !hostInfo.shutdown_supported;
  $("shutdown-button").textContent = `Shut down ${hostShutdownLabel}`;
  // Only offer hardware cards the host can actually drive.
  document.querySelector(".bluetooth-card").hidden = !hostInfo.bluetooth_controller_supported;
  document.querySelector(".gpio-card").hidden = !hostInfo.gpio_supported;
}

function updateStatus(payload) {
  renderHost(payload.host);
  const config = payload.config || {};
  $("local-vision-ask").hidden = !(config.local_vision_enabled && config.camera_enabled);
  const runtime = payload.runtime || {};
  const state = runtime.state || "unknown";
  $("runtime-state").textContent = state.replaceAll("_", " ");
  $("runtime-detail").textContent = runtime.detail || "";
  $("last-transcript").textContent = runtime.transcript || "—";
  $("last-response").textContent = runtime.response_preview || "—";
  const powerMode = runtime.power_mode || "unknown";
  currentPowerMode = powerMode;
  const homeAssistant = runtime.home_assistant || {};
  const homeAssistantStatus = $("home-assistant-status");
  if (homeAssistantStatus) {
    homeAssistantStatus.textContent = homeAssistant.error
      ? `Bridge error: ${homeAssistant.error}`
      : homeAssistant.connected
        ? `${homeAssistant.device_name || "Reachy"} connected to Home Assistant on port ${homeAssistant.port || 6053}`
        : homeAssistant.ready
          ? `${homeAssistant.device_name || "Reachy"} is available on port ${homeAssistant.port || 6053}; waiting for Home Assistant`
          : homeAssistant.enabled
            ? "Bridge starts after an app restart"
            : "Bridge disabled";
    homeAssistantStatus.className = `muted${homeAssistant.error ? " error" : homeAssistant.connected ? " ok" : ""}`;
  }
  const kidsMode = runtime.kids_mode || {};
  const kidsActive = Boolean(kidsMode.active);
  const kidsCameraActive = Boolean(kidsMode.camera_active);
  const kidsLocked = Boolean(kidsMode.locked);
  const agent = runtime.agent || {};
  const agentProfile = agent.profile || "conversation";
  agentProfileActive = agentProfile === "agent";
  $("agent-profile-badge").textContent = agentProfile;
  $("tab-agent").dataset.profileActive = String(agentProfileActive);
  $("agent-capabilities").textContent = (agent.enabled_capabilities || []).join(", ") || "None in Conversation profile";
  $("agent-current-task").textContent = agent.current_task || "—";
  $("agent-pending-approval").textContent = pendingAgentApproval
    ? `Waiting · expires in ${Number(pendingAgentApproval.expires_in_seconds || 0)}s`
    : agent.pending_approval ? "Waiting for adult approval" : "None";
  const presence = runtime.presence || {};
  const presenceEnabled = Boolean(payload.config?.proactive_presence_enabled);
  const acknowledgementEnabled = Boolean(payload.config?.presence_acknowledgement_enabled);
  $("presence-enabled").checked = presenceEnabled;
  $("presence-acknowledgement-enabled").checked = acknowledgementEnabled;
  $("presence-enabled").disabled = kidsActive || kidsLocked || presenceRequestPending;
  $("presence-acknowledgement-enabled").disabled = !presenceEnabled || kidsActive || kidsLocked || presenceRequestPending;
  const presenceLevel = presenceEnabled ? String(presence.level || "away") : "off";
  $("presence-badge").textContent = presenceLevel;
  $("presence-badge").dataset.status = presenceLevel === "attentive" ? "completed" : "idle";
  $("presence-level").textContent = presenceLevel.replaceAll("_", " ");
  $("presence-source").textContent = presence.source
    ? String(presence.source).replaceAll("_", " ")
    : "None";
  const outcome = presenceEnabled ? String(presence.last_outcome || "none") : "disabled";
  $("presence-outcome").textContent = outcome.replaceAll("_", " ");
  const initiative = runtime.initiative || {};
  const initiativeEnabled = Boolean(payload.config?.initiative_policy_enabled);
  const initiativeMode = String(payload.config?.initiative_mode || "quiet");
  if (!initiativeEditActive) {
    $("initiative-policy-enabled").checked = initiativeEnabled;
    $("initiative-mode").value = initiativeMode;
    $("initiative-quiet-hours-enabled").checked = Boolean(payload.config?.initiative_quiet_hours_enabled);
    $("initiative-quiet-hours-start").value = String(payload.config?.initiative_quiet_hours_start || "22:00");
    $("initiative-quiet-hours-end").value = String(payload.config?.initiative_quiet_hours_end || "07:00");
    $("initiative-hourly-budget").value = String(Number(payload.config?.initiative_hourly_budget || 2));
    $("initiative-daily-budget").value = String(Number(payload.config?.initiative_daily_budget || 6));
    $("contextual-offers-enabled").checked = Boolean(payload.config?.contextual_offers_enabled);
    $("contextual-offer-response-window").value = String(Number(payload.config?.contextual_offer_response_window_seconds || 10));
  }
  const initiativeBlocked = kidsActive || kidsLocked || initiativeRequestPending;
  $("initiative-policy-enabled").disabled = initiativeBlocked;
  ["initiative-mode", "initiative-quiet-hours-enabled", "initiative-quiet-hours-start", "initiative-quiet-hours-end", "initiative-hourly-budget", "initiative-daily-budget", "contextual-offers-enabled", "contextual-offer-response-window"].forEach((id) => {
    $(id).disabled = !initiativeEnabled || initiativeBlocked;
  });
  $("initiative-badge").textContent = initiativeEnabled ? initiativeMode : "off";
  $("initiative-badge").dataset.status = initiativeEnabled ? "completed" : "idle";
  $("initiative-outcome").textContent = String(initiative.latest_outcome || "remain_silent").replaceAll("_", " ");
  const initiativeReason = initiativeEnabled
    ? String(initiative.latest_reason || "none").replaceAll("_", " ")
    : "disabled";
  const initiativeTopic = initiativeEnabled && initiative.latest_topic
    ? String(initiative.latest_topic).replaceAll("_", " ")
    : "";
  $("initiative-reason").textContent = initiativeTopic
    ? `${initiativeReason} · ${initiativeTopic}`
    : initiativeReason;
  $("initiative-explanation").textContent = String(initiative.latest_explanation || "Nothing has happened yet.");
  renderInitiativePreferences(Array.isArray(initiative.preferences) ? initiative.preferences : [], initiativeBlocked);
  $("initiative-budget").textContent = `${Number(initiative.initiatives_today || 0)} / ${Number(initiative.daily_budget || payload.config?.initiative_daily_budget || 0)} today · ${Number(initiative.initiatives_this_hour || 0)} / ${Number(initiative.hourly_budget || payload.config?.initiative_hourly_budget || 0)} this hour`;
  const contextualOffer = runtime.contextual_offer || {};
  const offerState = String(contextualOffer.state || "disabled");
  currentContextualOfferToken = Number(contextualOffer.token || 0);
  $("contextual-offer-state").textContent = offerState.replaceAll("_", " ");
  $("contextual-offer-text").textContent = contextualOffer.topic
    ? `Topic: ${String(contextualOffer.topic).replaceAll("_", " ")}`
    : "No offer yet.";
  $("contextual-offer-explanation").textContent = contextualOffer.explanation
    ? `${contextualOffer.explanation} · ${String(contextualOffer.reason || "")}`
    : "Allowlisted context only.";
  const awaitingOfferResponse = offerState === "awaiting_response" && currentContextualOfferToken > 0;
  $("contextual-offer-response").hidden = !awaitingOfferResponse;
  $("contextual-offer-yes").disabled = !awaitingOfferResponse || initiativeRequestPending;
  $("contextual-offer-no").disabled = !awaitingOfferResponse || initiativeRequestPending;
  $("contextual-offer-later").disabled = !awaitingOfferResponse || initiativeRequestPending;
  const presentation = runtime.shared_physical_context || {};
  const presentationEnabled = Boolean(payload.config?.shared_physical_context_enabled);
  const presentationState = String(presentation.state || "disabled");
  const presentationActive = ["starting", "watching"].includes(presentationState);
  if (!presentationEditActive) {
    $("shared-physical-context-enabled").checked = presentationEnabled;
    $("presentation-window-seconds").value = String(Number(payload.config?.presentation_window_seconds || 20));
  }
  $("presentation-badge").textContent = presentationActive ? "Local camera active" : presentationState;
  $("presentation-badge").dataset.status = presentationActive ? "completed" : "idle";
  $("presentation-state").textContent = presentationState.replaceAll("_", " ");
  $("presentation-remaining").textContent = `${Number(presentation.expires_seconds_remaining || 0)} seconds`;
  $("presentation-reason").textContent = String(presentation.reason || "none").replaceAll("_", " ");
  const presentationBlocked = kidsActive || kidsLocked || presentationRequestPending;
  $("shared-physical-context-enabled").disabled = presentationBlocked;
  $("presentation-window-seconds").disabled = !presentationEnabled || presentationBlocked;
  $("presentation-start").disabled = !presentationEnabled
    || !Boolean(payload.config?.camera_enabled)
    || !initiativeEnabled
    || !Boolean(payload.config?.contextual_offers_enabled)
    || agentProfile !== "agent"
    || powerMode !== "awake"
    || presentationActive
    || presentationBlocked;
  $("presentation-stop").disabled = !presentationActive || presentationRequestPending;
  runtimeAgentActivity = agent.recent_activity || [];
  renderAgentActivity();
  document.querySelector(".agent-card").hidden = kidsActive || kidsLocked;
  const agentBlocked = kidsActive || kidsLocked || ["meeting", "sleep"].includes(powerMode) || agentRequestPending;
  $("agent-conversation-button").disabled = agentBlocked || agentProfile === "conversation";
  $("agent-enable-button").disabled = agentBlocked || agentProfile === "agent";
  $("agent-stop-button").disabled = agentRequestPending;
  renderAgentRun();
  const robotBusy = Boolean(runtime.robot_action_busy);
  const motorsEnabled = runtime.motors_enabled;
  const headSafelyFolded = Boolean(runtime.head_safely_folded);
  const controlsBlocked = ["meeting", "sleep"].includes(powerMode)
    || kidsActive || robotBusy || manualActionPending || powerTransitionPending;
  $("power-mode-badge").textContent = powerMode;
  $("robot-mode-badge").textContent = robotBusy ? "moving" : powerMode;
  const robotActionLabels = {
    move_reachy_head: "Look direction",
    nudge_reachy: "Precision pose",
    express_reachy_emotion: "Expression preset",
    dance_reachy: "Dance preset",
  };
  $("last-robot-action").textContent = robotActionLabels[runtime.last_robot_action]
    || runtime.last_robot_action
    || "—";
  $("robot-action-error").textContent = runtime.robot_action_last_error
    ? ` · ${runtime.robot_action_last_error}`
    : "";
  const motorStateText = motorsEnabled === true
    ? `Torque on · ${headSafelyFolded ? "folded pose" : "active pose"}`
    : motorsEnabled === false
      ? `Torque off · ${headSafelyFolded ? "folded safely" : "pose unconfirmed"}`
      : "Motor state unavailable";
  $("motor-state").textContent = motorStateText;
  const motorDot = $("motor-state-dot");
  motorDot.className = "motor-state-dot";
  if (motorsEnabled === true) motorDot.classList.add("on");
  if (motorsEnabled === false) motorDot.classList.add("off");
  if (runtime.last_error && motorsEnabled !== false) motorDot.classList.add("error");
  document.querySelector(".robot-control-card").setAttribute(
    "aria-busy",
    String(robotBusy || manualActionPending || powerTransitionPending),
  );
  document.querySelector(".robot-control-card").dataset.actionBusy = String(
    robotBusy || manualActionPending,
  );
  document.querySelectorAll(".manual-control").forEach((button) => {
    button.disabled = controlsBlocked;
  });
  $("emotion-select").disabled = controlsBlocked;
  $("robot-stop-button").disabled = powerTransitionPending;
  const readinessText = powerTransitionPending
    ? "Changing motor power — manual presets are paused"
    : robotBusy
      ? "Moving — press Stop action to cancel active and queued movement"
      : powerMode === "standby"
        ? "Standby — a movement command wakes Reachy first"
        : powerMode === "awake"
          ? "Awake — bounded remote movement is enabled"
          : `${powerMode} — manual movement unavailable`;
  $("robot-readiness").textContent = readinessText;
  const motorAnnouncement = `${motorStateText}. ${readinessText}`;
  if (motorAnnouncement !== lastMotorAnnouncement) {
    $("motor-state-live").textContent = motorAnnouncement;
    lastMotorAnnouncement = motorAnnouncement;
  }
  document.querySelectorAll("[data-power]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.power === powerMode));
    button.disabled = kidsActive || powerTransitionPending || manualActionPending;
  });
  if (!robotBusy) refreshRobotPose();
  const dot = $("status-dot");
  dot.className = "status-dot";
  if (["waiting_for_wake_word", "listening", "looking", "thinking", "speaking"].includes(state)) dot.classList.add("ready");
  if (["error", "configuration_error"].includes(state)) dot.classList.add("error");
  fillConfig(payload.config);
  currentConfig = payload.config || currentConfig;
  const kidsProfile = kidsMode.profile || {};
  const remainingSeconds = Math.max(0, Number(kidsMode.remaining_seconds || 0));
  const totalSeconds = Math.max(1, Number(kidsProfile.duration_minutes || 1) * 60);
  const remainingPercent = kidsActive ? Math.max(0, Math.min(100, (remainingSeconds / totalSeconds) * 100)) : 0;
  const remainingMinutes = Math.floor(remainingSeconds / 60);
  const remainingRemainder = remainingSeconds % 60;
  $("kids-status-badge").textContent = kidsCameraActive ? "Camera search" : kidsActive ? "Active" : "Off";
  $("kids-status-badge").classList.toggle("active", kidsActive);
  $("kids-timer").textContent = kidsActive
    ? `${remainingMinutes}:${String(remainingRemainder).padStart(2, "0")} remaining`
    : kidsMode.last_end_reason === "time_limit" && kidsMode.last_fold_succeeded === true
      ? "Time is up · safe fold verified"
      : String(kidsMode.last_end_reason || "").includes("fold_failed")
        ? "Session ended · safe fold needs parent attention"
        : "Choose an activity below";
  $("kids-progress-bar").style.width = `${remainingPercent}%`;
  const progress = document.querySelector(".kids-progress");
  progress.setAttribute("aria-valuenow", String(Math.round(remainingPercent)));
  $("kids-session-detail").textContent = kidsActive
    ? kidsCameraActive
      ? "I Spy camera is active for this bounded five-frame desk search only. End now stops capture and folds Reachy."
      : `${kidsActivityLabels[kidsProfile.activity] || "Kids activity"} · age ${kidsProfile.age_band} · ${kidsMode.turns_completed || 0} completed turn${kidsMode.turns_completed === 1 ? "" : "s"} · ${kidsMode.tool_policy === "voice-state-motion-only" ? "gentle voice-state motion only" : "no tools"}`
    : "Camera, personal Hermes memory, smart-home control, messaging, purchases, and power controls are unavailable to the child session.";
  $("kids-start-button").disabled = kidsActive || kidsLocked || kidsRequestPending || ["meeting", "sleep"].includes(powerMode);
  $("kids-stop-button").disabled = !kidsActive;
  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.hidden = kidsLocked && button.dataset.tab !== "kids";
  });
  if (kidsLocked && document.querySelector('[data-tab="kids"]')?.getAttribute("aria-selected") !== "true") {
    activateTab("kids");
  }
  document.querySelectorAll("[data-kids-activity], #kids-nickname, #kids-age-band, #kids-duration, #kids-language, #kids-motion-enabled").forEach((control) => {
    control.disabled = kidsActive || kidsRequestPending;
  });
  window.ReachyCamera?.setPolicy({
    enabled: !kidsActive && !kidsLocked && Boolean(payload.config?.camera_feed_enabled),
    controlsEnabled: Boolean(payload.config?.camera_controls_enabled),
    handedness: payload.config?.camera_controls_handedness || "right",
    powerMode,
    motorsEnabled: motorsEnabled === true,
    robotBusy,
  });
  const announcementBusy = Boolean(runtime.announcement_busy);
  const announcementQueueDepth = Number(runtime.announcement_queue_depth || 0);
  const announcementBlocked = ["meeting", "sleep"].includes(powerMode);
  $("announcement-badge").textContent = announcementBusy ? "Speaking" : announcementQueueDepth ? "Queued" : "Ready";
  $("announcement-queue-badge").textContent = `${announcementQueueDepth} queued`;
  $("announcement-current").textContent = runtime.announcement_current_preview || "—";
  $("announcement-last").textContent = runtime.announcement_last_text || "—";
  $("announcement-error").textContent = runtime.announcement_last_error || "";
  const announcementLiveText = announcementBusy
    ? `Reachy is speaking. ${announcementQueueDepth} announcements remain queued.`
    : announcementQueueDepth
      ? `${announcementQueueDepth} announcements queued.`
      : "Announcement playback is ready.";
  if (announcementLiveText !== lastAnnouncementLiveText) {
    $("announcement-live").textContent = announcementLiveText;
    lastAnnouncementLiveText = announcementLiveText;
  }
  $("announcement-send").disabled = announcementBlocked || announcementRequestPending;
  $("announcement-stop").disabled = !announcementBusy && announcementQueueDepth === 0;
}

function modelLabel(model) {
  if (model.id === "hermes-agent") return "Hermes default model";
  if (/^openclaw[/:]/i.test(model.id)) return `OpenClaw agent · ${model.id.replace(/^openclaw[/:]/i, "")}`;
  const root = model.root && model.root !== model.id ? model.root : model.id;
  return `${root} — ${model.id}`;
}

async function loadModels() {
  const select = $("model");
  const selected = currentConfig?.model || select.value || "hermes-agent";
  try {
    const response = await fetch("/api/models", { cache: "no-store" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    select.replaceChildren();
    (body.models || []).forEach((model) => {
      const option = document.createElement("option");
      option.value = model.id;
      option.textContent = modelLabel(model);
      select.appendChild(option);
    });
    const offered = [...select.options].some((option) => option.value === selected);
    if (!offered && select.options.length) {
      // e.g. an OpenClaw-only bridge: offer its agent instead of a Hermes route it does not have.
      select.value = select.options[0].value;
      $("model-help").textContent = `The bridge does not offer ${selected}. Save settings to use ${select.options[0].textContent}.`;
    } else if (!offered) {
      const option = document.createElement("option");
      option.value = selected;
      option.textContent = `${selected} — unavailable route`;
      select.appendChild(option);
      select.value = selected;
    } else {
      select.value = selected;
    }
    const health = body.health || {};
    const backends = (health.agent_backends || []).map((item) => item.label).filter(Boolean);
    if (backends.length && offered) $("model-help").textContent = `Agents connected through the bridge: ${backends.join(" and ")}.`;
    const tts = health.tts_provider || "configured Hermes provider";
    const stt = health.stt_provider || "configured Hermes provider";
    $("voice-provider").textContent = `Speech voice: ${tts} TTS · Recognition: ${stt} STT. These are independent from the agent model.`;
  } catch (error) {
    $("model-help").textContent = `Could not load Hermes model routes: ${String(error)}`;
  }
}

function replaceOptions(select, values, selected, label = (value) => value) {
  select.replaceChildren();
  values.forEach((value) => {
    const option = document.createElement("option");
    option.value = typeof value === "string" ? value : value.id;
    option.textContent = label(value);
    select.appendChild(option);
  });
  if (selected && ![...select.options].some((option) => option.value === selected)) {
    const option = document.createElement("option");
    option.value = selected;
    option.textContent = selected;
    select.appendChild(option);
  }
  if (selected) select.value = selected;
}

function refreshSpeechControls() {
  const sttSelected = $("stt_provider").value || currentConfig?.stt_provider || "configured";
  const ttsSelected = $("tts_provider").value || currentConfig?.tts_provider || "configured";
  const stt = voiceOptions.stt.find((item) => item.id === sttSelected) || {};
  const tts = voiceOptions.tts.find((item) => item.id === ttsSelected) || {};
  replaceOptions($("stt_model"), stt.models || [], currentConfig?.stt_model || "base");
  replaceOptions($("tts_model"), tts.models || [], currentConfig?.tts_model || "eleven_flash_v2_5");
  replaceOptions(
    $("tts_voice"),
    tts.voices || [],
    currentConfig?.tts_voice || "pNInz6obpgDQGcFmaJgB",
    (voice) => `${voice.name} — ${voice.id}`,
  );
}

function refreshAnnouncementSpeechControls() {
  const providerSelect = $("announcement-provider");
  const selectedProvider = providerSelect.value;
  providerSelect.replaceChildren(new Option("Use app voice setting", ""));
  (voiceOptions.tts || []).forEach((item) => providerSelect.add(new Option(item.label, item.id)));
  providerSelect.value = [...providerSelect.options].some((option) => option.value === selectedProvider)
    ? selectedProvider : "";
  const provider = voiceOptions.tts.find((item) => item.id === providerSelect.value) || {};
  const modelSelect = $("announcement-model");
  const voiceSelect = $("announcement-voice");
  const selectedModel = modelSelect.value;
  const selectedVoice = voiceSelect.value;
  modelSelect.replaceChildren(new Option("Use app model", ""));
  (provider.models || []).forEach((model) => modelSelect.add(new Option(model, model)));
  voiceSelect.replaceChildren(new Option("Use app voice", ""));
  (provider.voices || []).forEach((voice) => voiceSelect.add(new Option(`${voice.name} — ${voice.id}`, voice.id)));
  if ([...modelSelect.options].some((option) => option.value === selectedModel)) modelSelect.value = selectedModel;
  if ([...voiceSelect.options].some((option) => option.value === selectedVoice)) voiceSelect.value = selectedVoice;
}

async function loadVoiceOptions() {
  try {
    const response = await fetch("/api/voice-options", { cache: "no-store" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    voiceOptions = body;
    replaceOptions(
      $("stt_provider"), body.stt || [], currentConfig?.stt_provider || "configured", (item) => item.label,
    );
    replaceOptions(
      $("tts_provider"), body.tts || [], currentConfig?.tts_provider || "configured", (item) => item.label,
    );
    refreshSpeechControls();
    refreshAnnouncementSpeechControls();
    $("voice-provider").textContent = "Wake detection stays local. Selected STT and TTS run through the authenticated Hermes-host bridge.";
    toggleModePanels();
  } catch (error) {
    $("voice-provider").textContent = `Could not load speech providers: ${String(error)}`;
  }
}

$("stt_provider").addEventListener("change", () => {
  currentConfig = { ...currentConfig, stt_provider: $("stt_provider").value, stt_model: "" };
  refreshSpeechControls();
});
$("tts_provider").addEventListener("change", () => {
  currentConfig = { ...currentConfig, tts_provider: $("tts_provider").value, tts_model: "", tts_voice: "" };
  refreshSpeechControls();
});

async function fetchWithTimeout(url, options = {}, timeoutMs = 5000) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (error) {
    if (error.name === "AbortError") throw new Error(`No response from Reachy after ${timeoutMs / 1000} seconds`);
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

async function refreshStatus() {
  if (statusRefreshPending) return statusRefreshHealthy;
  statusRefreshPending = true;
  try {
    const response = await fetchWithTimeout("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    updateStatus(await response.json());
    statusRefreshHealthy = true;
  } catch (error) {
    statusRefreshHealthy = false;
    if (window.ReachyCamera?.isActive()) {
      window.ReachyCamera.stop("Camera stopped because Hermes status is unavailable.");
    }
    $("runtime-state").textContent = "Disconnected";
    $("runtime-detail").textContent = String(error);
    $("status-dot").className = "status-dot error";
    $("robot-mode-badge").textContent = "offline";
    $("motor-state").textContent = "Motor state unavailable";
    $("motor-state-dot").className = "motor-state-dot error";
    $("robot-readiness").textContent = "Remote controls disabled until live status returns";
    const disconnectedAnnouncement = "Motor state unavailable. Remote controls disabled until live status returns.";
    if (disconnectedAnnouncement !== lastMotorAnnouncement) {
      $("motor-state-live").textContent = disconnectedAnnouncement;
      lastMotorAnnouncement = disconnectedAnnouncement;
    }
    document.querySelectorAll(".manual-control, [data-power]").forEach((button) => { button.disabled = true; });
    $("emotion-select").disabled = true;
    $("announcement-send").disabled = true;
    $("announcement-stop").disabled = true;
    $("announcement-badge").textContent = "Offline";
    $("kids-start-button").disabled = true;
    $("kids-stop-button").disabled = true;
    $("kids-status-badge").textContent = "Offline";
    $("presence-enabled").disabled = true;
    $("presence-acknowledgement-enabled").disabled = true;
    $("presence-badge").textContent = "Offline";
    ["initiative-policy-enabled", "initiative-mode", "initiative-quiet-hours-enabled", "initiative-quiet-hours-start", "initiative-quiet-hours-end", "initiative-hourly-budget", "initiative-daily-budget", "contextual-offers-enabled", "contextual-offer-response-window", "contextual-offer-yes", "contextual-offer-no", "contextual-offer-later", "initiative-preferences-reset", "shared-physical-context-enabled", "presentation-window-seconds", "presentation-start", "presentation-stop"].forEach((id) => {
      $(id).disabled = true;
    });
    $("initiative-badge").textContent = "Offline";
  } finally {
    statusRefreshPending = false;
  }
  return statusRefreshHealthy;
}

// Poll status every 1.5 s while healthy, back off to 15 s while Reachy is unreachable, and
// pause entirely while the page is hidden.
let statusPollTimer = 0;
let statusPollFailures = 0;
function scheduleStatusPoll(delayMs) {
  window.clearTimeout(statusPollTimer);
  if (document.hidden) return;
  statusPollTimer = window.setTimeout(pollStatus, delayMs);
}
async function pollStatus() {
  const healthy = await refreshStatus();
  statusPollFailures = healthy ? 0 : Math.min(statusPollFailures + 1, 4);
  scheduleStatusPoll(healthy ? 1500 : Math.min(1500 * 2 ** statusPollFailures, 15000));
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    window.clearTimeout(statusPollTimer);
  } else {
    statusPollFailures = 0;
    pollStatus();
  }
});

async function refreshAgentRun() {
  if (!agentProfileActive || agentRunRequestPending) return;
  try {
    const endpoint = agentRunId ? "/api/agent/run/status" : "/api/agent/run/current";
    const response = await fetchWithTimeout(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Reachy-Adult-UI": "unlocked" },
      body: agentRunId ? JSON.stringify({ run_id: agentRunId }) : undefined,
    });
    if (!response.ok) {
      // Only an authoritative 4xx (stale, locked, forbidden, unknown run) ends the local run;
      // a network blip or bridge 5xx must not re-enable Preview while a plan is running.
      if (response.status >= 400 && response.status < 500) {
        currentAgentRun = null;
        agentRunId = "";
        window.sessionStorage.removeItem("homebody-agent-run-id");
        renderAgentRun();
      }
      return;
    }
    const body = await response.json();
    currentAgentRun = body.run || null;
    if (currentAgentRun?.run_id) {
      agentRunId = String(currentAgentRun.run_id);
      window.sessionStorage.setItem("homebody-agent-run-id", agentRunId);
    }
    renderAgentRun();
  } catch (error) {
    // Keep the last known run; the next poll reconciles it.
  }
}

async function refreshAgentActivity() {
  if (!agentProfileActive) {
    brokerAgentActivity = [];
    pendingAgentApproval = null;
    currentAgentRun = null;
    agentRunId = "";
    window.sessionStorage.removeItem("homebody-agent-run-id");
    $("agent-approval-sheet").hidden = true;
    renderAgentActivity();
    renderAgentRun();
    return;
  }
  if (agentActivityPending) return;
  agentActivityPending = true;
  try {
    await refreshAgentRun();
    const response = await fetchWithTimeout("/api/agent/activity", {
      cache: "no-store",
      headers: { "X-Reachy-Adult-UI": "unlocked" },
    });
    if (!response.ok) return;
    const body = await response.json();
    brokerAgentActivity = Array.isArray(body.activity) ? body.activity : [];
    renderAgentActivity();
    const activeCapability = [...brokerAgentActivity]
      .reverse()
      .find((item) => item.result_class === "running" && item.capability_id);
    if (activeCapability) {
      $("agent-current-task").textContent = `Using ${String(activeCapability.capability_id).replaceAll("_", " ")}…`;
    }
    const pendingResponse = await fetchWithTimeout("/api/agent/pending-approval", {
      cache: "no-store",
      headers: { "X-Reachy-Adult-UI": "unlocked" },
    });
    if (!pendingResponse.ok) return;
    const pendingBody = await pendingResponse.json();
    pendingAgentApproval = pendingBody.pending_approval || null;
    const sheet = $("agent-approval-sheet");
    sheet.hidden = !pendingAgentApproval;
    $("agent-approve-button").disabled = !pendingAgentApproval || agentRequestPending;
    if (pendingAgentApproval) {
      $("agent-current-task").textContent = "Waiting for exact phone approval";
      $("agent-approval-capability").textContent = String(pendingAgentApproval.capability_id || "").replaceAll("_", " ");
      $("agent-approval-arguments").textContent = JSON.stringify(pendingAgentApproval.arguments || {}, null, 2);
      $("agent-pending-approval").textContent = `Waiting · expires in ${Number(pendingAgentApproval.expires_in_seconds || 0)}s`;
    }
  } catch (error) {
    // Runtime status remains authoritative if the Hermes-host timeline is unavailable.
  } finally {
    agentActivityPending = false;
  }
}

function payloadFromForm() {
  const payload = {};
  fields.forEach((name) => {
    const el = $(name);
    if (!el) return;
    if (el.type === "checkbox") payload[name] = el.checked;
    else if (el.type === "number") payload[name] = Number(el.value);
    else payload[name] = el.value.trim();
  });
  const currentKey = $("current_api_key").value.trim();
  if (currentKey) payload.current_api_key = currentKey;
  return payload;
}

async function updatePresenceSettings() {
  if (presenceRequestPending) return;
  presenceRequestPending = true;
  $("presence-enabled").disabled = true;
  $("presence-acknowledgement-enabled").disabled = true;
  $("presence-message").textContent = "Saving Presence settings…";
  $("presence-message").className = "message";
  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        proactive_presence_enabled: $("presence-enabled").checked,
        presence_acknowledgement_enabled: $("presence-acknowledgement-enabled").checked,
      }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    $("presence-message").textContent = $("presence-enabled").checked
      ? "Presence is ready for trusted local signals."
      : "Proactive Presence is off.";
    $("presence-message").className = "message ok";
  } catch (error) {
    $("presence-message").textContent = String(error);
    $("presence-message").className = "message error";
  } finally {
    presenceRequestPending = false;
    await refreshStatus();
  }
}

$("presence-enabled").addEventListener("change", updatePresenceSettings);
$("presence-acknowledgement-enabled").addEventListener("change", updatePresenceSettings);

async function updateInitiativeSettings() {
  if (initiativeRequestPending) return;
  initiativeRequestPending = true;
  $("initiative-message").textContent = "Saving initiative policy…";
  $("initiative-message").className = "message";
  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        initiative_policy_enabled: $("initiative-policy-enabled").checked,
        initiative_mode: $("initiative-mode").value,
        initiative_quiet_hours_enabled: $("initiative-quiet-hours-enabled").checked,
        initiative_quiet_hours_start: $("initiative-quiet-hours-start").value,
        initiative_quiet_hours_end: $("initiative-quiet-hours-end").value,
        initiative_hourly_budget: Number($("initiative-hourly-budget").value),
        initiative_daily_budget: Number($("initiative-daily-budget").value),
        contextual_offers_enabled: $("contextual-offers-enabled").checked,
        contextual_offer_response_window_seconds: Number($("contextual-offer-response-window").value),
      }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    $("initiative-message").textContent = $("initiative-policy-enabled").checked
      ? ($("contextual-offers-enabled").checked
        ? "Initiative is active. Contextual offers may ask one yes/no question."
        : "Initiative eligibility is active. Contextual offers remain off.")
      : "Initiative policy is off.";
    $("initiative-message").className = "message ok";
  } catch (error) {
    $("initiative-message").textContent = String(error);
    $("initiative-message").className = "message error";
  } finally {
    initiativeRequestPending = false;
    await refreshStatus();
  }
}

["initiative-policy-enabled", "initiative-mode", "initiative-quiet-hours-enabled", "initiative-quiet-hours-start", "initiative-quiet-hours-end", "initiative-hourly-budget", "initiative-daily-budget", "contextual-offers-enabled", "contextual-offer-response-window"].forEach((id) => {
  $(id).addEventListener("change", updateInitiativeSettings);
});

async function respondToContextualOffer(responseValue) {
  if (initiativeRequestPending || !currentContextualOfferToken) return;
  initiativeRequestPending = true;
  $("initiative-message").textContent = `Sending ${responseValue}…`;
  $("initiative-message").className = "message";
  try {
    const response = await fetch("/api/initiative/offers/respond", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Reachy-Adult-UI": "unlocked",
      },
      body: JSON.stringify({ token: currentContextualOfferToken, response: responseValue }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    $("initiative-message").textContent = {
      yes: "Accepted. Reachy will only read the prepared help; no action was executed.",
      no: "Declined. Reachy will offer this kind of help less often.",
      later: "Snoozed. Reachy will wait a few hours before offering this kind of help again.",
    }[responseValue];
    $("initiative-message").className = "message ok";
  } catch (error) {
    $("initiative-message").textContent = String(error);
    $("initiative-message").className = "message error";
  } finally {
    initiativeRequestPending = false;
    await refreshStatus();
  }
}

$("contextual-offer-yes").addEventListener("click", () => respondToContextualOffer("yes"));
$("contextual-offer-no").addEventListener("click", () => respondToContextualOffer("no"));
$("contextual-offer-later").addEventListener("click", () => respondToContextualOffer("later"));

// Learned preferences: rebuilt only when they change, so polling never steals focus.
let initiativePreferencesSignature = "";
function renderInitiativePreferences(preferences, blocked) {
  const signature = JSON.stringify([preferences, blocked, initiativeRequestPending]);
  if (signature === initiativePreferencesSignature) return;
  initiativePreferencesSignature = signature;
  const list = $("initiative-preferences");
  list.replaceChildren();
  preferences.forEach((preference) => {
    const item = document.createElement("li");
    item.dataset.state = String(preference.state || "normal");
    const heading = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = String(preference.label || preference.category);
    const state = document.createElement("span");
    state.className = "badge";
    state.textContent = String(preference.state || "normal").replaceAll("_", " ");
    heading.append(title, " ", state);
    const explanation = document.createElement("small");
    explanation.textContent = String(preference.explanation || "");
    const controls = document.createElement("div");
    controls.className = "initiative-preference-controls";
    const allowed = document.createElement("label");
    allowed.className = "checkbox-row compact";
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.checked = !preference.disabled;
    toggle.disabled = blocked;
    toggle.dataset.preferenceCategory = String(preference.category);
    const toggleText = document.createElement("span");
    toggleText.textContent = "Allowed";
    allowed.append(toggle, toggleText);
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "secondary compact";
    reset.textContent = "Forget";
    reset.disabled = blocked;
    reset.dataset.preferenceReset = String(preference.category);
    controls.append(allowed, reset);
    item.append(heading, explanation, controls);
    list.appendChild(item);
  });
  $("initiative-preferences-reset").disabled = blocked;
}

async function updateInitiativePreferences(path, payload, successMessage) {
  if (initiativeRequestPending) return;
  initiativeRequestPending = true;
  try {
    const response = await fetchWithTimeout(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Reachy-Adult-UI": "unlocked" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error(await responseDetail(response));
    $("initiative-message").textContent = successMessage;
    $("initiative-message").className = "message ok";
  } catch (error) {
    $("initiative-message").textContent = `Could not update learned preferences: ${error.message || error}`;
    $("initiative-message").className = "message error";
  } finally {
    initiativeRequestPending = false;
    initiativePreferencesSignature = "";
    await refreshStatus();
  }
}

$("initiative-preferences").addEventListener("change", (event) => {
  const category = event.target?.dataset?.preferenceCategory;
  if (!category) return;
  const allowed = event.target.checked;
  updateInitiativePreferences(
    "/api/initiative/preferences",
    { category, disabled: !allowed },
    allowed ? "Category allowed again." : "Category turned off. Reachy will not take this initiative.",
  );
});
$("initiative-preferences").addEventListener("click", (event) => {
  const category = event.target?.dataset?.preferenceReset;
  if (!category) return;
  updateInitiativePreferences(
    "/api/initiative/preferences/reset",
    { category },
    "Reachy forgot what it learned for this category.",
  );
});
$("initiative-preferences-reset").addEventListener("click", () => {
  if (!window.confirm("Forget everything Reachy learned about your initiative preferences?")) return;
  updateInitiativePreferences(
    "/api/initiative/preferences/reset",
    {},
    "Reachy forgot all learned initiative preferences.",
  );
});

async function updatePresentationSettings() {
  if (presentationRequestPending) return;
  presentationRequestPending = true;
  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        shared_physical_context_enabled: $("shared-physical-context-enabled").checked,
        presentation_window_seconds: Number($("presentation-window-seconds").value),
      }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    $("presentation-message").textContent = $("shared-physical-context-enabled").checked
      ? "Shared physical context is ready. Start remains an explicit action."
      : "Shared physical context is off.";
    $("presentation-message").className = "message ok";
  } catch (error) {
    $("presentation-message").textContent = `Could not update presentation settings: ${error.message}`;
    $("presentation-message").className = "message error";
  } finally {
    presentationRequestPending = false;
    await refreshStatus();
  }
}

async function setPresentationWindow(active) {
  if (presentationRequestPending) return;
  presentationRequestPending = true;
  $("presentation-message").textContent = active
    ? "Starting the visible local presentation window…"
    : "Stopping the presentation window…";
  $("presentation-message").className = "message";
  try {
    const response = await fetch(`/api/presentation/${active ? "start" : "stop"}`, {
      method: "POST",
      headers: { "X-Reachy-Adult-UI": "unlocked" },
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    $("presentation-message").textContent = active
      ? "Local presentation window active. Hold the item or text steady in the center of Reachy's view."
      : "Presentation window stopped; ephemeral visual features were cleared.";
    $("presentation-message").className = "message ok";
  } catch (error) {
    $("presentation-message").textContent = `Presentation window failed: ${error.message}`;
    $("presentation-message").className = "message error";
  } finally {
    presentationRequestPending = false;
    await refreshStatus();
  }
}

["shared-physical-context-enabled", "presentation-window-seconds"].forEach((id) => {
  $(id).addEventListener("change", updatePresentationSettings);
});
$("presentation-start").addEventListener("click", () => setPresentationWindow(true));
$("presentation-stop").addEventListener("click", () => setPresentationWindow(false));
document.querySelector(".presentation-card").addEventListener("focusin", () => {
  presentationEditActive = true;
});
document.querySelector(".presentation-card").addEventListener("focusout", () => {
  window.setTimeout(() => {
    if (!document.querySelector(".presentation-card").contains(document.activeElement)) {
      presentationEditActive = false;
    }
  }, 0);
});

document.querySelector(".initiative-card").addEventListener("focusin", () => {
  initiativeEditActive = true;
});
document.querySelector(".initiative-card").addEventListener("focusout", () => {
  window.setTimeout(() => {
    initiativeEditActive = document.querySelector(".initiative-card").contains(document.activeElement);
  }, 0);
});

$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  setMessage("Saving…");
  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payloadFromForm()),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    setMessage(body.note || "Saved", "ok");
    $("api_key").value = "********";
    $("current_api_key").value = "";
    await refreshStatus();
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    button.disabled = false;
  }
});

$("test-button").addEventListener("click", async () => {
  const button = $("test-button");
  button.disabled = true;
  setMessage("Testing Hermes bridge…");
  try {
    const response = await fetch("/api/test-connection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payloadFromForm()),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    const agents = (body.health.agent_backends || []).map((item) => item.label).filter(Boolean);
    setMessage(`Connected: ${body.health.status || "ok"}${agents.length ? ` · ${agents.join(" and ")}` : ""}`, "ok");
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    button.disabled = false;
  }
});

document.querySelectorAll("[data-kids-activity]").forEach((button) => {
  button.addEventListener("click", () => {
    selectedKidsActivity = button.dataset.kidsActivity;
    document.querySelectorAll("[data-kids-activity]").forEach((candidate) => {
      const selected = candidate === button;
      candidate.classList.toggle("selected", selected);
      candidate.setAttribute("aria-checked", String(selected));
    });
    $("kids-activity-badge").textContent = kidsActivityLabels[selectedKidsActivity];
    $("kids-ispy-consent-row").hidden = selectedKidsActivity !== "ispy";
    $("kids-ispy-camera-consent").checked = false;
  });
  button.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    const buttons = [...document.querySelectorAll("[data-kids-activity]")];
    const offset = ["ArrowLeft", "ArrowUp"].includes(event.key) ? -1 : 1;
    buttons[(buttons.indexOf(button) + offset + buttons.length) % buttons.length].focus();
    buttons[(buttons.indexOf(button) + offset + buttons.length) % buttons.length].click();
  });
});

function kidsProfileFromForm() {
  return {
    nickname: $("kids-nickname").value.trim(),
    age_band: $("kids-age-band").value,
    activity: selectedKidsActivity,
    language: $("kids-language").value,
    duration_minutes: Number($("kids-duration").value),
    motion_enabled: $("kids-motion-enabled").checked,
    camera_consent: selectedKidsActivity === "ispy" && $("kids-ispy-camera-consent").checked,
  };
}

$("kids-start-button").addEventListener("click", async () => {
  const profile = kidsProfileFromForm();
  const message = $("kids-message");
  if (profile.activity === "ispy" && !profile.camera_consent) {
    message.textContent = "Allow the narrow camera search for this I Spy start.";
    message.className = "message error";
    $("kids-ispy-camera-consent").focus();
    return;
  }
  kidsRequestPending = true;
  const savedProfile = { ...profile };
  delete savedProfile.camera_consent;
  window.localStorage.setItem("homebody-kids-profile", JSON.stringify(savedProfile));
  message.textContent = "Starting the private, time-boxed child session…";
  message.className = "message";
  if (window.ReachyCamera?.isActive()) window.ReachyCamera.stop("Camera stopped before Kids Mode.");
  try {
    const response = await fetch("/api/kids/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(profile),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    message.textContent = "Kids Mode is active. Reachy is giving the child-safe greeting now.";
    message.className = "message ok";
  } catch (error) {
    message.textContent = String(error);
    message.className = "message error";
  } finally {
    kidsRequestPending = false;
    await refreshStatus();
  }
});

$("kids-stop-button").addEventListener("click", async () => {
  const message = $("kids-message");
  kidsRequestPending = true;
  message.textContent = "Ending voice and motion, then folding Reachy safely…";
  message.className = "message";
  try {
    const response = await fetch("/api/kids/stop", { method: "POST" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    message.textContent = "Kids Mode ended. Reachy is safely folded in Standby.";
    message.className = "message ok";
  } catch (error) {
    message.textContent = String(error);
    message.className = "message error";
  } finally {
    kidsRequestPending = false;
    await refreshStatus();
  }
});

$("announcement-provider").addEventListener("change", refreshAnnouncementSpeechControls);
announcementText.addEventListener("input", () => {
  $("announcement-count").textContent = `${announcementText.value.length.toLocaleString()} / 15,000`;
  window.sessionStorage.setItem("homebody-announcement-draft", announcementText.value);
});
announcementText.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    $("announcement-send").click();
  }
});
document.querySelectorAll(".announcement-template").forEach((button) => {
  button.addEventListener("click", () => {
    announcementText.value = button.dataset.announcementTemplate || "";
    announcementText.dispatchEvent(new Event("input"));
    announcementText.focus();
  });
});

$("announcement-send").addEventListener("click", async () => {
  const text = announcementText.value.trim();
  const message = $("announcement-message");
  if (!text) {
    message.textContent = "Enter announcement text first.";
    message.className = "message error";
    announcementText.focus();
    return;
  }
  announcementRequestPending = true;
  $("announcement-send").disabled = true;
  message.textContent = "Adding announcement to Reachy's playback queue…";
  message.className = "message";
  try {
    const response = await fetch("/api/announcements", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        provider: $("announcement-provider").value,
        model: $("announcement-model").value,
        voice: $("announcement-voice").value,
        behavior: $("announcement-behavior").value,
        repeat: Number($("announcement-repeat").value),
        pause_seconds: Number($("announcement-pause").value),
      }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    message.textContent = body.queue_depth > 1
      ? `Queued behind ${body.queue_depth - 1} announcement${body.queue_depth === 2 ? "" : "s"}.`
      : "Announcement accepted. Reachy is preparing to speak.";
    message.className = "message ok";
    announcementText.value = "";
    announcementText.dispatchEvent(new Event("input"));
  } catch (error) {
    message.textContent = String(error);
    message.className = "message error";
  } finally {
    announcementRequestPending = false;
    await refreshStatus();
  }
});

$("announcement-stop").addEventListener("click", async () => {
  const button = $("announcement-stop");
  const message = $("announcement-message");
  button.disabled = true;
  try {
    const response = await fetch("/api/announcements/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clear_queue: true }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    message.textContent = body.active_cancelled
      ? `Announcement stopped${body.queued_cleared ? ` and ${body.queued_cleared} queued cleared` : ""}.`
      : `${body.queued_cleared} queued announcement${body.queued_cleared === 1 ? "" : "s"} cleared.`;
    message.className = "message ok";
  } catch (error) {
    message.textContent = String(error);
    message.className = "message error";
  } finally {
    await refreshStatus();
  }
});

$("camera-test-button").addEventListener("click", async () => {
  const button = $("camera-test-button");
  const message = $("camera-message");
  button.disabled = true;
  message.textContent = "Capturing one local camera frame…";
  message.className = "message";
  try {
    const response = await fetch("/api/camera/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "camera" }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    message.textContent = `Camera ready: ${body.bytes} byte JPEG captured locally`;
    message.className = "message ok";
  } catch (error) {
    message.textContent = String(error);
    message.className = "message error";
  } finally {
    button.disabled = false;
  }
});

async function refreshRobotPose() {
  if (poseRefreshPending || $("panel-robot").hidden) return;
  poseRefreshPending = true;
  try {
    const response = await fetch("/api/robot/pose", { cache: "no-store" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    const pose = body.pose || {};
    const formatPose = (value) => {
      if (value === null || value === undefined || value === "") return "—";
      const number = Number(value);
      return Number.isFinite(number) ? number.toFixed(1) : "—";
    };
    ["x", "y", "z", "roll", "pitch", "yaw"].forEach((axis) => {
      $(`pose-${axis}`).textContent = formatPose(pose[axis]);
    });
    $("pose-body-yaw").textContent = formatPose(pose.body_yaw);
  } catch (error) {
    ["x", "y", "z", "roll", "pitch", "yaw", "body-yaw"].forEach((axis) => {
      $(`pose-${axis}`).textContent = "—";
    });
  } finally {
    poseRefreshPending = false;
  }
}

function renderBluetooth(state) {
  bluetoothState = state;
  const devices = Array.isArray(state.devices) ? state.devices : [];
  const select = $("bluetooth-device-select");
  const previous = select.value;
  select.replaceChildren();
  if (!devices.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No controller found";
    select.appendChild(option);
  } else {
    devices.forEach((device) => {
      const option = document.createElement("option");
      option.value = device.address;
      const flags = [
        device.connected ? "connected" : "",
        device.bonded ? "bonded" : device.paired ? "unbonded" : "",
      ]
        .filter(Boolean).join(", ");
      option.textContent = `${device.name} · ${device.address}${flags ? ` · ${flags}` : ""}`;
      select.appendChild(option);
    });
    if (devices.some((device) => device.address === previous)) select.value = previous;
  }
  const selected = devices.find((device) => device.address === select.value);
  $("bluetooth-adapter-state").textContent = !state.adapter_available
    ? "BlueZ unavailable"
    : state.adapter_powered ? "Powered on" : "Powered off";
  const features = [
    state.rumble_available ? "rumble" : "",
    state.gyro_available ? state.gyro_calibrating ? "gyro calibrating" : state.gyro_active ? "gyro active" : "gyro" : "",
    state.touchpad_available ? "touchpad" : "",
  ].filter(Boolean).join(", ");
  $("gamepad-state").textContent = state.gamepad_connected
    ? `${state.gamepad_name || "Connected"}${features ? ` · ${features}` : ""}`
    : state.gamepad_enabled ? "Waiting for /dev/input/js*" : "Disabled";
  $("gamepad-last-action").textContent = state.last_gamepad_action || "—";
  $("gamepad-enabled").checked = Boolean(state.gamepad_enabled);
  $("gamepad-enabled").disabled = false;
  $("bluetooth-scan-button").disabled = !state.adapter_available || Boolean(state.scan_active);
  $("bluetooth-pair-button").disabled = !selected || Boolean(selected.bonded);
  $("bluetooth-connect-button").disabled = !selected || !selected.bonded || Boolean(selected.connected);
  $("bluetooth-disconnect-button").disabled = !selected || !selected.connected;
  $("bluetooth-remove-button").disabled = !selected || !(selected.paired || selected.connected);
  const connected = devices.some((device) => device.connected && device.bonded);
  $("bluetooth-badge").textContent = state.gamepad_connected
    ? "Controller ready" : connected ? "Bluetooth connected" : state.adapter_available ? "Ready" : "Unavailable";
  const message = $("bluetooth-message");
  if (state.last_error) {
    message.textContent = state.last_error;
    message.className = "message error";
  } else if (state.feature_error) {
    message.textContent = state.feature_error;
    message.className = "message error";
  } else if (!message.classList.contains("ok")) {
    message.textContent = "";
    message.className = "message";
  }
}

async function refreshBluetooth() {
  if (bluetoothRefreshPending || $("panel-robot").hidden) return;
  bluetoothRefreshPending = true;
  try {
    const response = await fetch("/api/bluetooth/status", { cache: "no-store" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    renderBluetooth(body);
  } catch (error) {
    $("bluetooth-badge").textContent = "Unavailable";
    $("bluetooth-adapter-state").textContent = "Unavailable";
    $("gamepad-state").textContent = "Disabled";
    $("bluetooth-message").textContent = String(error);
    $("bluetooth-message").className = "message error";
    [
      "bluetooth-scan-button", "bluetooth-pair-button", "bluetooth-connect-button",
      "bluetooth-disconnect-button", "bluetooth-remove-button", "gamepad-enabled",
    ].forEach((id) => { $(id).disabled = true; });
  } finally {
    bluetoothRefreshPending = false;
  }
}

async function bluetoothCommand(path, payload, pendingText) {
  const message = $("bluetooth-message");
  message.textContent = pendingText;
  message.className = "message";
  [
    "bluetooth-scan-button", "bluetooth-pair-button", "bluetooth-connect-button",
    "bluetooth-disconnect-button", "bluetooth-remove-button", "gamepad-enabled",
  ].forEach((id) => { $(id).disabled = true; });
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    renderBluetooth(body);
    if (body.last_error) throw new Error(body.last_error);
    message.textContent = "Bluetooth controller settings updated.";
    message.className = "message ok";
  } catch (error) {
    await refreshBluetooth();
    message.textContent = String(error);
    message.className = "message error";
  }
}

$("bluetooth-device-select").addEventListener("change", () => {
  if (bluetoothState) renderBluetooth(bluetoothState);
});
$("bluetooth-scan-button").addEventListener("click", () => {
  bluetoothCommand("/api/bluetooth/scan", { seconds: 12 }, "Scanning—put the controller in pairing mode now…");
});
$("bluetooth-pair-button").addEventListener("click", () => {
  bluetoothCommand(
    "/api/bluetooth/pair",
    { address: $("bluetooth-device-select").value },
    "Pairing, trusting, and connecting controller…",
  );
});
$("bluetooth-connect-button").addEventListener("click", () => {
  bluetoothCommand(
    "/api/bluetooth/connect",
    { address: $("bluetooth-device-select").value },
    "Connecting controller…",
  );
});
$("bluetooth-disconnect-button").addEventListener("click", () => {
  bluetoothCommand(
    "/api/bluetooth/disconnect",
    { address: $("bluetooth-device-select").value },
    "Disconnecting controller…",
  );
});
$("bluetooth-remove-button").addEventListener("click", () => {
  if (!window.confirm("Forget this controller from Reachy Pi?")) return;
  bluetoothCommand(
    "/api/bluetooth/remove",
    { address: $("bluetooth-device-select").value },
    "Removing paired controller…",
  );
});
$("gamepad-enabled").addEventListener("change", (event) => {
  const enabled = event.target.checked;
  if (enabled && !window.confirm("Enable controller movement? Keep Reachy clear of obstacles.")) {
    event.target.checked = false;
    return;
  }
  bluetoothCommand(
    "/api/bluetooth/gamepad",
    { enabled },
    enabled ? "Enabling safe controller input…" : "Disabling controller input…",
  );
});

function gpioPinLabel(name, pin) {
  return pin === null || pin === undefined ? "" : `${name} GPIO${pin}`;
}

function renderGpio(state) {
  const configured = state.configured || {};
  const stuck = Array.isArray(state.stuck) ? state.stuck : [];
  let label = "Off";
  let badge = "Off";
  if (state.enabled && state.available) {
    label = stuck.length ? `Ignoring stuck ${stuck.join(" and ")}` : "Listening for presses";
    badge = stuck.length ? "Check button" : "Ready";
  } else if (configured.enabled) {
    label = "Unavailable";
    badge = "Unavailable";
  }
  $("gpio-state").textContent = label;
  $("gpio-badge").textContent = badge;
  const pins = [gpioPinLabel("Green", configured.green_pin), gpioPinLabel("Red", configured.red_pin)]
    .filter(Boolean).join(" · ");
  $("gpio-pins").textContent = pins || "None set";
  const [button, gesture] = String(state.last_event || "").split(" ");
  $("gpio-last-event").textContent = button
    ? `${button[0].toUpperCase()}${button.slice(1)} ${gesture === "long" ? "hold" : "press"}`
    : "—";
  if (!gpioFormDirty) {
    $("gpio-enabled").checked = Boolean(configured.enabled);
    $("gpio-green-pin").value = configured.green_pin ?? "";
    $("gpio-red-pin").value = configured.red_pin ?? "";
    $("gpio-long-press").value = configured.long_press_seconds ?? 2;
  }
  GPIO_CONTROL_IDS.forEach((id) => { $(id).disabled = false; });
  const message = $("gpio-message");
  if (state.last_error) {
    message.textContent = state.last_error;
    message.className = "message error";
  } else if (!message.classList.contains("ok")) {
    message.textContent = "";
    message.className = "message";
  }
}

async function refreshGpio() {
  if (gpioRefreshPending || $("panel-robot").hidden) return;
  gpioRefreshPending = true;
  try {
    const response = await fetchWithTimeout("/api/gpio/status", { cache: "no-store" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    renderGpio(body);
  } catch (error) {
    $("gpio-badge").textContent = "Unavailable";
    $("gpio-message").textContent = String(error);
    $("gpio-message").className = "message error";
    GPIO_CONTROL_IDS.forEach((id) => { $(id).disabled = true; });
  } finally {
    gpioRefreshPending = false;
  }
}

function gpioPinValue(id) {
  const raw = $(id).value.trim();
  return raw === "" ? null : Number(raw);
}

async function saveGpioSettings() {
  const message = $("gpio-message");
  const payload = {
    enabled: $("gpio-enabled").checked,
    green_pin: gpioPinValue("gpio-green-pin"),
    red_pin: gpioPinValue("gpio-red-pin"),
    long_press_seconds: Number($("gpio-long-press").value || 2),
  };
  message.textContent = payload.enabled ? "Opening the button lines…" : "Turning the buttons off…";
  message.className = "message";
  GPIO_CONTROL_IDS.forEach((id) => { $(id).disabled = true; });
  try {
    const response = await fetchWithTimeout("/api/gpio/buttons", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await response.json();
    if (!response.ok) {
      const detail = Array.isArray(body.detail) ? "Check the line numbers (0–1023) and hold time (0.5–10 s)." : body.detail;
      throw new Error(detail || `HTTP ${response.status}`);
    }
    gpioFormDirty = false;
    renderGpio(body);
    if (body.last_error) throw new Error(body.last_error);
    message.textContent = payload.enabled ? "Physical buttons are on." : "Physical buttons are off.";
    message.className = "message ok";
  } catch (error) {
    gpioFormDirty = false;
    await refreshGpio();
    message.textContent = String(error.message || error);
    message.className = "message error";
  }
}

["gpio-green-pin", "gpio-red-pin", "gpio-long-press"].forEach((id) => {
  $(id).addEventListener("input", () => { gpioFormDirty = true; });
});
$("gpio-enabled").addEventListener("change", () => {
  gpioFormDirty = true;
  saveGpioSettings();
});
$("gpio-save-button").addEventListener("click", saveGpioSettings);

async function sendPrecisionRobotAction(axis, delta) {
  const message = $("robot-message");
  manualActionPending = true;
  const isCenter = axis.startsWith("center_");
  message.textContent = currentPowerMode === "standby"
    ? `Waking Reachy before ${isCenter ? axis.replace("_", " ") : `${axis} ${delta > 0 ? "+" : ""}${delta}`}…`
    : `Moving ${isCenter ? axis.replace("_", " ") : `${axis} ${delta > 0 ? "+" : ""}${delta}`}…`;
  message.className = "message";
  document.querySelectorAll(".manual-control, [data-power]").forEach((button) => { button.disabled = true; });
  try {
    const response = await fetch("/api/robot/nudge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ axis, delta }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    message.textContent = `${isCenter ? axis.replace("_", " ") : `${axis} ${delta > 0 ? "+" : ""}${delta}`} started · Reachy is ${body.power_mode}`;
    message.className = "message ok";
  } catch (error) {
    message.textContent = String(error);
    message.className = "message error";
  } finally {
    manualActionPending = false;
    await refreshStatus();
  }
}

async function sendManualRobotAction(action, value) {
  const message = $("robot-message");
  manualActionPending = true;
  message.textContent = currentPowerMode === "standby"
    ? `Waking Reachy before ${action}: ${value}…`
    : `Starting ${action}: ${value}…`;
  message.className = "message";
  document.querySelectorAll(".manual-control, [data-power]").forEach((button) => { button.disabled = true; });
  $("emotion-select").disabled = true;
  try {
    const response = await fetch("/api/robot/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, value }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    message.textContent = `${action === "look" ? "Look" : action} ${value} started · Reachy is ${body.power_mode}`;
    message.className = "message ok";
  } catch (error) {
    message.textContent = String(error);
    message.className = "message error";
  } finally {
    manualActionPending = false;
    await refreshStatus();
  }
}

document.querySelectorAll("[data-robot-action]").forEach((button) => {
  button.addEventListener("click", () => {
    if (button.dataset.confirm && !window.confirm(button.dataset.confirm)) return;
    sendManualRobotAction(button.dataset.robotAction, button.dataset.robotValue);
  });
});

document.querySelectorAll("[data-nudge-axis]").forEach((button) => {
  button.addEventListener("click", () => {
    const stepControl = button.dataset.nudgeAxis === "body_yaw" ? "base-yaw-step" : "precision-step";
    const step = Number($(stepControl).value);
    const sign = Number(button.dataset.nudgeSign);
    const delta = Number.isFinite(sign) ? step * sign : 0;
    if (
      button.dataset.nudgeAxis === "body_yaw"
      && Math.abs(delta) >= 30
      && !window.confirm("Wide base turns need clear space around Reachy. Continue?")
    ) return;
    sendPrecisionRobotAction(button.dataset.nudgeAxis, delta);
  });
});

const dPad = $("look-d-pad");
dPad.addEventListener("keydown", (event) => {
  const keyDirections = {
    ArrowUp: "up",
    ArrowDown: "down",
    ArrowLeft: "left",
    ArrowRight: "right",
    Home: "center",
  };
  const direction = keyDirections[event.key];
  if (!direction) return;
  event.preventDefault();
  dPad.querySelector(`[data-robot-value="${direction}"]`)?.click();
});

$("emotion-button").addEventListener("click", () => {
  sendManualRobotAction("emotion", $("emotion-select").value);
});

$("robot-stop-button").addEventListener("click", async () => {
  const button = $("robot-stop-button");
  const message = $("robot-message");
  button.disabled = true;
  message.textContent = "Stopping active and queued movement…";
  message.className = "message";
  try {
    const response = await fetch("/api/robot/stop", { method: "POST" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    message.textContent = !body.robot_stopped
      ? "Stop requested — Reachy is still settling"
      : body.active_cancelled
        ? "Movement stopped"
        : body.queued_cancelled
          ? `Cleared ${body.queued_cancelled} queued movement${body.queued_cancelled === 1 ? "" : "s"}`
          : "Robot is already stopped";
    message.className = "message ok";
  } catch (error) {
    message.textContent = String(error);
    message.className = "message error";
  } finally {
    button.disabled = false;
    await refreshStatus();
  }
});

async function previewAgentRun() {
  if (agentRunRequestPending || !agentProfileActive) return;
  const goal = $("agent-run-goal").value.trim();
  if (!goal) {
    $("agent-message").textContent = "Enter a concrete goal before previewing a plan.";
    $("agent-message").className = "message error";
    return;
  }
  agentRunRequestPending = true;
  renderAgentRun();
  const message = $("agent-message");
  message.textContent = "Creating an exact bounded plan; nothing is running yet…";
  message.className = "message";
  try {
    const response = await fetch("/api/agent/run/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Reachy-Adult-UI": "unlocked" },
      body: JSON.stringify({ goal }),
    });
    const body = await response.json();
    if (!response.ok || !body.run) throw new Error(body.detail || `HTTP ${response.status}`);
    currentAgentRun = body.run;
    agentRunId = String(body.run.run_id || "");
    window.sessionStorage.setItem("homebody-agent-run-id", agentRunId);
    message.textContent = "Plan ready. Review every step and exact argument, then press Start.";
    message.className = "message ok";
  } catch (error) {
    message.textContent = String(error);
    message.className = "message error";
  } finally {
    agentRunRequestPending = false;
    renderAgentRun();
  }
}

async function sendAgentRunAction(action, stepId = "") {
  if (agentRunRequestPending || !currentAgentRun || !agentRunId) return;
  agentRunRequestPending = true;
  renderAgentRun();
  const message = $("agent-message");
  const labels = {
    start: "Starting the reviewed plan…",
    pause: "Pausing after cancellation reaches the current bounded step…",
    resume: "Resuming the unchanged generation-bound plan…",
    cancel: "Cancelling the run and every queued step…",
    approve: "Applying one exact phone approval…",
  };
  message.textContent = labels[action] || "Updating Agent run…";
  message.className = "message";
  try {
    const response = await fetch(`/api/agent/run/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Reachy-Adult-UI": "unlocked" },
      body: JSON.stringify({ run_id: agentRunId, ...(stepId ? { step_id: stepId } : {}) }),
    });
    const body = await response.json();
    if (!response.ok || !body.run) throw new Error(body.detail || `HTTP ${response.status}`);
    currentAgentRun = body.run;
    message.textContent = action === "approve"
      ? "Exact step approved; the bounded run will continue until its next boundary."
      : `Agent run ${String(body.run.status || action).replaceAll("_", " ")}.`;
    message.className = "message ok";
  } catch (error) {
    message.textContent = String(error);
    message.className = "message error";
  } finally {
    agentRunRequestPending = false;
    renderAgentRun();
    window.setTimeout(refreshAgentRun, 350);
  }
}

$("agent-run-preview-button").addEventListener("click", previewAgentRun);
$("agent-run-start-button").addEventListener("click", () => sendAgentRunAction("start"));
$("agent-run-pause-button").addEventListener("click", () => sendAgentRunAction("pause"));
$("agent-run-resume-button").addEventListener("click", () => sendAgentRunAction("resume"));
$("agent-run-cancel-button").addEventListener("click", () => {
  if (window.confirm("Cancel this run? Queued steps will not execute and the run cannot resume.")) {
    sendAgentRunAction("cancel");
  }
});
$("agent-run-approve-button").addEventListener("click", () => {
  const step = (currentAgentRun?.steps || []).find((item) => item.step_id === currentAgentRun.active_step_id);
  if (!step) return;
  const exact = `${String(step.capability_id).replaceAll("_", " ")}\n${JSON.stringify(step.arguments || {}, null, 2)}`;
  if (window.confirm(`Approve this exact step once?\n\n${exact}`)) {
    sendAgentRunAction("approve", step.step_id);
  }
});

async function setAgentProfile(profile) {
  if (agentRequestPending || agentRunRequestPending) return;
  const runStatus = currentAgentRun?.status || "";
  if (
    profile !== "agent"
    && currentAgentRun
    && !["completed", "partial", "failed", "cancelled"].includes(runStatus)
    && !window.confirm("Leaving Agent Mode invalidates this run and every pending approval. Continue?")
  ) return;
  agentRequestPending = true;
  const message = $("agent-message");
  message.textContent = profile === "agent" ? "Starting a fresh Agent session…" : "Returning to Conversation…";
  message.className = "message";
  try {
    const response = await fetch("/api/agent/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Reachy-Adult-UI": "unlocked" },
      body: JSON.stringify({ profile }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    message.textContent = profile === "agent"
      ? "Agent profile active. Reversible owner tools are available; consequential actions pause for exact phone approval."
      : "Conversation profile active.";
    message.className = "message ok";
  } catch (error) {
    message.textContent = String(error);
    message.className = "message error";
  } finally {
    agentRequestPending = false;
    await refreshStatus();
  }
}

$("agent-enable-button").addEventListener("click", () => setAgentProfile("agent"));
$("agent-conversation-button").addEventListener("click", () => setAgentProfile("conversation"));
$("agent-approve-button").addEventListener("click", async () => {
  if (agentRequestPending || !pendingAgentApproval) return;
  agentRequestPending = true;
  const message = $("agent-message");
  $("agent-approve-button").disabled = true;
  try {
    const response = await fetch("/api/agent/approve-pending", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Reachy-Adult-UI": "unlocked" },
      body: JSON.stringify({ draft_id: pendingAgentApproval.draft_id }),
    });
    const body = await response.json();
    if (!response.ok || body.verified !== true) throw new Error(body.detail || `HTTP ${response.status}`);
    message.textContent = "Exact action completed and verified.";
    message.className = "message ok";
    pendingAgentApproval = null;
    $("agent-approval-sheet").hidden = true;
  } catch (error) {
    message.textContent = String(error);
    message.className = "message error";
  } finally {
    agentRequestPending = false;
    await refreshStatus();
    await refreshAgentActivity();
  }
});
$("agent-stop-button").addEventListener("click", async () => {
  if (agentRequestPending) return;
  agentRequestPending = true;
  const message = $("agent-message");
  try {
    const response = await fetch("/api/agent/stop", { method: "POST" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    message.textContent = "Agent work stopped; pending approvals and late results are invalid.";
    message.className = "message ok";
  } catch (error) {
    message.textContent = String(error);
    message.className = "message error";
  } finally {
    agentRequestPending = false;
    await refreshStatus();
  }
});

async function setPowerMode(mode, durationMinutes = 60, message = $("power-message")) {
  if (powerTransitionPending) return;
  powerTransitionPending = true;
  document.querySelectorAll("[data-power], .manual-control").forEach((button) => { button.disabled = true; });
  $("emotion-select").disabled = true;
  $("robot-stop-button").disabled = true;
  if (mode !== "awake" && window.ReachyCamera?.isActive()) {
    window.ReachyCamera.stop(`Camera stopped before switching to ${mode}.`);
  }
  message.textContent = mode === "standby"
    ? "Folding Reachy before disabling motor torque…"
    : mode === "awake"
      ? "Enabling motor torque and waking Reachy…"
      : `Switching to ${mode}…`;
  message.className = "message";
  try {
    const response = await fetch("/api/power", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, duration_minutes: durationMinutes }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    const runtime = body.runtime || {};
    if (mode === "awake" && (runtime.power_mode !== "awake" || runtime.motors_enabled !== true)) {
      throw new Error("Awake was not confirmed by the robot runtime");
    }
    if (mode === "standby" && (runtime.motors_enabled !== false || runtime.head_safely_folded !== true)) {
      throw new Error("Safe folded Standby was not confirmed by the robot runtime");
    }
    message.textContent = mode === "standby"
      ? "Reachy folded safely · motor torque disabled"
      : mode === "awake"
        ? "Reachy awake · motor torque enabled"
        : `Power mode: ${body.runtime.power_mode}`;
    message.className = "message ok";
  } catch (error) {
    message.textContent = String(error);
    message.className = "message error";
  } finally {
    powerTransitionPending = false;
    await refreshStatus();
  }
}

document.querySelectorAll("[data-power]").forEach((button) => {
  button.addEventListener("click", () => {
    const panel = button.closest("[data-panel]");
    const message = panel.closest("#panel-robot") ? $("robot-message") : $("power-message");
    setPowerMode(button.dataset.power, Number(button.dataset.minutes || 60), message);
  });
});

async function responseDetail(response) {
  // An HTML 502 from a proxy is not JSON; report the HTTP status instead of a parse error.
  try {
    const body = await response.json();
    return body.detail || `HTTP ${response.status}`;
  } catch {
    return `HTTP ${response.status}`;
  }
}

function setPowerMessage(text, kind) {
  $("power-message").textContent = text;
  $("power-message").className = `message ${kind}`;
}

$("app-off-button").addEventListener("click", async () => {
  if (!window.confirm("Stop the voice app? Restart it later from Reachy Control.")) return;
  if (window.ReachyCamera?.isActive()) {
    window.ReachyCamera.stop("Camera stopped before stopping the voice app.");
  }
  try {
    const response = await fetchWithTimeout("/api/app-off", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: "off" }),
    });
    if (!response.ok) throw new Error(await responseDetail(response));
    setPowerMessage("Voice app is stopping", "ok");
  } catch (error) {
    setPowerMessage(`Could not stop the voice app: ${error.message || error}`, "error");
  }
});

$("shutdown-button").addEventListener("click", async () => {
  if (window.prompt(`Type SHUTDOWN to safely power off the ${hostShutdownLabel}`) !== "SHUTDOWN") return;
  try {
    const response = await fetchWithTimeout("/api/shutdown", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: "shutdown" }),
    });
    if (!response.ok) throw new Error(await responseDetail(response));
    setPowerMessage(`${hostShutdownLabel} is shutting down safely`, "ok");
  } catch (error) {
    setPowerMessage(`Shutdown was not started: ${error.message || error}`, "error");
  }
});

async function loadRobotOptions() {
  try {
    const response = await fetch("/api/robot/options", { cache: "no-store" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    const emotionSelect = $("emotion-select");
    const selected = emotionSelect.value || "happy";
    const emotionLabels = {
      happy: "Happy · animated",
      excited: "Excited · animated",
      loving: "Loving · gentle",
      grateful: "Grateful · gentle",
      thinking: "Thinking · subtle",
      confused: "Confused · expressive",
      sad: "Sad · gentle",
      surprised: "Surprised · animated",
      calm: "Calm · gentle",
      welcoming: "Welcoming · expressive",
      yes: "Yes · head nod",
      no: "No · head shake",
    };
    emotionSelect.replaceChildren();
    (body.emotion || []).forEach((emotion) => {
      const option = document.createElement("option");
      option.value = emotion;
      option.textContent = emotionLabels[emotion] || emotion.charAt(0).toUpperCase() + emotion.slice(1);
      emotionSelect.appendChild(option);
    });
    if ([...emotionSelect.options].some((option) => option.value === selected)) emotionSelect.value = selected;
    const allowedLook = new Set(body.look || []);
    document.querySelectorAll('[data-robot-action="look"]').forEach((button) => {
      button.hidden = !allowedLook.has(button.dataset.robotValue);
    });
    const allowedDances = new Set(body.dance || []);
    document.querySelectorAll('[data-robot-action="dance"]').forEach((button) => {
      button.hidden = !allowedDances.has(button.dataset.robotValue);
    });
  } catch (error) {
    $("robot-message").textContent = `Could not load robot controls: ${String(error)}`;
    $("robot-message").className = "message error";
  }
}

async function startUi() {
  await refreshStatus();
  await Promise.all([loadModels(), loadVoiceOptions(), loadRobotOptions()]);
}

startUi().finally(() => scheduleStatusPoll(1500));
setInterval(refreshAgentActivity, 5000);
setInterval(() => {
  if (!$("panel-robot").hidden) {
    refreshBluetooth();
    refreshGpio();
  }
}, 5000);
