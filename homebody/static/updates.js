/* Software updates: no HTML from release notes, no optimistic success, reconnect from durable jobs. */
(() => {
  const $ = id => document.getElementById(id);
  let catalog = null;
  let pending = false;
  let acknowledgedId = null;
  let displayedSuccess = null;
  const active = new Set(['queued', 'downloading', 'installing', 'stopping', 'starting', 'verifying', 'rolling_back']);
  const controls = ['update-check', 'update-preview', 'update-install', 'update-later', 'update-ack'];
  const notes = rows => (rows || []).map(row => `Homebody ${row.version}\n${row.notes}`).join('\n\n');
  async function api(path = '', payload) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 45000);
    try {
      const response = await window.fetch(`/api/updates${path}`, {
        cache: 'no-store', signal: controller.signal,
        ...(payload === undefined ? {} : {method: 'POST', headers: {'Content-Type':'application/json'}, body:JSON.stringify(payload)}),
      });
      if (!response.ok) {
        let detail = `HTTP ${response.status}`;
        try { detail = (await response.json()).detail || detail; } catch { /* proxy restart response */ }
        throw new Error(detail);
      }
      return await response.json();
    } finally { clearTimeout(timer); }
  }
  function disable() { controls.forEach(id => { $(id).disabled = true; }); }
  function preview(rows, success = null) {
    acknowledgedId = success?.id || null;
    $('update-notes').textContent = success
      ? `Updated and verified: ${success.from_version} → ${success.manifest.version}\n\n${notes(rows)}` : notes(rows);
    $('update-install').hidden = !!success;
    $('update-ack').hidden = !success;
    if (!$('update-dialog').open) $('update-dialog').showModal();
  }
  function render(body) {
    catalog = body;
    const job = body.job;
    const busy = pending || active.has(job?.state);
    $('update-current').textContent = `Installed Homebody ${body.current.version}`;
    $('update-status').textContent = job && active.has(job.state)
      ? `Update ${job.state.replaceAll('_',' ')} · reconnecting is safe; progress is saved.`
      : body.error || (job ? `Last update: ${job.state.replaceAll('_',' ')}${job.error ? ` · ${job.error}` : ''}` : '')
        || (body.available ? `Homebody ${body.available.version} is available.` : 'No newer stable release found.');
    if (!body.supported) $('update-status').textContent += ` ${body.reason}`;
    $('update-check').disabled = busy;
    $('update-preview').disabled = busy || !body.available;
    $('update-later').disabled = busy || !body.available;
    $('update-install').disabled = busy || !body.supported || !body.available;
    $('update-ack').disabled = pending;
    $('update-notice').hidden = !body.available || busy || body.deferred_release_id === body.available.release_id;
    $('update-notice-text').textContent = body.available ? `Homebody ${body.available.version} is available` : '';
    $('update-history').replaceChildren();
    for (const history of [...(body.history || [])].reverse()) {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = `${history.from_version} → ${history.manifest.version} · ${history.state.replaceAll('_',' ')}`;
      const content = document.createElement('pre');
      content.textContent = `${history.error || ''}\n${notes(history.notes)}`;
      details.appendChild(summary); details.appendChild(content); $('update-history').appendChild(details);
    }
    const unseen = (body.history || []).find(h => h.state === 'succeeded' && !h.acknowledged && h.id !== displayedSuccess);
    if (unseen) { displayedSuccess = unseen.id; preview(unseen.notes, unseen); }
  }
  async function refresh() {
    if (pending) return;
    try { render(await api()); }
    catch (error) {
      disable();
      $('update-notice').hidden = true;
      $('update-dialog').close();
      $('update-status').textContent = `Updates unavailable; reconnecting to read saved progress. ${error.message}`;
    }
  }
  async function action(path, payload) {
    if (pending) return;
    pending = true; disable();
    try {
      const body = await api(path, payload);
      if (path === '/install') {
        $('update-dialog').close();
        $('update-status').textContent = `Update ${body.state} · waiting for verified restart.`;
      } else { pending = false; render(body); }
    } catch (error) {
      $('update-status').textContent = `Update action was not confirmed: ${error.message}. Saved progress will be checked on reconnect.`;
    } finally { pending = false; }
  }
  $('update-check').addEventListener('click', () => action('/check', {}));
  $('update-preview').addEventListener('click', () => { if (catalog?.available) preview(catalog.notes); });
  $('update-open').addEventListener('click', () => { if (catalog?.available) preview(catalog.notes); });
  $('update-close').addEventListener('click', () => $('update-dialog').close());
  $('update-later').addEventListener('click', () => action('/later', {release_id:catalog.available.release_id}));
  $('update-ack').addEventListener('click', async () => {
    if (!acknowledgedId) return;
    await action('/acknowledge', {job_id:acknowledgedId});
    $('update-dialog').close();
  });
  $('update-install').addEventListener('click', async () => {
    if (pending || !catalog?.supported || !catalog?.available) return;
    const target = catalog.available;
    if (!window.confirm(`Install Homebody ${target.version}? Reachy must already be in safely folded Standby. The app will stop and restart. Settings are preserved; failed startup is rolled back.`)) return;
    await action('/install', {confirm:'INSTALL', release_id:target.release_id});
  });
  window.HomebodyUpdates = {refresh};
  // Owner transport injects CSRF and gates every fetch. A locked/guest response never triggers discovery.
  refresh().then(() => {
    if (catalog && !catalog.checked_at) action('/check', {});
  });
  setInterval(async () => {
    await refresh();
    if (catalog && !pending && !active.has(catalog.job?.state) && catalog.checked_at && Date.now()/1000-catalog.checked_at > 86400) {
      await action('/check', {});
    }
  }, 5000);
})();
