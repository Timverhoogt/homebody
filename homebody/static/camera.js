(() => {
  "use strict";

  const CAMERA_CONTROL_DEAD_ZONE = 0.16;
  const CAMERA_CONTROL_INTERVAL_MS = 50;
  const byId = (id) => document.getElementById(id);
  const state = {
    api: null,
    session: null,
    stream: null,
    producersListener: null,
    connectionListener: null,
    producerId: null,
    playbackGeneration: 0,
    fallbackAbort: null,
    connectionTimer: null,
    enabled: false,
    controlsEnabled: false,
    controlsHandedness: "right",
    motorsEnabled: false,
    robotBusy: false,
    powerMode: "unknown",
    requested: false,
    visionStartedAt: null,
    visionTimer: null,
    cameraControlSession: null,
    cameraControlSequence: 0,
    controlPointerId: null,
    controlGeneration: 0,
    controlInFlight: false,
    controlTimer: null,
    desiredPan: 0,
    desiredTilt: 0,
    controlOriginX: null,
    controlOriginY: null,
    controlRadius: null,
    centering: false,
    releasing: false,
    releaseToken: 0,
    stopping: false,
    movementSpeed: 0.6,
    lastFrameAt: null,
    frameCount: 0,
    directDecoded: false,
    frameCallback: null,
    healthTimer: null,
    transport: "Off",
    statsInFlight: false,
    statsLastAt: 0,
    appFullscreen: false,
    appFullscreenAnchor: null,
  };

  // Explicit clipboard export only: bounded RAM, never SDP, addresses, frames or tokens.
  let connectionReport = null;
  let reportExpiryTimer = null;
  let reportStatsTimer = null;
  const REPORT_STATES = new Set(["new", "checking", "connecting", "connected", "completed", "complete", "gathering", "failed", "closed", "disconnected", "in-progress", "waiting", "frozen", "succeeded"]);
  const reportState = value => REPORT_STATES.has(value) ? value : "unknown";
  const reportNumber = value => Number.isFinite(value) && value >= 0 ? Math.min(value, 1e12) : 0;
  function resetConnectionReport() {
    if (!byId("camera-copy-report")) return; // Older cached HTML must keep its camera/Stop working.
    if (reportExpiryTimer !== null) clearTimeout(reportExpiryTimer);
    const chrome = navigator.userAgent.match(/(?:Chrome|CriOS)\/(\d+)/);
    const report = { revision: "v76", browser: chrome ? `Chrome ${chrome[1]}` : "Other browser",
      platform: /Android/i.test(navigator.userAgent) ? "Android" : "Other", startedAt: performance.now(),
      events: [], last: null, failure: null };
    connectionReport = report;
    byId("camera-report-text").value = "";
    byId("camera-report-text").hidden = true;
    byId("camera-report-status").textContent = "Report stays on this device until you copy it";
    reportExpiryTimer = setTimeout(() => {
      if (connectionReport !== report) return;
      connectionReport = null;
      byId("camera-report-text").value = "";
      byId("camera-report-text").hidden = true;
      byId("camera-report-status").textContent = "Report expired · start a new camera attempt";
    }, 600000);
  }
  function reportPeer(peer) {
    return { ice: reportState(peer?.iceConnectionState), connection: reportState(peer?.connectionState),
      gathering: reportState(peer?.iceGatheringState), dtls: connectionReport?.last?.peer?.dtls || "unknown" };
  }
  function reportEvent(event, peer = state.session?.rtcPeerConnection) {
    if (!connectionReport) return;
    connectionReport.events.push({ ms: Math.round(performance.now() - connectionReport.startedAt), event,
      ...(peer ? { ice: reportState(peer.iceConnectionState), connection: reportState(peer.connectionState) } : {}) });
    connectionReport.events = connectionReport.events.slice(-8);
  }
  function reportFailure(reason) {
    if (!connectionReport || connectionReport.failure) return;
    const peer = state.session?.rtcPeerConnection;
    connectionReport.failure = { reason, ms: Math.round(performance.now() - connectionReport.startedAt),
      peer: reportPeer(peer), pairs: connectionReport.last?.pairs || [], video: connectionReport.last?.video || [] };
    reportEvent(reason, peer);
  }
  function recordReportStats(stats, peer) {
    if (!connectionReport) return;
    const reports = [...stats.values()];
    const transport = reports.find(r => r.type === "transport");
    const selected = r => Boolean(r.nominated || (transport?.selectedCandidatePairId && r.id === transport.selectedCandidatePairId));
    const kind = candidate => `${["host", "srflx", "prflx", "relay"].includes(candidate?.candidateType) ? candidate.candidateType : "unknown"}/${["udp", "tcp"].includes(candidate?.protocol) ? candidate.protocol : "unknown"}`;
    connectionReport.last = { peer: { ...reportPeer(peer), dtls: reportState(transport?.dtlsState) },
      pairs: reports.filter(r => r.type === "candidate-pair").sort((a, b) => Number(selected(b)) - Number(selected(a))).slice(0, 4).map(r => ({
        state: reportState(r.state), selected: selected(r),
        local: kind(stats.get(r.localCandidateId)), remote: kind(stats.get(r.remoteCandidateId)),
        requestsSent: reportNumber(r.requestsSent), responsesReceived: reportNumber(r.responsesReceived),
        requestsReceived: reportNumber(r.requestsReceived), responsesSent: reportNumber(r.responsesSent),
        bytesReceived: reportNumber(r.bytesReceived), rttMs: Math.round(reportNumber(r.currentRoundTripTime) * 1000) })),
      video: reports.filter(r => r.type === "inbound-rtp" && (r.kind === "video" || r.mediaType === "video")).slice(0, 2).map(r => {
        const mime = stats.get(r.codecId)?.mimeType;
        return { codec: ["video/VP8", "video/VP9", "video/H264", "video/H265", "video/AV1"].includes(mime) ? mime : "unknown",
          decoded: reportNumber(r.framesDecoded), packets: reportNumber(r.packetsReceived), bytes: reportNumber(r.bytesReceived) };
      }) };
  }
  async function copyConnectionReport() {
    if (connectionReport && performance.now() - connectionReport.startedAt >= 600000) {
      connectionReport = null;
      byId("camera-report-text").value = "";
      byId("camera-report-text").hidden = true;
    }
    if (!connectionReport) {
      byId("camera-report-status").textContent = "Start the camera once, then copy its connection report.";
      return;
    }
    const report = connectionReport;
    const text = JSON.stringify({ homebodyCamera: report.revision, browser: report.browser, platform: report.platform,
      transport: state.transport, failure: report.failure, last: report.last, events: report.events });
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(text);
      if (connectionReport === report) byId("camera-report-status").textContent = "Copied · paste the report into Telegram";
    } catch {
      if (connectionReport !== report) return;
      const area = byId("camera-report-text");
      area.value = text;
      area.hidden = false;
      area.focus();
      area.select();
      byId("camera-report-status").textContent = "Clipboard blocked · press and hold the selected report to copy";
    }
  }

  // Lens beside the status badge (scene.js). Display-only: it reads this module's state and never
  // starts, stops or requests anything. "live" needs a decoded frame that is still fresh.
  const lens = window.HomebodyScene?.mount(byId("camera-lens"), { variant: "glyph", label: "Camera" }) || null;
  let lensStatus = "Off";
  function lensPhase() {
    const connecting = state.requested || state.session || state.stream;
    if (state.stream && lensStatus.startsWith("Live") && videoFresh()) return "live";
    if (state.stream && state.frameCount > 0) return "held";
    if (connecting) return lensStatus === "Paused" ? "paused" : "connecting";
    if (["Error", "Offline", "Unsupported"].includes(lensStatus)) return "trouble";
    if (lensStatus === "Blocked" || !state.enabled || ["meeting", "sleep"].includes(state.powerMode)) return "privacy";
    return lensStatus === "Ready" ? "ready" : "off";
  }
  function renderLens() {
    const offline = window.HomebodyOwner ? window.HomebodyOwner.isOwner() !== true : false;
    const phase = offline ? "offline" : lensPhase();
    byId("camera-viewer").dataset.lens = phase;
    lens?.update(window.HomebodyScene.lensView({ phase, offline }));
  }

  function setUi(status, message, kind = "") {
    lensStatus = String(status);
    byId("camera-live-status").textContent = status;
    const messageElement = byId("camera-message");
    messageElement.textContent = message;
    messageElement.className = `message ${kind}`;
    if (kind === "error") {
      if (window.HomebodyNotifications?.show(message, { id: "camera-stream", kind, title: "Camera" })) {
        messageElement.className += " message-surfaced";
      }
    }
    else if (kind === "ok") window.HomebodyNotifications?.dismiss("camera-stream");
    renderLens();
  }

  function setControlStatus(message) {
    byId("camera-control-status").textContent = message;
  }

  function updateVisionTimecode() {
    const element = byId("camera-vision-timecode");
    if (!element || state.visionStartedAt === null) return;
    const elapsedSeconds = Math.max(0, Math.floor((performance.now() - state.visionStartedAt) / 1000));
    const hours = String(Math.floor(elapsedSeconds / 3600)).padStart(2, "0");
    const minutes = String(Math.floor((elapsedSeconds % 3600) / 60)).padStart(2, "0");
    const seconds = String(elapsedSeconds % 60).padStart(2, "0");
    element.textContent = `${hours}:${minutes}:${seconds}`;
  }

  function stopVisionOverlay() {
    if (state.visionTimer !== null) window.clearInterval(state.visionTimer);
    state.visionTimer = null;
    state.visionStartedAt = null;
    const element = byId("camera-vision-timecode");
    if (element) element.textContent = "00:00:00";
  }

  function startVisionOverlay() {
    stopVisionOverlay();
    state.visionStartedAt = performance.now();
    updateVisionTimecode();
    state.visionTimer = window.setInterval(updateVisionTimecode, 250);
  }

  const FRAME_STALE_MS = 1500;
  function videoFresh() {
    return state.lastFrameAt !== null && performance.now() - state.lastFrameAt < FRAME_STALE_MS;
  }

  function noteFrame() {
    state.lastFrameAt = performance.now();
    state.frameCount += 1;
    if (state.transport === "WebRTC" && !state.directDecoded) {
      state.directDecoded = true;
      if (state.connectionTimer !== null) window.clearTimeout(state.connectionTimer);
      state.connectionTimer = null;
      setUi("Live", "Private direct video connected. No frames are sent to Hermes or OpenAI.", "ok");
      updateButtons();
    }
  }

  function stopFrameHealth() {
    if (state.healthTimer !== null) window.clearInterval(state.healthTimer);
    const video = byId("reachy-camera-video");
    if (state.frameCallback !== null) video.cancelVideoFrameCallback?.(state.frameCallback);
    state.healthTimer = null;
    state.frameCallback = null;
    state.lastFrameAt = null;
    state.frameCount = 0;
    state.directDecoded = false;
    state.statsInFlight = false;
    state.statsLastAt = 0;
    state.transport = "Off";
    byId("camera-diag-transport").textContent = "Off";
    byId("camera-diag-video").textContent = "Not receiving";
    byId("camera-feed-health").textContent = "Camera off · movement requires fresh video";
    byId("camera-feed-health").dataset.stale = "false";
  }

  async function inspectConnection(generation) {
    const peer = state.session?.rtcPeerConnection;
    if (!peer) return;
    byId("camera-diag-ice").textContent = `ICE ${peer.iceConnectionState} · ${peer.connectionState}`;
    if (state.statsInFlight || typeof peer.getStats !== "function") return;
    state.statsInFlight = true;
    try {
      const stats = await peer.getStats();
      if (state.playbackGeneration !== generation || state.session?.rtcPeerConnection !== peer) return;
      recordReportStats(stats, peer);
      stats.forEach(report => {
        if (report.type === "transport" && report.dtlsState) byId("camera-diag-ice").textContent = `ICE ${peer.iceConnectionState} · DTLS ${report.dtlsState}`;
        if (report.type === "candidate-pair" && (report.nominated || report.selected || [...stats.values()].some(t => t.type === "transport" && t.selectedCandidatePairId === report.id))) {
          const local = stats.get(report.localCandidateId);
          const remote = stats.get(report.remoteCandidateId);
          byId("camera-diag-network").textContent = Number.isFinite(report.currentRoundTripTime)
            ? `${Math.round(report.currentRoundTripTime * 1000)} ms · ${local?.candidateType || "unknown"} ↔ ${remote?.candidateType || "unknown"}`
            : `${report.state || "checking"} · ICE replies ${report.responsesReceived ?? 0}/${report.requestsSent ?? 0}`;
        }
        if (report.type === "inbound-rtp" && (report.kind === "video" || report.mediaType === "video")) {
          const codec = stats.get(report.codecId);
          const jitter = report.jitterBufferEmittedCount > 0
            ? Math.round(report.jitterBufferDelay / report.jitterBufferEmittedCount * 1000) : null;
          byId("camera-diag-video").textContent = `${report.framesPerSecond ?? "—"} fps · ${codec?.mimeType || "video"}${jitter !== null ? ` · buffer ${jitter} ms` : ""} · ${report.framesDecoded ?? 0} decoded · ${report.packetsReceived ?? 0} packets · ${report.freezeCount ?? 0} freezes`;
        }
      });
    } catch { /* Stats are advisory, not a new media session. */ }
    finally { if (state.playbackGeneration === generation) state.statsInFlight = false; }
  }

  function startFrameHealth(transport) {
    if (reportStatsTimer !== null) clearInterval(reportStatsTimer);
    reportStatsTimer = null;
    const generation = state.playbackGeneration;
    state.transport = transport;
    if (transport === "HTTPS backup") byId("camera-diag-ice").textContent += " · fell back to HTTPS";
    byId("camera-diag-transport").textContent = transport;
    const video = byId("reachy-camera-video");
    if (transport === "WebRTC") {
      const receive = () => {
        if (generation !== state.playbackGeneration || !state.stream) return;
        noteFrame();
        state.frameCallback = video.requestVideoFrameCallback(receive);
      };
      if (typeof video.requestVideoFrameCallback === "function") state.frameCallback = video.requestVideoFrameCallback(receive);
      // Older engines expose decoded-frame counters instead of callbacks.
    }
    let previousFrames = video.getVideoPlaybackQuality?.().totalVideoFrames ?? 0;
    let sampleCount = state.frameCount;
    let sampleAt = performance.now();
    state.healthTimer = window.setInterval(() => {
      if (generation !== state.playbackGeneration) return;
      const now = performance.now();
      if (transport === "WebRTC" && typeof video.requestVideoFrameCallback !== "function") {
        const frames = video.getVideoPlaybackQuality?.().totalVideoFrames ?? previousFrames;
        if (frames > previousFrames) noteFrame();
        previousFrames = frames;
      }
      const fresh = videoFresh();
      if (!fresh && (state.controlPointerId !== null || state.cameraControlSession || state.centering)) void endControlGesture("Video delayed · movement held");
      const age = state.lastFrameAt === null ? "waiting" : `${Math.round(now - state.lastFrameAt)} ms since decoded frame`;
      byId("camera-feed-health").textContent = `${transport}${fresh ? " · receiving" : " · video delayed · movement held"} · ${age}`;
      byId("camera-feed-health").dataset.stale = String(!fresh);
      if (now - sampleAt >= 1000) {
        if (transport === "HTTPS backup") byId("camera-diag-video").textContent = `${((state.frameCount - sampleCount) * 1000 / (now - sampleAt)).toFixed(1)} fps · decoded HTTPS frames`;
        sampleCount = state.frameCount;
        sampleAt = now;
        void inspectConnection(generation);
      }
      updateControls();
    }, 250);
  }

  function controlsAllowed() {
    return Boolean(
      state.stream
      && videoFresh()
      && !state.centering && !state.releasing && !state.stopping
      && state.enabled
      && state.controlsEnabled
      && state.powerMode === "awake"
      && state.motorsEnabled
      && (!state.robotBusy || Boolean(state.cameraControlSession))
    );
  }

  function updateControls() {
    const overlay = byId("camera-control-overlay");
    const joystick = byId("camera-joystick");
    const allowed = controlsAllowed();
    // Visibility is not permission: keep the opt-in and runtime gates in controlsAllowed().
    overlay.hidden = !state.stream;
    overlay.dataset.handedness = state.controlsHandedness;
    joystick.setAttribute("aria-disabled", String(!allowed));
    byId("camera-control-center").disabled = !allowed;
    byId("camera-control-center").textContent = state.centering ? "Centering…" : "Center view";
    byId("camera-control-center").setAttribute("aria-busy", String(state.centering));
    byId("camera-control-speed").disabled = !allowed;
    byId("camera-control-stop").textContent = state.stopping ? "Stopping…" : "Stop movement";
    // Stop remains available even if the movement opt-in was just revoked.
    byId("camera-control-stop").disabled = !state.stream;
    byId("camera-control-settings").hidden = state.controlsEnabled;
    if (!state.controlsEnabled) setControlStatus("Enable Camera movement controls in Settings, then Save settings.");
    else if (!state.enabled || !state.stream) setControlStatus("Start the local live camera to move the view");
    else if (state.powerMode !== "awake") setControlStatus("Wake Reachy before moving the camera");
    else if (!state.motorsEnabled) setControlStatus("Movement unavailable: motors are off. Wake Reachy first.");
    else if (state.stopping) setControlStatus("Stopping movement…");
    else if (state.centering) setControlStatus("Centering head and base · Stop remains available");
    else if (state.releasing) setControlStatus("Holding view…");
    else if (!videoFresh()) setControlStatus("Video delayed · movement held. Wait for fresh video.");
    else if (!allowed) setControlStatus("Reachy is busy. Wait or use Stop movement.");
    else if (state.controlPointerId === null) setControlStatus("Release holds the current view");
    renderLens();
  }

  function updateButtons() {
    const active = Boolean(state.requested || state.session || state.stream);
    const allowed = state.enabled && state.powerMode === "awake";
    byId("camera-live-start").disabled = active || !allowed;
    byId("camera-live-start").textContent = active && !state.stream ? "Connecting…" : "Start live camera";
    byId("camera-live-start").setAttribute("aria-busy", String(active && !state.stream));
    byId("camera-live-stop").disabled = !active;
    byId("camera-live-fullscreen").disabled = !state.stream;
    updateControls();
  }

  function resetJoystickVisual() {
    const joystick = byId("camera-joystick");
    joystick.dataset.active = "false";
    byId("camera-joystick-knob").style.transform = "translate(-50%, -50%)";
    state.desiredPan = 0;
    state.desiredTilt = 0;
    state.controlOriginX = null;
    state.controlOriginY = null;
    state.controlRadius = null;
  }

  async function postControl(path, payload, { keepalive = false, headers = {} } = {}) {
    const began = performance.now();
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body: payload === null ? undefined : JSON.stringify(payload),
      keepalive,
      signal: AbortSignal.timeout(path.endsWith("/center") ? 8000 : path === "/api/robot/stop" ? 6000 : 2500),
    });
    let body = {};
    try { body = await response.json(); } catch { /* An empty error body is still an error. */ }
    byId("camera-diag-control").textContent = `${Math.round(performance.now() - began)} ms · ${response.ok ? "acknowledged" : "rejected"}`;
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    return body;
  }

  async function endRemoteControlSession(sessionId, sequence, reason, generation = state.controlGeneration) {
    if (!sessionId) return;
    try {
      await postControl(
        "/api/camera-control/end",
        { session_id: sessionId, sequence },
        { keepalive: true },
      );
      if (generation === state.controlGeneration) setControlStatus(reason || "View held");
    } catch (error) {
      if (generation !== state.controlGeneration) return;
      setControlStatus(`Control release: ${String(error)}`);
      window.HomebodyNotifications?.show(`Camera movement: ${error.message || error}`, { id: "camera-movement", kind: "error" });
    }
  }

  function clearControlTimer() {
    if (state.controlTimer !== null) window.clearInterval(state.controlTimer);
    state.controlTimer = null;
  }

  async function endControlGesture(reason = "View held on release") {
    const generation = ++state.controlGeneration;
    state.centering = false;
    clearControlTimer();
    const joystick = byId("camera-joystick");
    const pointerId = state.controlPointerId;
    state.controlPointerId = null;
    if (pointerId !== null && joystick.hasPointerCapture?.(pointerId)) {
      try { joystick.releasePointerCapture(pointerId); } catch { /* Pointer may already be gone. */ }
    }
    resetJoystickVisual();
    const sessionId = state.cameraControlSession;
    state.cameraControlSession = null;
    state.controlInFlight = false;
    if (sessionId) {
      state.cameraControlSequence += 1;
      const releaseToken = ++state.releaseToken;
      state.releasing = true;
      updateControls();
      try { await endRemoteControlSession(sessionId, state.cameraControlSequence, reason, generation); }
      finally { if (releaseToken === state.releaseToken) { state.releasing = false; updateControls(); } }
    } else {
      setControlStatus(reason);
    }
    if (!controlsAllowed()) updateControls();
  }

  function detachStream() {
    state.playbackGeneration += 1;
    void endControlGesture("Controls stopped with the camera feed");
    const video = byId("reachy-camera-video");
    stopVisionOverlay();
    stopFrameHealth();
    byId("camera-viewer").classList.remove("live");
    video.pause();
    video.srcObject = null;
    if (state.stream) {
      state.stream.getTracks().forEach((track) => track.stop());
      state.stream = null;
    }
    updateControls();
  }

  function cleanupConnection() {
    if (reportStatsTimer !== null) clearInterval(reportStatsTimer);
    reportStatsTimer = null;
    if (state.connectionTimer !== null) window.clearTimeout(state.connectionTimer);
    state.connectionTimer = null;
    state.fallbackAbort?.abort();
    state.fallbackAbort = null;
    detachStream();
    if (state.session) {
      const session = state.session;
      state.session = null; // Invalidate callbacks before close can dispatch events.
      try { session.close(); } catch { /* The signaling session may already be closed. */ }
    }
    if (state.api) {
      try {
        if (state.producersListener) state.api.unregisterProducersListener(state.producersListener);
        if (state.connectionListener) state.api.unregisterConnectionListener(state.connectionListener);
        if (typeof state.api._channel?.close === "function") state.api._channel.close();
      } catch { /* The signaling channel may already be closed. */ }
    }
    state.api = null;
    state.producersListener = null;
    state.connectionListener = null;
    state.producerId = null;
    updateButtons();
  }

  function stop(message = "Live camera stopped.") {
    const wasActive = state.requested || state.session || state.stream;
    state.requested = false;
    cleanupConnection();
    if (wasActive) setUi("Off", message);
  }

  function policyMessage() {
    if (!state.enabled) return "Enable Local live camera in Settings first.";
    if (state.powerMode === "meeting" || state.powerMode === "sleep") return `Camera viewing is blocked in ${state.powerMode}.`;
    if (state.powerMode !== "awake") return "Wake Reachy before starting the live camera.";
    return "Press Start live camera to connect directly to Reachy's local WebRTC feed.";
  }

  function setPolicy({ enabled, controlsEnabled = false, handedness = "right", powerMode, motorsEnabled = false, robotBusy = false }) {
    const wasAllowed = controlsAllowed();
    state.enabled = Boolean(enabled);
    state.controlsEnabled = Boolean(controlsEnabled);
    state.controlsHandedness = handedness === "left" ? "left" : "right";
    state.powerMode = powerMode || "unknown";
    state.motorsEnabled = Boolean(motorsEnabled);
    state.robotBusy = Boolean(robotBusy);
    if ((wasAllowed && !controlsAllowed()) || ((state.controlPointerId !== null || state.cameraControlSession || state.centering)
      && (!state.enabled || !state.controlsEnabled || state.powerMode !== "awake" || !state.motorsEnabled))) {
      void endControlGesture("Controls stopped by robot policy");
    }
    if (!state.enabled || state.powerMode !== "awake") {
      if (state.requested || state.session || state.stream) stop(policyMessage());
      else setUi("Off", policyMessage());
    } else if (!state.requested && !state.session && !state.stream) {
      setUi("Ready", policyMessage());
    }
    updateButtons();
  }

  function isReachyCameraProducer(producer) {
    const name = String(producer?.meta?.name || "").trim().toLowerCase();
    return name === "reachymini" || name === "reachy-mini-camera";
  }

  function attachSession(api, producer) {
    if (!state.requested || state.api !== api || state.session || !isReachyCameraProducer(producer)) return;
    const session = api.createConsumerSession(producer.id);
    if (!session) return;
    state.session = session;
    state.producerId = producer.id;
    session.addEventListener("error", (event) => {
      if (state.session !== session) return;
      reportFailure("direct session error");
      void startHttpsFallback(event?.message || "WebRTC connection failed");
    });
    session.addEventListener("closed", () => {
      if (state.session !== session) return;
      if (state.requested) { reportFailure("direct session closed"); void startHttpsFallback("Direct camera connection closed"); }
    });
    session.addEventListener("streamsChanged", () => {
      if (!state.requested || state.session !== session || !session.streams?.length) return;
      const stream = session.streams[0];
      // GStreamer may announce the same stream more than once. Reassignment
      // interrupts the existing play request in Chromium.
      if (state.stream === stream) return;
      if (state.stream) detachStream();
      const generation = ++state.playbackGeneration;
      const isCurrent = () => state.requested && state.session === session
        && state.stream === stream && state.playbackGeneration === generation;
      stream.getAudioTracks().forEach((track) => { track.enabled = false; });
      state.stream = stream;
      const video = byId("reachy-camera-video");
      byId("camera-viewer").classList.add("live");
      startVisionOverlay();
      video.srcObject = stream;
      updateButtons(); // Stop must remain available while play is pending.
      video.play().then(() => {
        if (!isCurrent()) return;
        startFrameHealth("WebRTC");
        setUi("Connecting", "Video track received; waiting for a decoded frame…");
        updateButtons();
      }).catch((error) => {
        // Stop/privacy/replacement can intentionally abort a pending play.
        // Never let its late result overwrite the current connection or policy.
        if (!isCurrent()) return;
        const reason = error?.name === "AbortError" ? "interrupted" : "blocked";
        setUi("Paused", `Camera connected, but playback was ${reason}: ${String(error)}`, "error");
      });
    });
    session.addEventListener("rtcPeerConnectionChanged", () => {
      if (state.session !== session || !session.rtcPeerConnection) return;
      const peer = session.rtcPeerConnection;
      const receive = () => {
        if (state.session !== session) return;
        reportEvent("peer state", peer);
        byId("camera-diag-ice").textContent = `ICE ${peer.iceConnectionState} · ${peer.connectionState}`;
        void inspectConnection(state.playbackGeneration);
      };
      peer.addEventListener("iceconnectionstatechange", receive);
      peer.addEventListener("connectionstatechange", receive);
      peer.addEventListener("icegatheringstatechange", receive);
      if (reportStatsTimer !== null) clearInterval(reportStatsTimer);
      if (connectionReport && state.healthTimer === null) reportStatsTimer = setInterval(() => { void inspectConnection(state.playbackGeneration); }, 500);
      peer.addEventListener("icecandidateerror", (event) => {
        if (state.session === session) reportEvent(`ICE candidate error ${Number.isInteger(event.errorCode) ? event.errorCode : "unknown"}`, peer);
        if (state.session === session) byId("camera-diag-network").textContent = `ICE candidate error ${event.errorCode || "unknown"}`;
      });
      for (const receiver of peer.getReceivers()) {
        if (receiver.track?.kind === "video" && "jitterBufferTarget" in receiver) {
          try { receiver.jitterBufferTarget = 50; } catch { /* Optional browser hint. */ }
        }
      }
      receive();
    });
    session.connect();
  }

  async function startHttpsFallback(reason) {
    if (!state.requested || state.fallbackAbort) return;
    cleanupConnection();
    if (!state.requested || !state.enabled || state.powerMode !== "awake") return;
    const controller = new AbortController();
    state.fallbackAbort = controller;
    const current = () => state.requested && state.fallbackAbort === controller && !controller.signal.aborted;
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d");
    let stream = null;
    let misses = 0;
    setUi("Connecting", `${reason}. Trying private HTTPS video…`);
    try {
      while (current()) {
        const response = await window.fetch("/api/camera/frame", {
          method: "POST", cache: "no-store", signal: AbortSignal.any([controller.signal, AbortSignal.timeout(3000)]),
        });
        if (!current()) break;
        if ([429, 503].includes(response.status) && misses++ < 12) {
          await new Promise(resolve => window.setTimeout(resolve, 250));
          continue;
        }
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.detail || `Camera HTTP ${response.status}`);
        }
        misses = 0;
        const bitmap = await createImageBitmap(await response.blob());
        if (!current()) { bitmap.close(); break; }
        if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
          canvas.width = bitmap.width;
          canvas.height = bitmap.height;
        }
        context.drawImage(bitmap, 0, 0);
        noteFrame();
        bitmap.close();
        if (!stream) {
          stream = canvas.captureStream(0);
          state.stream = stream;
          const video = byId("reachy-camera-video");
          video.srcObject = stream;
          stream.getVideoTracks()[0].requestFrame();
          await video.play();
          if (!current()) break;
          byId("camera-viewer").classList.add("live");
          startVisionOverlay();
          state.movementSpeed = 0.3;
          byId("camera-control-speed").value = "0.3";
          startFrameHealth("HTTPS backup");
          setUi("Live · HTTPS", "Private HTTPS camera connected (reduced frame rate). No frames are sent to Hermes or OpenAI.", "ok");
          updateButtons();
        } else {
          stream.getVideoTracks()[0].requestFrame();
        }
        await new Promise(resolve => window.setTimeout(resolve, 100));
      }
    } catch (error) {
      if (current()) {
        state.requested = false;
        cleanupConnection();
        setUi("Error", `Camera connection failed: ${String(error.message || error)}`, "error");
      }
    } finally {
      if (!current()) stream?.getTracks().forEach(track => track.stop());
    }
  }

  function start() {
    if (!state.enabled || state.powerMode !== "awake") {
      setUi("Blocked", policyMessage(), "error");
      updateButtons();
      return;
    }
    if (typeof window.RTCPeerConnection !== "function" || !window.GstWebRTCAPI) {
      setUi("Unsupported", "This browser does not provide the required WebRTC camera support.", "error");
      return;
    }
    cleanupConnection();
    resetConnectionReport();
    reportEvent("start");
    state.requested = true;
    setUi("Connecting", "Connecting directly to Reachy's local camera…");
    updateButtons();
    state.connectionTimer = window.setTimeout(() => {
      if (state.requested) { reportFailure("no decoded video within 15 seconds"); void startHttpsFallback("Direct video did not arrive"); }
    }, 15000);
    try {
      const signalingScheme = window.location.protocol === "https:" ? "wss" : "ws";
      const api = new window.GstWebRTCAPI({
        signalingServerUrl: `${signalingScheme}://${window.location.host}/api/camera/signaling`,
        reconnectionTimeout: 0,
        meta: { name: "homebody-ui" },
        webrtcConfig: { iceServers: [] },
      });
      state.api = api;
      state.connectionListener = {
        connected: () => {
          if (state.api === api && state.requested && !state.stream) {
            setUi("Connecting", "Camera signaling connected; waiting for video…");
          }
        },
        disconnected: () => {
          if (!state.requested || state.api !== api) return;
          reportFailure("signaling disconnected");
          void startHttpsFallback("Camera signaling disconnected");
        },
      };
      state.producersListener = {
        producerAdded: (producer) => { if (isReachyCameraProducer(producer)) attachSession(api, producer); },
        producerRemoved: (producer) => {
          if (!state.requested || state.api !== api || producer?.id !== state.producerId) return;
          state.requested = false;
          cleanupConnection();
          setUi("Offline", "Reachy's camera producer stopped.", "error");
        },
      };
      api.registerConnectionListener(state.connectionListener);
      api.registerProducersListener(state.producersListener);
    } catch (error) {
      state.requested = false;
      cleanupConnection();
      setUi("Error", `Could not connect to Reachy's camera: ${String(error)}`, "error");
    }
  }

  function pointerVector(event) {
    const joystick = byId("camera-joystick");
    const rect = joystick.getBoundingClientRect();
    const radius = state.controlRadius ?? Math.min(rect.width, rect.height) * 0.36;
    const originX = state.controlOriginX ?? rect.left + rect.width / 2;
    const originY = state.controlOriginY ?? rect.top + rect.height / 2;
    const rawX = (event.clientX - originX) / radius;
    const rawY = (event.clientY - originY) / radius;
    const rawMagnitude = Math.hypot(rawX, rawY);
    const boundedMagnitude = Math.min(1, rawMagnitude);
    const unitX = rawMagnitude ? rawX / rawMagnitude : 0;
    const unitY = rawMagnitude ? rawY / rawMagnitude : 0;
    const scaledMagnitude = boundedMagnitude <= CAMERA_CONTROL_DEAD_ZONE
      ? 0
      : (boundedMagnitude - CAMERA_CONTROL_DEAD_ZONE) / (1 - CAMERA_CONTROL_DEAD_ZONE);
    const visualX = unitX * boundedMagnitude * radius;
    const visualY = unitY * boundedMagnitude * radius;
    byId("camera-joystick-knob").style.transform = `translate(calc(-50% + ${visualX}px), calc(-50% + ${visualY}px))`;
    return { pan: unitX * scaledMagnitude, tilt: -unitY * scaledMagnitude };
  }

  async function sendControlCommand() {
    if (!videoFresh()) { void endControlGesture("Video delayed · movement held"); return; }
    if (!state.cameraControlSession || state.controlPointerId === null || state.controlInFlight) return;
    const generation = state.controlGeneration;
    state.controlInFlight = true;
    state.cameraControlSequence += 1;
    const sessionId = state.cameraControlSession;
    const sequence = state.cameraControlSequence;
    try {
      await postControl("/api/camera-control/move", {
        session_id: sessionId,
        sequence,
        pan: state.desiredPan * state.movementSpeed,
        tilt: state.desiredTilt * state.movementSpeed,
      });
      if (generation !== state.controlGeneration || state.cameraControlSession !== sessionId) return;
      window.HomebodyNotifications?.dismiss("camera-movement");
      setControlStatus("Moving · release to hold");
    } catch (error) {
      if (generation !== state.controlGeneration || state.cameraControlSession !== sessionId) return;
      const message = String(error);
      if (!message.includes("Robot is busy")) {
        setControlStatus(message);
        window.HomebodyNotifications?.show(`Camera movement: ${message}`, { id: "camera-movement", kind: "error" });
        await endControlGesture("Control stopped after rejection");
      }
    } finally {
      if (generation === state.controlGeneration && state.cameraControlSession === sessionId) state.controlInFlight = false;
    }
  }

  async function beginPointerControl(event) {
    if (!controlsAllowed() || state.controlPointerId !== null) return;
    event.preventDefault();
    const joystick = byId("camera-joystick");
    state.controlPointerId = event.pointerId;
    state.controlGeneration += 1;
    const generation = state.controlGeneration;
    const rect = joystick.getBoundingClientRect();
    state.controlOriginX = rect.left + rect.width / 2;
    state.controlOriginY = rect.top + rect.height / 2;
    state.controlRadius = Math.min(rect.width, rect.height) * 0.36;
    joystick.dataset.active = "true";
    joystick.setPointerCapture(event.pointerId);
    const vector = pointerVector(event);
    state.desiredPan = vector.pan;
    state.desiredTilt = vector.tilt;
    setControlStatus("Authorizing movement…");
    try {
      const body = await postControl("/api/camera-control/session", null, {
        headers: { "X-Reachy-Adult-UI": "unlocked" },
      });
      const sessionId = String(body.session_id || "");
      if (generation !== state.controlGeneration || state.controlPointerId === null) {
        await endRemoteControlSession(sessionId, 1, "Gesture ended before authorization");
        return;
      }
      state.cameraControlSession = sessionId;
      state.cameraControlSequence = 0;
      await sendControlCommand();
      if (generation !== state.controlGeneration || state.controlPointerId === null) return;
      state.controlTimer = window.setInterval(() => { void sendControlCommand(); }, CAMERA_CONTROL_INTERVAL_MS);
    } catch (error) {
      if (generation !== state.controlGeneration) return;
      setControlStatus(`Blocked: ${String(error)}`);
      window.HomebodyNotifications?.show(`Camera movement: ${error.message || error}`, { id: "camera-movement", kind: "error" });
      await endControlGesture("Movement blocked");
    }
  }

  function movePointerControl(event) {
    if (event.pointerId !== state.controlPointerId) return;
    event.preventDefault();
    const vector = pointerVector(event);
    state.desiredPan = vector.pan;
    state.desiredTilt = vector.tilt;
    void sendControlCommand();
  }

  async function centerCamera() {
    if (!controlsAllowed()) return;
    await endControlGesture("Preparing Center");
    if (!controlsAllowed()) return;
    const generation = ++state.controlGeneration;
    state.centering = true;
    updateControls();
    try {
      const started = await postControl("/api/camera-control/session", null, {
        headers: { "X-Reachy-Adult-UI": "unlocked" },
      });
      const sessionId = String(started.session_id || "");
      if (generation !== state.controlGeneration || !state.centering || !videoFresh()) {
        void endRemoteControlSession(sessionId, 1, "Center cancelled", generation);
        return;
      }
      state.cameraControlSession = sessionId;
      state.cameraControlSequence = 1;
      await postControl("/api/camera-control/center", { session_id: sessionId, sequence: 1 });
      if (generation !== state.controlGeneration || state.cameraControlSession !== sessionId) return;
      await endControlGesture("Camera centered");
    } catch (error) {
      if (generation !== state.controlGeneration) return;
      window.HomebodyNotifications?.show(`Center: ${error.message || error}`, { id: "camera-movement", kind: "error" });
      await endControlGesture(`Center stopped: ${error.message || error}`);
    } finally {
      if (generation === state.controlGeneration) { state.centering = false; updateControls(); }
    }
  }

  async function keyboardMove(pan, tilt) {
    if (!controlsAllowed() || state.controlPointerId !== null) return;
    state.controlPointerId = -1;
    state.controlGeneration += 1;
    const generation = state.controlGeneration;
    state.desiredPan = pan;
    state.desiredTilt = tilt;
    byId("camera-joystick").dataset.active = "true";
    try {
      const started = await postControl("/api/camera-control/session", null, {
        headers: { "X-Reachy-Adult-UI": "unlocked" },
      });
      if (generation !== state.controlGeneration) {
        await endRemoteControlSession(String(started.session_id || ""), 1, "Keyboard command cancelled");
        return;
      }
      state.cameraControlSession = String(started.session_id || "");
      state.cameraControlSequence = 0;
      await sendControlCommand();
      if (generation !== state.controlGeneration) return;
      window.setTimeout(() => { if (generation === state.controlGeneration) void endControlGesture("Keyboard movement complete"); }, 200);
    } catch (error) {
      if (generation !== state.controlGeneration) return;
      setControlStatus(`Keyboard movement blocked: ${String(error)}`);
      window.HomebodyNotifications?.show(`Camera movement: ${error.message || error}`, { id: "camera-movement", kind: "error" });
      await endControlGesture("Keyboard movement blocked");
    }
  }

  async function emergencyStop() {
    if (state.stopping) return;
    // Do not let a lost /end response delay the independent emergency Stop.
    void endControlGesture("Stop requested");
    state.stopping = true;
    updateControls();
    try {
      await postControl("/api/robot/stop", null);
      setControlStatus("Movement stopped");
    } catch (error) {
      setControlStatus(`Stop error: ${String(error)}`);
      window.HomebodyNotifications?.show(`Camera movement: ${error.message || error}`, { id: "camera-movement", kind: "error" });
    } finally {
      state.stopping = false;
      updateControls();
    }
  }

  function setAppFullscreen(enabled) {
    const viewer = byId("camera-viewer");
    if (enabled === state.appFullscreen) return;
    if (enabled) {
      const anchor = document.createComment("camera-viewer-home");
      viewer.parentNode.insertBefore(anchor, viewer);
      state.appFullscreenAnchor = anchor;
      document.body.appendChild(viewer);
      viewer.classList.add("camera-app-fullscreen");
      document.body.classList.add("camera-app-fullscreen-active");
      byId("camera-live-fullscreen").textContent = "Exit fullscreen";
      state.appFullscreen = true;
      return;
    }
    viewer.classList.remove("camera-app-fullscreen");
    document.body.classList.remove("camera-app-fullscreen-active");
    const anchor = state.appFullscreenAnchor;
    if (anchor?.parentNode) {
      anchor.parentNode.insertBefore(viewer, anchor);
      anchor.remove();
    }
    state.appFullscreenAnchor = null;
    state.appFullscreen = false;
    byId("camera-live-fullscreen").textContent = "Fullscreen";
  }

  async function openControlSettings() {
    // Navigate to the existing opt-in; never enable movement as a side effect of viewing.
    void endControlGesture("Controls stopped for Settings");
    try {
      if (state.appFullscreen) setAppFullscreen(false);
      if (document.fullscreenElement) await document.exitFullscreen();
    } catch (error) {
      setControlStatus(`Exit fullscreen before opening Settings: ${String(error)}`);
      return;
    }
    const tab = document.querySelector('[data-tab="settings"]');
    if (!tab || tab.hidden) return;
    tab.click();
    // Settings navigation only: selects Privacy & access and focuses the opt-in without changing it.
    window.HomebodySettings?.reveal("camera_controls_enabled");
  }

  async function fullscreen() {
    void endControlGesture("Gesture cancelled for fullscreen change");
    const viewer = byId("camera-viewer");
    try {
      if (state.appFullscreen) {
        setAppFullscreen(false);
      } else if (document.fullscreenElement) {
        await document.exitFullscreen();
      } else if (typeof viewer.requestFullscreen === "function") {
        try {
          await viewer.requestFullscreen();
        } catch (_error) {
          setAppFullscreen(true);
        }
      } else {
        setAppFullscreen(true);
      }
    } catch (error) {
      setUi("Live", `Fullscreen could not open: ${String(error)}`, "error");
    }
  }

  const joystick = byId("camera-joystick");
  joystick.addEventListener("pointerdown", (event) => { void beginPointerControl(event); });
  joystick.addEventListener("pointermove", movePointerControl);
  for (const eventName of ["pointerup", "pointercancel", "lostpointercapture"]) {
    joystick.addEventListener(eventName, (event) => {
      if (event.pointerId === state.controlPointerId) void endControlGesture("View held on release");
    });
  }
  joystick.addEventListener("keydown", (event) => {
    const vectors = { ArrowLeft: [-0.55, 0], ArrowRight: [0.55, 0], ArrowUp: [0, 0.55], ArrowDown: [0, -0.55] };
    const vector = vectors[event.key];
    if (!vector || !controlsAllowed()) return;
    event.preventDefault();
    void keyboardMove(vector[0], vector[1]);
  });

  byId("camera-copy-report")?.addEventListener("click", () => { void copyConnectionReport(); });
  byId("camera-live-start").addEventListener("click", start);
  byId("camera-live-stop").addEventListener("click", () => stop());
  byId("camera-live-fullscreen").addEventListener("click", () => { void fullscreen(); });
  byId("camera-control-fullscreen-exit").addEventListener("click", () => { void fullscreen(); });
  byId("camera-control-settings").addEventListener("click", () => { void openControlSettings(); });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.appFullscreen) {
      void endControlGesture("Controls stopped for fullscreen exit");
      setAppFullscreen(false);
    }
  });
  byId("camera-control-speed").addEventListener("change", (event) => {
    const speed = Number(event.target.value);
    state.movementSpeed = [0.3, 0.6, 1].includes(speed) ? speed : 0.3;
    void endControlGesture("Speed changed · drag again to move");
  });
  byId("camera-control-center").addEventListener("click", () => { void centerCamera(); });
  byId("camera-control-stop").addEventListener("click", () => { void emergencyStop(); });
  window.addEventListener("pagehide", () => stop("Camera stopped because the page closed."));
  window.addEventListener("blur", () => { void endControlGesture("Controls stopped when the window lost focus"); });
  window.addEventListener("orientationchange", () => { void endControlGesture("Controls stopped for orientation change"); });
  document.addEventListener("fullscreenchange", () => { void endControlGesture("Controls stopped for fullscreen change"); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop("Camera stopped while the app was in the background.");
  });

  window.ReachyCamera = {
    setPolicy,
    stop,
    isActive: () => Boolean(state.requested || state.session || state.stream),
  };
  setUi("Off", "Reading camera policy…");
  resetJoystickVisual();
  updateButtons();
})();
