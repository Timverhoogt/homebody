/* First-run checks are owner-bound, read-only unless explicitly clicked, and never start media. */
(() => {
  const el = id => document.getElementById(id);
  let owner = false, locked = false, generation = 0, state = null, pending = false;
  const dialog = el('setup-dialog');
  const buttons = ['setup-next', 'setup-back', 'setup-check', 'setup-finish', 'setup-refresh', 'setup-save-language'];
  function clear() {
    generation += 1; state = null; pending = false;
    dialog.close(); el('setup-intro').hidden = true;
    el('setup-host').textContent = ''; el('setup-platform-note').textContent = '';
    el('setup-message').textContent = ''; el('setup-connection-state').textContent = '';
    el('setup-restart-consent').checked = false;
  }
  function render() {
    if (!owner || locked || !state) return;
    const step = state.step || 'welcome';
    ['welcome', 'connection', 'review'].forEach(id => { el(`setup-${id}`).hidden = step !== id; });
    el('setup-intro').hidden = state.required !== true && state.restart_required !== true;
    el('setup-intro-title').textContent = state.restart_required ? 'Setup saved · restart Homebody' : 'Finish setting up Homebody';
    el('setup-host').textContent = `${state.host?.label || 'Computer'} · ${state.host?.system || ''} ${state.host?.machine || ''}`;
    el('setup-platform-note').textContent = `Python ${state.python || 'unknown'} · Reachy SDK ${state.sdk || 'unknown'}. ${state.recommendation || ''} ${state.capability_note || ''}`;
    el('setup-entry').value = state.entry_route || 'unknown';
    el('setup-route-note').textContent = state.entry_route === 'marketplace'
      ? 'Homebody is already installed through Reachy Mini Control. No second installer or daemon is needed.'
      : 'This same setup works for marketplace and manual installations. Configure only what is still missing.';
    el('setup-connection-state').textContent = state.connection_checked
      ? 'Backend connection checked. This is not a provider access or speech test.'
      : state.connection_configured ? 'Connection settings saved. Check them before continuing.' : 'Voice backend not configured yet. The local dashboard is available without one.';
    el('setup-language').value = ['nl', 'es', 'de', 'fr', 'it', 'pt'].includes(state.language) ? state.language : 'en';
    buttons.forEach(id => { el(id).disabled = pending; });
    el('setup-back').hidden = step === 'welcome';
    el('setup-next').hidden = step !== 'welcome';
    el('setup-finish').disabled = pending || !state.connection_checked || !el('setup-restart-consent').checked;
  }
  async function request(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(path, {...options, cache:'no-store', signal:controller.signal});
      const body = await response.json();
      if (!response.ok) {
        if ([401, 403, 423].includes(response.status)) clear();
        throw new Error(response.status === 423 ? 'Adult setup is locked during Kids Mode.' : body.detail || 'Setup is unavailable. Retry after checking the connection.');
      }
      return body;
    } finally { clearTimeout(timer); }
  }
  async function action(path, payload) {
    if (!owner || locked || pending) return;
    const ticket = generation; pending = true; render();
    try {
      const next = await request(path, {method:'POST', headers:{'Content-Type':'application/json'}, ...(payload ? {body:JSON.stringify(payload)} : {})});
      if (!owner || locked || ticket !== generation) return;
      state = next; el('setup-message').textContent = next.note || ''; render();
    } catch (error) {
      if (owner && !locked && ticket === generation) el('setup-message').textContent = String(error.message || error);
    } finally {
      if (ticket === generation) { pending = false; render(); }
    }
  }
  async function refresh() {
    if (!owner || locked || pending) return;
    const ticket = generation; pending = true; render();
    try {
      const next = await request('/api/onboarding');
      if (!owner || locked || ticket !== generation || typeof next.required !== 'boolean') return;
      state = next;
    } catch (error) {
      if (owner && !locked && ticket === generation) el('setup-message').textContent = String(error.message || error);
    } finally { if (ticket === generation) { pending = false; render(); } }
  }
  async function open() {
    if (!owner || locked) return;
    await refresh();
    if (owner && !locked && state && !dialog.open) { dialog.showModal(); render(); }
  }
  el('setup-open').addEventListener('click', open);
  el('setup-review-open').addEventListener('click', open);
  el('setup-close').addEventListener('click', () => dialog.close());
  el('setup-refresh').addEventListener('click', refresh);
  el('setup-next').addEventListener('click', () => action('/api/onboarding/progress', {step:'connection', entry_route:el('setup-entry').value}));
  el('setup-back').addEventListener('click', () => action('/api/onboarding/progress', {step:'welcome'}));
  el('setup-check').addEventListener('click', async () => {
    await action('/api/onboarding/check');
    if (owner && !locked && state?.connection_checked) await action('/api/onboarding/progress', {step:'review'});
  });
  el('setup-save-language').addEventListener('click', async () => {
    if (!owner || locked || pending) return;
    const ticket = generation; pending = true; render();
    try {
      await request('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({language:el('setup-language').value})});
      if (owner && !locked && ticket === generation) el('setup-message').textContent = 'Language saved. Check the connection again.';
    } catch (error) {
      if (owner && !locked && ticket === generation) el('setup-message').textContent = String(error.message || error);
    } finally { if (ticket === generation) { pending = false; await refresh(); } }
  });
  el('setup-settings').addEventListener('click', () => {
    dialog.close(); el('tab-settings').click();
    window.HomebodySettings?.reveal('bridge_url');
  });
  el('setup-restart-consent').addEventListener('change', render);
  el('setup-finish').addEventListener('click', () => {
    if (el('setup-restart-consent').checked && state?.connection_checked) action('/api/onboarding/finish', {confirm:'restart'});
  });
  window.HomebodyOnboarding = {
    refresh,
    owner(value) {
      const next = value === true;
      if (!next) { owner = false; clear(); return; }
      if (!owner) { owner = true; generation += 1; refresh(); }
    },
    observe(status) {
      const next = status.runtime?.kids_mode?.locked === true;
      if (next !== locked) { locked = next; if (locked) clear(); else refresh(); }
    },
  };
})();
