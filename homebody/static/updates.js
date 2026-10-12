/* Software updates: no HTML from release notes, no optimistic success, reconnect from durable jobs. */
(() => {
  const $ = id => document.getElementById(id);
  let catalog = null;
  let pending = false;
  let acknowledgedId = null;
  let displayedSuccess = null;
  let actionError = '';
  const active = new Set(['queued', 'downloading', 'installing', 'stopping', 'starting', 'verifying', 'rolling_back']);
  const controls = ['update-check', 'update-preview', 'update-install', 'update-later', 'update-ack'];
  const notes = rows => (rows || []).map(row => `Homebody ${row.version}\n${row.notes}`).join('\n\n');
  // Display-only rail (scene.js) over the durable job state. Reads are numbered as they start so an
  // older answer that lands late can never move the rail backwards or forwards.
  const rail = window.HomebodyScene?.mount($('update-rail'), {variant:'rail', label:'Reachy software update'}) || null;
  let readSequence = 0;
  let railSequence = 0;
  let checking = false;
  let railBase = {checked:false};
  function renderRail(view, sequence = readSequence) {
    if (!rail || sequence < railSequence) return;
    railSequence = sequence;
    const owner = window.HomebodyOwner ? window.HomebodyOwner.isOwner() === true : true;
    rail.update(window.HomebodyScene.updaterView(owner ? view : {offline:true}));
  }
  const newer = (next, current) => {
    if (!/^\d+\.\d+\.\d+$/.test(next || '') || !/^\d+\.\d+\.\d+$/.test(current || '')) return false;
    const a = next.split('.').map(Number), b = current.split('.').map(Number);
    for (let i = 0; i < 3; i++) { if (a[i] !== b[i]) return a[i] > b[i]; }
    return false;
  };
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
    $('update-dialog-title').textContent = success ? 'Reachy update installed' : 'Reachy software update available';
    $('update-summary').textContent = success
      ? `Homebody on Reachy updated from ${success.from_version} to ${success.manifest.version}. Installation and restart were verified. No need to install that version again.`
      : `Install Homebody ${catalog.available.version} on Reachy. Its dashboard will update too; your settings are preserved.`;
    $('update-notes').textContent = notes(rows);
    $('update-details').open = false;
    $('update-dialog').scrollTop = 0;
    $('update-install').hidden = !!success;
    $('update-ack').hidden = !success;
    if (!$('update-dialog').open) { $('update-dialog').showModal(); window.HomebodyMotion?.enter($('update-dialog')); }
  }
  function render(body, sequence = readSequence) {
    catalog = {...body, available:newer(body.available?.version, body.current.version) ? body.available : null};
    const job = body.job;
    const busy = pending || active.has(job?.state);
    $('update-current').textContent = `Reachy software: Homebody ${body.current.version}`;
    $('update-status').textContent = job && active.has(job.state)
      ? `Reachy update: ${job.state.replaceAll('_',' ')}. Reconnecting is safe; progress is saved.`
      : actionError || (body.error ? `Could not check for Reachy updates. ${body.error}` : '')
        || (job && ['failed','rolled_back','rollback_failed'].includes(job.state)
          ? `${job.state === 'rolled_back' ? 'Update did not complete; the previous Reachy version was restored.' : 'Reachy update failed; operator help may be needed.'} ${job.error || ''}` : '')
        || (catalog.available ? `Reachy software ${catalog.available.version} is available; currently installed: ${body.current.version}.`
          : body.checked_at ? `Reachy is up to date: Homebody ${body.current.version}. No installation is needed. The Android app is updated separately.`
            : 'Check for newer Reachy software. This does not check the Android app.');
    if (!body.supported) $('update-status').textContent += ` Updates unavailable: ${body.reason || 'updater is not ready'}.`;
    $('update-check').disabled = busy;
    $('update-preview').disabled = busy || !catalog.available;
    $('update-later').disabled = busy || !catalog.available;
    $('update-install').disabled = busy || !body.supported || !catalog.available;
    $('update-ack').disabled = pending;
    $('update-notice').hidden = !catalog.available || busy || body.deferred_release_id === catalog.available.release_id;
    $('update-notice-text').textContent = catalog.available ? `Reachy software ${catalog.available.version} is available` : '';
    $('update-history').replaceChildren();
    for (const history of [...(body.history || [])].reverse()) {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = `Reachy ${history.from_version} → ${history.manifest.version} · ${history.state.replaceAll('_',' ')}`;
      const content = document.createElement('pre');
      content.textContent = `${history.error || ''}\n${notes(history.notes)}`;
      details.appendChild(summary); details.appendChild(content); $('update-history').appendChild(details);
    }
    railBase = {
      job: job?.state || '', acknowledged: job?.acknowledged === true,
      available: !!catalog.available, supported: body.supported !== false, checked: !!body.checked_at,
    };
    renderRail({...railBase, checking}, sequence);
    const unseen = (body.history || []).find(h => h.state === 'succeeded' && !h.acknowledged && h.id !== displayedSuccess);
    if (unseen) { displayedSuccess = unseen.id; preview(unseen.notes, unseen); }
  }
  async function refresh() {
    if (pending) return;
    const sequence = ++readSequence;
    try { render(await api(), sequence); }
    catch (error) {
      renderRail({unreachable:true}, sequence);
      disable();
      $('update-notice').hidden = true;
      $('update-dialog').close();
      $('update-status').textContent = `Reachy updater unavailable; reconnecting to read saved progress. Update success is not confirmed. ${error.message}`;
    }
  }
  async function action(path, payload) {
    if (pending) return false;
    pending = true; actionError = ''; disable();
    const sequence = ++readSequence;
    const feedback = path === '/check' ? window.HomebodyMotion?.pending($('update-check')) : null;
    checking = path === '/check';
    if (checking) renderRail({...railBase, checking:true}, sequence);
    $('update-status').textContent = path === '/check'
      ? 'Checking for Reachy software updates… The Android app is not being updated.'
      : 'Sending your request to the Reachy updater…';
    try {
      const body = await api(path, payload);
      checking = false;
      if (path === '/install') {
        $('update-dialog').close();
        $('update-status').textContent = `Reachy update ${body.state}. Waiting for installation and verified restart; the Android app is unchanged.`;
        renderRail({job: body.state}, sequence);
      } else { pending = false; render(body, sequence); }
      window.HomebodyMotion?.settle(feedback, 'confirmed');
      return true;
    } catch (error) {
      checking = false;
      window.HomebodyMotion?.settle(feedback, 'failed');
      actionError = `Reachy update request was not confirmed: ${error.message}. Saved progress will be checked on reconnect.`;
      $('update-status').textContent = actionError;
      $('update-check').disabled = false;
      // An unconfirmed request says nothing new about the job: keep the last durable reading.
      renderRail({...railBase}, sequence);
      return false;
    } finally { pending = false; checking = false; }
  }
  $('update-check').addEventListener('click', () => action('/check', {}));
  $('update-preview').addEventListener('click', () => { if (catalog?.available) preview(catalog.notes); });
  $('update-open').addEventListener('click', () => { if (catalog?.available) preview(catalog.notes); });
  $('update-close').addEventListener('click', () => $('update-dialog').close());
  $('update-later').addEventListener('click', () => action('/later', {release_id:catalog.available.release_id}));
  $('update-ack').addEventListener('click', async () => {
    if (!acknowledgedId) return;
    if (await action('/acknowledge', {job_id:acknowledgedId})) $('update-dialog').close();
  });
  $('update-install').addEventListener('click', async () => {
    if (pending || !catalog?.supported || !catalog?.available) return;
    const target = catalog.available;
    if (!window.confirm(`Install Homebody ${target.version} on Reachy? Its app and dashboard will restart. The Android app (APK) will not change. Reachy must be safely folded in Standby. Settings are preserved; failed startup is rolled back.`)) return;
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
