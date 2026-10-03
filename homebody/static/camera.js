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
    appFullscreen: false,
    appFullscreenAnchor: null,
  };

  function setUi(status, message, kind = "") {
    byId("camera-live-status").textContent = status;
    const messageElement = byId("camera-message");
    messageElement.textContent = message;
    messageElement.className = `message ${kind}`;
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

  function controlsAllowed() {
    return Boolean(
      state.stream
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
    overlay.hidden = !(state.stream && state.controlsEnabled);
    overlay.dataset.handedness = state.controlsHandedness;
    joystick.setAttribute("aria-disabled", String(!allowed));
    byId("camera-control-center").disabled = !allowed;
    byId("camera-control-stop").disabled = !(state.stream && state.controlsEnabled);
    if (!overlay.hidden && !allowed) setControlStatus("Movement unavailable until Awake and idle");
    else if (!state.controlPointerId) setControlStatus("Release holds the current view");
  }

  function updateButtons() {
    const active = Boolean(state.session || state.stream);
    const allowed = state.enabled && state.powerMode === "awake";
    byId("camera-live-start").disabled = active || !allowed;
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
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body: payload === null ? undefined : JSON.stringify(payload),
      keepalive,
    });
    let body = {};
    try { body = await response.json(); } catch { /* An empty error body is still an error. */ }
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    return body;
  }

  async function endRemoteControlSession(sessionId, sequence, reason) {
    if (!sessionId) return;
    try {
      await postControl(
        "/api/camera-control/end",
        { session_id: sessionId, sequence },
        { keepalive: true },
      );
      setControlStatus(reason || "View held");
    } catch (error) {
      setControlStatus(`Control release: ${String(error)}`);
    }
  }

  function clearControlTimer() {
    if (state.controlTimer !== null) window.clearInterval(state.controlTimer);
    state.controlTimer = null;
  }

  async function endControlGesture(reason = "View held on release") {
    state.controlGeneration += 1;
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
      await endRemoteControlSession(sessionId, state.cameraControlSequence, reason);
    } else {
      setControlStatus(reason);
    }
  }

  function detachStream() {
    void endControlGesture("Controls stopped with the camera feed");
    const video = byId("reachy-camera-video");
    stopVisionOverlay();
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
    detachStream();
    if (state.session) {
      try { state.session.close(); } catch { /* The signaling session may already be closed. */ }
      state.session = null;
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
    if (wasAllowed && !controlsAllowed()) void endControlGesture("Controls stopped by robot policy");
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
    if (!state.requested || state.session || !isReachyCameraProducer(producer)) return;
    const session = api.createConsumerSession(producer.id);
    if (!session) return;
    state.session = session;
    state.producerId = producer.id;
    session.addEventListener("error", (event) => {
      const message = event?.message || "WebRTC camera stream failed";
      state.requested = false;
      cleanupConnection();
      setUi("Error", message, "error");
    });
    session.addEventListener("closed", () => {
      const requested = state.requested;
      state.session = null;
      detachStream();
      updateButtons();
      if (requested) setUi("Offline", "Camera connection closed. Press Start to reconnect.", "error");
    });
    session.addEventListener("streamsChanged", () => {
      if (!state.requested || !session.streams?.length) return;
      const stream = session.streams[0];
      stream.getAudioTracks().forEach((track) => { track.enabled = false; });
      state.stream = stream;
      const video = byId("reachy-camera-video");
      byId("camera-viewer").classList.add("live");
      startVisionOverlay();
      video.srcObject = stream;
      video.play().then(() => {
        setUi("Live", "Local camera connected. No frames are sent to Hermes or OpenAI.", "ok");
        updateButtons();
      }).catch((error) => setUi("Paused", `Camera connected, but playback was blocked: ${String(error)}`, "error"));
    });
    session.connect();
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
    state.requested = true;
    setUi("Connecting", "Connecting directly to Reachy's local camera…");
    updateButtons();
    try {
      const signalingScheme = window.location.protocol === "https:" ? "wss" : "ws";
      const api = new window.GstWebRTCAPI({
        signalingServerUrl: `${signalingScheme}://${window.location.hostname}:8443`,
        reconnectionTimeout: 0,
        meta: { name: "reachy-hermes-ui" },
        webrtcConfig: { iceServers: [] },
      });
      state.api = api;
      state.connectionListener = {
        connected: () => setUi("Connecting", "Camera signaling connected; waiting for video…"),
        disconnected: () => {
          if (!state.requested) return;
          state.requested = false;
          cleanupConnection();
          setUi("Offline", "Camera signaling disconnected. Press Start to reconnect.", "error");
        },
      };
      state.producersListener = {
        producerAdded: (producer) => { if (isReachyCameraProducer(producer)) attachSession(api, producer); },
        producerRemoved: (producer) => {
          if (!state.requested || producer?.id !== state.producerId) return;
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
    return { pan: unitX * scaledMagnitude, tilt: unitY * scaledMagnitude };
  }

  async function sendControlCommand() {
    if (!state.cameraControlSession || state.controlPointerId === null || state.controlInFlight) return;
    state.controlInFlight = true;
    state.cameraControlSequence += 1;
    const sessionId = state.cameraControlSession;
    const sequence = state.cameraControlSequence;
    try {
      await postControl("/api/camera-control/move", {
        session_id: sessionId,
        sequence,
        pan: state.desiredPan,
        tilt: state.desiredTilt,
      });
      setControlStatus("Moving · release to hold");
    } catch (error) {
      const message = String(error);
      if (!message.includes("Robot is busy")) {
        setControlStatus(message);
        await endControlGesture("Control stopped after rejection");
      }
    } finally {
      state.controlInFlight = false;
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
      state.controlTimer = window.setInterval(() => { void sendControlCommand(); }, CAMERA_CONTROL_INTERVAL_MS);
    } catch (error) {
      setControlStatus(`Blocked: ${String(error)}`);
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
    await endControlGesture("Preparing explicit Center");
    try {
      const started = await fetch("/api/camera-control/session", {
        method: "POST",
        headers: { "X-Reachy-Adult-UI": "unlocked" },
      });
      const startedBody = await started.json();
      if (!started.ok) throw new Error(startedBody.detail || `HTTP ${started.status}`);
      state.cameraControlSession = String(startedBody.session_id || "");
      state.cameraControlSequence = 1;
      setControlStatus("Centering head and base…");
      await postControl("/api/camera-control/center", {
        session_id: state.cameraControlSession,
        sequence: state.cameraControlSequence,
      });
      await endControlGesture("Camera centered");
    } catch (error) {
      setControlStatus(`Center blocked: ${String(error)}`);
      await endControlGesture("Center stopped");
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
      window.setTimeout(() => { void endControlGesture("Keyboard movement complete"); }, 400);
    } catch (error) {
      setControlStatus(`Keyboard movement blocked: ${String(error)}`);
      await endControlGesture("Keyboard movement blocked");
    }
  }

  async function emergencyStop() {
    await endControlGesture("Emergency Stop requested");
    try {
      await postControl("/api/robot/stop", null);
      setControlStatus("Movement stopped");
    } catch (error) {
      setControlStatus(`Stop error: ${String(error)}`);
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

  async function fullscreen() {
    await endControlGesture("Gesture cancelled for fullscreen change");
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
    const vectors = { ArrowLeft: [-0.55, 0], ArrowRight: [0.55, 0], ArrowUp: [0, -0.55], ArrowDown: [0, 0.55] };
    const vector = vectors[event.key];
    if (!vector || !controlsAllowed()) return;
    event.preventDefault();
    void keyboardMove(vector[0], vector[1]);
  });

  byId("camera-live-start").addEventListener("click", start);
  byId("camera-live-stop").addEventListener("click", () => stop());
  byId("camera-live-fullscreen").addEventListener("click", () => { void fullscreen(); });
  byId("camera-control-fullscreen-exit").addEventListener("click", () => { void fullscreen(); });
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
