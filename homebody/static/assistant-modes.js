/* Everyday/Workspace surface: disclosure, Stop speaking and a status-only project-work badge.
   Late replies cannot repaint after owner loss, Kids Mode or going offline (generation tokens). */
(() => {
  const $ = id => document.getElementById(id);
  let generation = 0, ownerAllowed = false, blocked = true, capabilities = null;
  let lastCapabilitiesAt = 0, lastBadgeAt = 0, stopping = false;
  let voiceConfig = {};
  let capabilitiesSequence = 0, cameraDeadline = 0, cameraTimer = null;
  const CAPABILITY_FIELDS = ['everyday-summary', 'everyday-camera-activity', 'everyday-robot-permissions',
    'everyday-camera-permission', 'everyday-household', 'everyday-information', 'everyday-search'];
  // Only a status reply names the mode; pending switches, offline and owner loss never claim one.
  let confirmedMode = '', switchingTo = '', kidsHidden = false, unknownReason = 'Waiting for Reachy to confirm.';
  // 'unknown' = no answer yet, 'uncertain' = the status check failed; both keep the global Stop reachable.
  let badgeState = 'unknown', voiceState = '', badgeSequence = 0;
  let badgeWorkUnresolved = false;
  const text = (id, value) => { if ($(id).textContent !== value) $(id).textContent = value; };
  const MODE_LABELS = { everyday: 'Everyday', workspace: 'Workspace' };

  function currentMode() {
    return kidsHidden ? 'unknown' : switchingTo ? 'pending' : confirmedMode || 'unknown';
  }

  function renderMode() {
    const mode = currentMode();
    if ($('panel-agent').dataset.assistantMode === 'workspace' && mode !== 'workspace') {
      window.invalidateAgentPolls?.();
    }
    $('panel-agent').dataset.assistantMode = mode;
    $('panel-agent').dataset.projects = String(mode === 'workspace');
    $('assistant-mode-state').dataset.mode = mode;
    document.querySelector('.assistant-mode-switch').dataset.confirmed = String(mode === 'everyday' || mode === 'workspace');
    text('assistant-mode-current', mode === 'pending' ? `Switching to ${MODE_LABELS[switchingTo]}…`
      : MODE_LABELS[mode] || 'Not confirmed');
    text('agent-profile-badge', mode === 'pending' ? 'Switching…' : MODE_LABELS[mode] || 'Mode unknown');
    text('assistant-mode-detail', mode === 'pending' ? 'Not active until Reachy confirms.'
      : mode === 'everyday' ? 'Fast home answers. No agent memory, projects or coding tools.'
        : mode === 'workspace' ? 'Project sessions on your agent host. Review planned coding work before approving it.'
          : unknownReason);
    // Each route's details show only once status confirms it; the Workspace destination offers one way in.
    $('everyday-card').hidden = mode !== 'everyday';
    $('workspace-mode-card').hidden = mode !== 'workspace';
    $('workspace-gate').hidden = mode === 'workspace';
    text('workspace-gate-state', mode === 'pending' ? `Switching to ${MODE_LABELS[switchingTo]}…`
      : mode === 'everyday' ? 'Voice is in Everyday. Workspace connects Reachy to your repositories and coding sessions.'
        : unknownReason);
    $('tab-dashboard').dataset.profileActive = String(mode === 'everyday');
    window.HomebodyWorkspace?.gate(mode);
    renderStop();
    sync();
  }

  /** Global work Stop: hidden only when Everyday is confirmed and the badge confirmed no work. */
  function renderStop() {
    $('agent-stop-button').hidden = currentMode() === 'everyday' && badgeState === 'idle';
  }

  /** Repaint the Workspace card from the display and project lanes; display-only, never fetches. */
  function sync() {
    if ($('workspace-mode-card').hidden) return;
    const display = window.HomebodyWorkspace?.state() || { showing: false, view: 'other' };
    const native = window.HomebodyNativeWorkspace?.summary() || { session: 'unchecked', repository: '' };
    text('workspace-mode-display', display.showing ? 'Connected' : 'Not connected');
    text('workspace-mode-repository', native.repository || (native.session === 'bound' ? native.label : 'No project open'));
    $('workspace-mode-card').dataset.session = native.session;
    $('workspace-mode-card').dataset.run = native.run || '';
    text('workspace-mode-session', {
      unchecked: 'Not connected · tap Connect, then open a project',
      checking: 'Checking…',
      unconfirmed: 'Unconfirmed · reconnecting',
      bound: `Open · ${native.label}`,
      other: 'None open for this repository',
      none: 'Not open · choose a repository, then Open project',
    }[native.session] || 'Not checked');
    // Reported runtime state only. Awake is not listening; there is no remote "start listening".
    text('workspace-mode-voice', voiceState === 'listening' ? 'Listening now'
      : ['thinking', 'looking', 'speaking'].includes(voiceState) ? 'In a voice turn'
        : 'Not listening · say “Hey Homebody” to Reachy to talk');
    text('workspace-mode-route', native.session === 'bound'
      ? native.voice === 'native' ? `Your project session · ${native.label}`
        : 'Not sent · voice discussion is off for this session on the agent host'
      : native.session === 'unconfirmed' || native.session === 'checking' ? 'Unconfirmed'
        : 'Not sent to a project · connect, then open a project');
    // The open session's own run is the most specific truth; the status-only badge covers the rest.
    text('workspace-mode-work', native.uncertain ? 'Project work status unconfirmed. Stop project work stays available.'
      : native.session === 'bound' && native.run === 'ready' ? 'Ready to test · fixed checks passed.'
      : native.session === 'bound' && native.run && native.run !== 'running' ? `Last run: ${native.run.replaceAll('_', ' ')}.`
      : badgeState === 'running' ? 'Project work running.'
        : badgeState === 'stopping' ? 'Project work stopping.'
          : badgeState === 'idle' ? 'No approved project run is reported. Native discussion uses its configured tools.'
            : 'Project work status not confirmed.');
  }

  function unconfirmed(reason) {
    confirmedMode = '';
    switchingTo = '';
    text('workspace-mode-repository', 'No project open');
    text('workspace-mode-session', 'Not checked');
    text('workspace-mode-display', 'Not connected');
    text('workspace-mode-route', 'Not checked yet');
    unknownReason = reason;
    renderMode();
  }

  function reset(message) {
    generation += 1;
    capabilities = null; lastCapabilitiesAt = 0; lastBadgeAt = 0;
    clearCapabilityDetails();
    $('everyday-permissions').open = false;
    $('everyday-technical-details').open = false;
    text('everyday-badge', 'Unavailable');
    $('everyday-badge').dataset.status = 'failed';
    voiceConfig = {};
    ['everyday-voice', 'everyday-voice-note', 'everyday-model', 'everyday-weather', 'everyday-time', 'everyday-devices', 'everyday-device-ids'].forEach(id => text(id, '—'));
    text('everyday-message', message);
    $('native-work-badge').hidden = true;
    badgeState = 'unknown';
    badgeWorkUnresolved = false;
    renderStop();
    $('voice-stop-button').disabled = blocked || stopping;
  }

  function renderVoice() {
    const mode = voiceConfig.conversation_mode;
    text('everyday-voice', mode === 'realtime'
      ? `OpenAI Realtime · ${voiceConfig.realtime_model || 'model not reported'}`
      : mode === 'live' ? `OpenAI GPT-Live · ${voiceConfig.live_model || 'model not reported'}`
        : mode === 'pipeline' ? 'Pipeline · speech → text → speech' : 'Not reported by Reachy');
    text('everyday-voice-note', 'Saved selection only · not proof of an active or ready voice session.');
  }

  function clearCapabilityDetails() {
    clearInterval(cameraTimer); cameraTimer = null; cameraDeadline = 0;
    CAPABILITY_FIELDS.forEach(id => text(id, 'Not reported.'));
  }

  const permission = value => value === true ? 'allowed' : value === false ? 'off' : 'not reported';
  const names = value => Array.isArray(value) ? value.filter(item => typeof item === 'string') : [];
  // Only explicit booleans or known status categories establish readiness; unknown versions stay unknown.
  function groupState(group) {
    if (typeof group === 'boolean') return group ? 'available' : 'unavailable';
    if (!group || typeof group !== 'object') return 'not reported';
    for (const key of ['available', 'ready', 'enabled', 'configured']) {
      if (typeof group[key] === 'boolean') return group[key]
        ? key === 'configured' ? 'configured · not verified live' : 'available'
        : key === 'configured' ? 'not configured' : 'unavailable';
    }
    if (['ready', 'available'].includes(group.status)) return 'available';
    if (['unavailable', 'disabled', 'not_configured'].includes(group.status)) return 'unavailable';
    return 'not reported';
  }

  function renderPermissions(body, requestedAt) {
    clearCapabilityDetails();
    const local = body.local || {}, permissions = local.permissions || {};
    const tools = new Set([...names(body.tools), ...names(local.tools)]);
    const groups = body.groups && typeof body.groups === 'object' ? body.groups : {};
    text('everyday-robot-permissions', `Power requests: ${permission(permissions.power)} · movement: ${permission(permissions.movement)} · speaker volume: ${permission(permissions.volume)}.`);
    text('everyday-camera-permission', `Camera access: ${permission(permissions.camera)}. Covers explicit looks and short voice sessions; permission never starts the camera. Viewer and tracking stay separate.`);
    const home = tools.has('home_control') || tools.has('home_status') || names(body.devices?.controls).length || names(body.devices?.status).length;
    const timers = groupState(groups.timers);
    const lookup = groupState(groups.public_lookup ?? groups.search);
    const timersKnown = timers.startsWith('configured') || timers === 'available';
    const lookupKnown = lookup.startsWith('configured') || lookup === 'available';
    const delivery = groups.timers?.scope === 'adult_device_ram'
      ? ' Across voice wakes; host restart clears them. Audible delivery unverified.'
      : groups.timers?.scope === 'conversation_session' ? ' Session-only; audible delivery unverified.' : '';
    text('everyday-household', `${home ? 'Only permitted home devices' : 'No home devices reported'} · timers & reminders: ${timers}.${delivery}`);
    const weather = !!body.weather?.source, clock = body.time_source === 'hermes_host_clock';
    text('everyday-information', `Weather: ${weather ? 'source configured' : 'not configured'} · current time: ${clock ? 'host clock' : 'not reported'}. General explanations use the selected voice route.`);
    text('everyday-search', `Public lookup: ${lookup}${lookupKnown ? ' · public queries leave your home' : lookup === 'not reported' ? ' by the host' : ''}.`);
    const summary = ['General explanations'];
    if (tools.has('get_reachy_status')) summary.push('robot status');
    if (home) summary.push('permitted home devices');
    if (timersKnown) summary.push('timers & reminders');
    if (weather || clock) summary.push(weather && clock ? 'weather & time' : weather ? 'weather' : 'current time');
    if (lookupKnown) summary.push('public lookup');
    text('everyday-summary', `${summary.join(' · ')}. No project tools or agent memory.`);
    const activity = local.activity || {};
    if (activity.camera_session_active === false) {
      text('everyday-camera-activity', 'No temporary camera session reported.');
    } else if (activity.camera_session_active === true) {
      const seconds = activity.camera_remaining_seconds;
      if (typeof seconds === 'number' && Number.isFinite(seconds) && seconds >= 0 && seconds <= 120) {
        // Network delay cannot renew an already-expired reported window.
        cameraDeadline = requestedAt + seconds * 1000;
        const token = generation;
        const tick = () => {
          if (blocked || token !== generation) return;
          const remaining = Math.max(0, Math.ceil((cameraDeadline - performance.now()) / 1000));
          text('everyday-camera-activity', remaining > 0
            ? `Temporary camera active · up to ${remaining}s remaining. Not the dashboard viewer.`
            : 'Temporary camera window expired · awaiting fresh status.');
          if (!remaining) { clearInterval(cameraTimer); cameraTimer = null; }
        };
        tick();
        if (cameraDeadline > performance.now()) cameraTimer = setInterval(tick, 1000);
      } else {
        text('everyday-camera-activity', 'Temporary camera session status unconfirmed · expiry not reported.');
      }
    } else {
      text('everyday-camera-activity', 'Temporary camera session status not reported.');
    }
  }

  function renderCapabilities(body, requestedAt) {
    capabilities = body;
    const provider = body.text_provider;
    text('everyday-model', provider ? `${provider.model || 'Model not reported'} · ${provider.label || 'Provider not reported'} (${provider.region || 'region not reported'})` : 'Not configured');
    const weather = body.weather || {};
    text('everyday-weather', weather.source
      ? `${weather.label || 'Configured weather source'}${weather.location_leaves_home === true ? ' · your configured location is sent to this provider' : weather.location_leaves_home === false ? ' · retrieved through your home system' : ' · location sharing not reported'}`
      : 'No weather source configured · Everyday will say so instead of guessing');
    text('everyday-time', body.time_source === 'hermes_host_clock' ? 'Hermes host clock' : 'Unavailable');
    const controls = names(body.devices?.controls), status = names(body.devices?.status);
    text('everyday-devices', controls.length || status.length
      ? `${controls.length} permitted control${controls.length === 1 ? '' : 's'} · ${status.length} status source${status.length === 1 ? '' : 's'}`
      : 'None shared with Everyday');
    text('everyday-device-ids', `Control: ${controls.join(', ') || 'none'}\nStatus: ${status.join(', ') || 'none'}`);
    renderVoice();
    renderPermissions(body, requestedAt);
    text('everyday-badge', body.available === true ? 'Tools ready' : 'Unavailable');
    $('everyday-badge').dataset.status = body.available === true ? 'completed' : 'failed';
    text('everyday-message', body.available === true
      ? ''
      : `Everyday is unavailable: ${String(body.error || 'not configured on the Hermes host')}`);
  }

  async function getJson(path, token) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await window.fetch(path, { cache: 'no-store', signal: controller.signal });
      const body = await response.json().catch(() => ({}));
      if (token !== generation) return null; // stale: owner/Kids/offline changed meanwhile
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
      return body;
    } finally { clearTimeout(timer); }
  }

  async function refreshCapabilities() {
    const token = generation, sequence = ++capabilitiesSequence, requestedAt = performance.now();
    try {
      const body = await getJson('/api/everyday/capabilities', token);
      if (body && token === generation && sequence === capabilitiesSequence && !blocked) renderCapabilities(body, requestedAt);
    } catch (error) {
      if (token !== generation || sequence !== capabilitiesSequence || blocked) return;
      capabilities = null;
      clearCapabilityDetails();
      ['everyday-model', 'everyday-weather', 'everyday-time', 'everyday-devices', 'everyday-device-ids'].forEach(id => text(id, '—'));
      text('everyday-badge', 'Unavailable');
      $('everyday-badge').dataset.status = 'failed';
      text('everyday-message', `Everyday status is unavailable: ${String(error.message || error)}`);
    }
  }

  async function refreshBadge() {
    const token = generation, sequence = ++badgeSequence;
    try {
      const body = await getJson('/api/agent/workspace/native-badge', token);
      if (!body || token !== generation || sequence !== badgeSequence || blocked) return;
      $('native-work-badge').hidden = body.running !== true;
      $('native-work-badge').dataset.status = 'running';
      text('native-work-badge', body.status === 'stopping' ? 'Project work stopping' : 'Project work running');
      badgeState = body.running !== true ? 'idle' : body.status === 'stopping' ? 'stopping' : 'running';
      badgeWorkUnresolved = body.running === true;
    } catch {
      // Preserved work may still be running; say so and keep Stop all work reachable.
      if (token === generation && sequence === badgeSequence && !blocked) {
        $('native-work-badge').hidden = false;
        $('native-work-badge').dataset.status = 'waiting_approval';
        text('native-work-badge', 'Project work status unconfirmed');
        badgeState = 'uncertain';
      }
    }
    if (token === generation && sequence === badgeSequence) { renderStop(); sync(); }
  }

  function update(payload) {
    const runtime = payload?.runtime || {};
    const kids = runtime.kids_mode || {};
    ownerAllowed = window.HomebodyOwner?.isOwner() === true;
    const wasBlocked = blocked;
    blocked = !ownerAllowed || kids.active === true || kids.locked === true || !runtime.state || runtime.state === 'not_started';
    kidsHidden = kids.active === true || kids.locked === true;
    const notice = payload?.config?.assistant_mode_notice === 'migrated';
    $('assistant-mode-notice').hidden = !notice || blocked;
    if (blocked) {
      if (!wasBlocked || capabilities || !$('native-work-badge').hidden) {
        reset(kids.active || kids.locked ? 'Hidden during Kids Mode.' : 'Pair an owner device and wait for Reachy to be ready.');
      }
      $('voice-stop-button').disabled = true;
      unconfirmed(kidsHidden ? 'Hidden during Kids Mode.' : 'Pair an owner device and wait for Reachy to be ready.');
      return;
    }
    // Same default as the switch buttons and the host: no reported profile is Everyday.
    confirmedMode = runtime.agent?.profile === 'agent' ? 'workspace' : 'everyday';
    if (switchingTo === confirmedMode) switchingTo = '';
    voiceState = String(runtime.state || '');
    renderMode();
    $('voice-stop-button').disabled = stopping;
    voiceConfig = payload?.config || {};
    renderVoice();
    const now = Date.now();
    if (wasBlocked || now - lastCapabilitiesAt > (cameraTimer ? 5000 : 15000)) { lastCapabilitiesAt = now; refreshCapabilities(); }
    // One discovery check still finds work preserved across a browser reload. Outside
    // Workspace only known running work needs polling; failures remain unconfirmed,
    // with Stop reachable, rather than hammering a Kids-latched/unused adult route.
    const badgeApplicable = confirmedMode === 'workspace' || badgeWorkUnresolved;
    if (wasBlocked || lastBadgeAt === 0 || (badgeApplicable && now - lastBadgeAt > 5000)) {
      lastBadgeAt = now; refreshBadge();
    }
  }

  $('voice-stop-button').addEventListener('click', async () => {
    if (stopping) return;
    stopping = true; $('voice-stop-button').disabled = true;
    const message = $('agent-message');
    try {
      const response = await window.fetch('/api/voice/stop', { method: 'POST' });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
      message.textContent = 'Stopped speaking. Approved project work, if any, keeps running.';
      window.notifyFeedback?.(message, 'ok');
    } catch (error) {
      message.textContent = String(error.message || error);
      window.notifyFeedback?.(message, 'error');
    } finally {
      stopping = false; $('voice-stop-button').disabled = blocked;
    }
  });

  $('assistant-mode-notice-ack').addEventListener('click', async () => {
    const response = await window.fetch('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ assistant_mode_notice: '' }),
    });
    if (response.ok) $('assistant-mode-notice').hidden = true;
  });

  function owner(allowed) {
    ownerAllowed = allowed === true;
    if (!ownerAllowed) {
      blocked = true; reset('Owner session ended. Everyday details hidden.');
      unconfirmed('Owner session ended. Pair an owner device to see the mode.');
    }
  }

  document.querySelectorAll('[data-everyday-setting]').forEach(button => {
    button.addEventListener('click', () => {
      if (blocked || window.HomebodyOwner?.isOwner() !== true) return;
      goToControl({ tab: 'settings', id: button.dataset.everydaySetting });
    });
  });

  window.HomebodyAssistantModes = {
    update, owner, sync,
    /** True only for a status-confirmed Workspace with no switch in flight; gates private Agent polls. */
    workspaceConfirmed: () => currentMode() === 'workspace',
    offline: () => { blocked = true; reset('Reachy is unreachable.'); unconfirmed('Reachy is unreachable, so the mode cannot be confirmed.'); },
    /** A mode change was requested ('everyday'/'workspace') or settled (''); only status confirms it. */
    switching: (mode) => {
      switchingTo = MODE_LABELS[mode] ? mode : '';
      if (switchingTo) { badgeSequence += 1; badgeState = 'unknown'; lastBadgeAt = 0; }
      renderMode();
    },
  };
  renderMode();
})();
