/* Everyday/Workspace surface: disclosure, Stop speaking and a status-only project-work badge.
   Late replies cannot repaint after owner loss, Kids Mode or going offline (generation tokens). */
(() => {
  const $ = id => document.getElementById(id);
  let generation = 0, ownerAllowed = false, blocked = true, capabilities = null;
  let lastCapabilitiesAt = 0, lastBadgeAt = 0, stopping = false;
  const text = (id, value) => { $(id).textContent = value; };

  function reset(message) {
    generation += 1;
    capabilities = null; lastCapabilitiesAt = 0; lastBadgeAt = 0;
    text('everyday-badge', 'Unavailable');
    $('everyday-badge').dataset.status = 'failed';
    ['everyday-model', 'everyday-weather', 'everyday-time', 'everyday-devices'].forEach(id => text(id, '—'));
    text('everyday-message', message);
    $('native-work-badge').hidden = true;
    $('voice-stop-button').disabled = blocked || stopping;
  }

  function renderCapabilities(body) {
    capabilities = body;
    const provider = body.text_provider;
    text('everyday-model', provider ? `${provider.model} · ${provider.label} (${provider.region})` : 'Not configured');
    const weather = body.weather || {};
    text('everyday-weather', weather.source
      ? `${weather.label}${weather.location_leaves_home ? ' · your configured location is sent to this provider' : ' · stays in your home'}`
      : 'No weather source configured · Everyday will say so instead of guessing');
    text('everyday-time', body.time_source === 'hermes_host_clock' ? 'Hermes host clock' : 'Unavailable');
    const controls = body.devices?.controls || [], status = body.devices?.status || [];
    text('everyday-devices', controls.length || status.length
      ? `Control: ${controls.join(', ') || 'none'} · Status: ${status.join(', ') || 'none'}`
      : 'None shared with Everyday');
    text('everyday-badge', body.available ? 'Ready' : 'Unavailable');
    $('everyday-badge').dataset.status = body.available ? 'completed' : 'failed';
    text('everyday-message', body.available
      ? 'Names and providers are set on the Hermes host; keys and coordinates never reach this page.'
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
    const token = generation;
    try {
      const body = await getJson('/api/everyday/capabilities', token);
      if (body && token === generation && !blocked) renderCapabilities(body);
    } catch (error) {
      if (token !== generation || blocked) return;
      text('everyday-badge', 'Unavailable');
      $('everyday-badge').dataset.status = 'failed';
      text('everyday-message', `Everyday status is unavailable: ${String(error.message || error)}`);
    }
  }

  async function refreshBadge() {
    const token = generation;
    try {
      const body = await getJson('/api/agent/workspace/native-badge', token);
      if (!body || token !== generation || blocked) return;
      $('native-work-badge').hidden = body.running !== true;
      text('native-work-badge', body.status === 'stopping' ? 'Project work stopping' : 'Project work running');
    } catch {
      if (token === generation) $('native-work-badge').hidden = true;
    }
  }

  function update(payload) {
    const runtime = payload?.runtime || {};
    const kids = runtime.kids_mode || {};
    ownerAllowed = window.HomebodyOwner?.isOwner() === true;
    const wasBlocked = blocked;
    blocked = !ownerAllowed || kids.active === true || kids.locked === true || !runtime.state || runtime.state === 'not_started';
    $('everyday-card').hidden = kids.active === true || kids.locked === true;
    const notice = payload?.config?.assistant_mode_notice === 'migrated';
    $('assistant-mode-notice').hidden = !notice || blocked;
    if (blocked) {
      if (!wasBlocked || capabilities || !$('native-work-badge').hidden) {
        reset(kids.active || kids.locked ? 'Hidden during Kids Mode.' : 'Pair an owner device and wait for Reachy to be ready.');
      }
      $('voice-stop-button').disabled = true;
      return;
    }
    $('voice-stop-button').disabled = stopping;
    const now = Date.now();
    if (wasBlocked || now - lastCapabilitiesAt > 60000) { lastCapabilitiesAt = now; refreshCapabilities(); }
    if (wasBlocked || now - lastBadgeAt > 5000) { lastBadgeAt = now; refreshBadge(); }
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
    if (!ownerAllowed) { blocked = true; reset('Owner session ended. Everyday details hidden.'); }
  }

  window.HomebodyAssistantModes = { update, owner, offline: () => { blocked = true; reset('Reachy is unreachable.'); } };
})();
