/* Speaker-only controls: drafts aren't confirmation; stale replies cannot survive owner/Kids/offline changes. */
(() => {
  const $ = id => document.getElementById(id);
  let confirmed = null, dirty = false, blocked = true, ownerAllowed = false;
  let reading = false, writing = false, generation = 0, uncertain = false;
  const requests = new Set();
  function valid(body) {
    return body && Number.isInteger(body.volume) && body.volume >= 0 && body.volume <= 100
      && typeof body.muted === 'boolean' && body.muted === (body.volume === 0) && typeof body.can_unmute === 'boolean';
  }
  function controls() {
    const unavailable = blocked || !ownerAllowed || !confirmed || uncertain || writing;
    $('speaker-volume').disabled = unavailable;
    $('speaker-apply').disabled = unavailable || !dirty || reading;
    $('speaker-mute').disabled = unavailable || reading || (confirmed?.muted && !confirmed.can_unmute);
    $('speaker-refresh').disabled = blocked || !ownerAllowed || reading || writing;
    $('speaker-mute').textContent = confirmed?.muted ? 'Unmute speaker' : 'Mute speaker';
  }
  function render(body) {
    if (!valid(body)) throw new Error('No verified speaker state returned');
    confirmed = body; uncertain = false;
    $('speaker-confirmed').textContent = body.muted ? 'Muted · 0%' : `Speaker · ${body.volume}%`;
    if (!dirty) {
      $('speaker-volume').value = String(body.volume);
      $('speaker-draft').textContent = `${body.volume}%`;
    }
    controls();
  }
  function reset(message) {
    generation += 1;
    requests.forEach(controller => controller.abort()); requests.clear();
    confirmed = null; dirty = false; reading = false; writing = false; blocked = true;
    $('speaker-confirmed').textContent = 'Speaker · unknown';
    $('speaker-draft').textContent = '—';
    $('speaker-message').textContent = message;
    controls();
  }
  async function api(payload) {
    const controller = new AbortController(); requests.add(controller);
    const timer = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await window.fetch('/api/audio/volume', {
        cache:'no-store', signal:controller.signal,
        ...(payload === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)}),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
      if (!valid(body)) throw new Error('No verified speaker state returned');
      return body;
    } finally { clearTimeout(timer); requests.delete(controller); }
  }
  async function refresh() {
    if (reading || writing || blocked || !ownerAllowed || document.hidden) return;
    const token = generation;
    reading = true; controls();
    try {
      const body = await api();
      if (token !== generation || blocked || !ownerAllowed) return;
      render(body);
    } catch (error) {
      if (token !== generation) return;
      confirmed = null;
      $('speaker-confirmed').textContent = 'Speaker · unknown';
      $('speaker-message').textContent = `Speaker volume unavailable: ${error.message}`;
    } finally {
      if (token === generation) { reading = false; controls(); }
    }
  }
  async function change(payload) {
    if (writing || reading || blocked || !ownerAllowed || !confirmed) return;
    const token = ++generation;
    writing = true; controls();
    $('speaker-message').textContent = 'Changing speaker level…';
    try {
      const body = await api(payload);
      if (token !== generation || blocked || !ownerAllowed) return;
      dirty = false; render(body);
      $('speaker-message').textContent = body.muted ? 'Speaker muted. Microphone unchanged.' : `Speaker set and verified at ${body.volume}%.`;
    } catch (error) {
      if (token !== generation) return;
      uncertain = true;
      $('speaker-confirmed').textContent = `Last verified · ${confirmed.volume}% · read again`;
      $('speaker-message').textContent = `Speaker change not confirmed: ${error.message}. Read current volume before retrying.`;
    } finally {
      if (token === generation) { writing = false; controls(); }
    }
  }
  function update(runtime) {
    ownerAllowed = window.HomebodyOwner?.isOwner() === true;
    const kids = runtime?.kids_mode || {};
    if (!ownerAllowed || !runtime?.state || runtime.state === 'not_started' || kids.active || kids.locked) {
      reset(kids.active || kids.locked ? 'Speaker controls are locked during Kids Mode.' : 'Pair an owner device and wait for Reachy to be ready.');
      return;
    }
    const wasBlocked = blocked;
    blocked = false; controls();
    if (wasBlocked) refresh();
  }
  function owner(allowed) {
    ownerAllowed = allowed === true;
    if (!ownerAllowed) reset('Owner session ended. Speaker controls locked.');
  }
  $('speaker-volume').addEventListener('input', () => {
    dirty = true;
    $('speaker-draft').textContent = `${$('speaker-volume').value}% · not applied`;
    controls();
  });
  $('speaker-apply').addEventListener('click', () => change({volume:Number($('speaker-volume').value)}));
  $('speaker-mute').addEventListener('click', () => { if (confirmed) change({muted:!confirmed.muted}); });
  $('speaker-refresh').addEventListener('click', refresh);
  window.HomebodyVolume = {refresh, update, owner, offline:() => reset('Reachy is unreachable. Speaker volume unknown.')};
  controls();
  setInterval(() => { if (!$('panel-settings').hidden) refresh(); }, 10000);
})();
