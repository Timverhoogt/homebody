/* Software updates: no HTML from release notes, no optimistic success, reconnect from durable jobs. */
(() => {
  const $ = id => document.getElementById(id);
  const dialog = $('update-dialog');
  let catalog = null;
  let pending = false;
  let acknowledgedId = null;
  // The exact offer shown in the open preview. Install sends this, never a catalog entry that arrived later.
  let reviewed = null;
  let offerWarning = false;
  // Completions opened automatically this page session; History can still reopen one explicitly.
  const shownSuccess = new Set();
  let completed = null;
  let actionError = '';
  const active = new Set(['queued', 'downloading', 'installing', 'stopping', 'starting', 'verifying', 'rolling_back']);
  const controls = ['update-check', 'update-preview', 'update-install', 'update-later', 'update-ack'];
  // Discovery caps each release body at this length; a note that reaches it was shortened.
  const NOTE_LIMIT = 100000;
  const sameRelease = (a, b) => !!a && !!b && JSON.stringify(a) === JSON.stringify(b);
  const semver = value => /^\d+\.\d+\.\d+$/.test(value || '') ? value.split('.').map(Number) : null;
  // Old-to-new by version number (0.5.9 before 0.5.10); unparseable versions keep their order at the end.
  const ordered = rows => (rows || []).map((row, index) => ({row, index, key:semver(row.version)})).sort((a, b) => {
    if (!a.key || !b.key) return (!a.key - !b.key) || a.index - b.index;
    for (let i = 0; i < 3; i++) { if (a.key[i] !== b.key[i]) return a.key[i] - b.key[i]; }
    return a.index - b.index;
  }).map(item => item.row);
  // Text-only sections: one heading and one plain-text block per published release.
  function renderNotes(container, rows) {
    container.replaceChildren();
    for (const row of ordered(rows)) {
      const section = document.createElement('section');
      const heading = document.createElement('h3');
      heading.textContent = `Homebody ${row.version}`;
      const text = document.createElement('p');
      text.className = 'update-note-text';
      text.textContent = `${row.notes}`;
      section.appendChild(heading); section.appendChild(text); container.appendChild(section);
    }
  }
  // Only the published notes we actually hold are described; never claim a complete version range.
  function coverage(rows, target) {
    const list = ordered(rows);
    if (!list.length) return `Release notes are unavailable for this update to Homebody ${target}.`;
    const count = `${list.length} published release${list.length === 1 ? '' : 's'}`;
    const versions = list.map(row => row.version).join(', ');
    const shortened = list.some(row => `${row.notes}`.length >= NOTE_LIMIT) ? ' Some notes were shortened.' : '';
    if (!list.some(row => row.version === target)) {
      return `Notes from ${count} (${versions}) are included, but none for Homebody ${target}; the list may be incomplete.${shortened}`;
    }
    return `Notes from ${count} are included: ${versions}.${shortened}`;
  }
  // Scroll ownership: the dashboard stays where it was while the modal is open, then is restored exactly.
  // A class (not inline styles) so existing inline styles and other owners such as camera fullscreen are untouched.
  let restoreScroll = null;
  function lockScroll() {
    if (restoreScroll) return;
    restoreScroll = {x:window.scrollX, y:window.scrollY};
    document.documentElement.classList.add('update-dialog-scroll-lock');
  }
  function released() {
    reviewed = null; acknowledgedId = null; offerWarning = false; dialogError('');
    if (!restoreScroll) return;
    const {x, y} = restoreScroll;
    restoreScroll = null;
    document.documentElement.classList.remove('update-dialog-scroll-lock');
    if (window.scrollX !== x || window.scrollY !== y) window.scrollTo(x, y);
  }
  function closeDialog() { if (dialog.open) dialog.close(); released(); }
  function dialogError(text) { $('update-dialog-error').textContent = text; $('update-dialog-error').hidden = !text; }
  // Newest journal entry only: an older success never displaces a newer failure, rollback or running job.
  function completion(body) {
    if (body.job && body.job.state !== 'succeeded') return null;
    const latest = (body.history || []).at(-1);
    if (!latest || latest.state !== 'succeeded' || latest.acknowledged) return null;
    if (body.job?.id && body.job.id !== latest.id) return null;
    if (latest.manifest?.version !== body.current?.version) return null;
    if (body.current?.commit && latest.manifest?.commit && body.current.commit !== latest.manifest.commit) return null;
    return latest;
  }
  // An open preview keeps its release; a changed or withdrawn offer disables Install and says why.
  function syncOffer() {
    if (!reviewed) { $('update-install').disabled = true; return; }
    const current = catalog?.available;
    const valid = sameRelease(reviewed, current);
    $('update-install').disabled = pending || active.has(catalog?.job?.state) || !catalog?.supported || !valid;
    if (!valid) {
      offerWarning = true;
      dialogError(current
        ? `Homebody ${current.version} is now offered instead of ${reviewed.version}. Close and review it before installing.`
        : `Homebody ${reviewed.version} is no longer offered. Close and check for Reachy updates again.`);
    } else if (offerWarning) { offerWarning = false; dialogError(''); }
  }
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
    reviewed = success ? null : catalog.available;
    offerWarning = false; dialogError('');
    const between = ordered(rows).length > 1;
    $('update-dialog-title').textContent = success ? 'Reachy update installed' : 'Reachy software update available';
    $('update-summary').textContent = success
      ? `Homebody on Reachy updated from ${success.from_version} to ${success.manifest.version} in one direct installation. Installation and restart were verified. No need to install that version again.`
      : `Update Homebody on Reachy from ${catalog.current.version} to ${reviewed.version} in one direct installation${between ? '; the releases in between are not installed separately' : ''}. Its dashboard will update too; your settings are preserved.`;
    $('update-coverage').textContent = coverage(rows, success ? success.manifest.version : reviewed.version);
    renderNotes($('update-notes'), rows);
    $('update-details').open = false;
    dialog.scrollTop = 0;
    $('update-install').hidden = !!success;
    $('update-ack').hidden = !success;
    $('update-ack').disabled = pending;
    syncOffer();
    if (!dialog.open) { lockScroll(); dialog.showModal(); window.HomebodyMotion?.enter(dialog); }
  }
  function render(body, sequence = readSequence) {
    // Fence before mutating retained state, not only before drawing the rail.
    // Otherwise a rejected stale success can be replayed by a later failed Check.
    if (sequence < railSequence) return;
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
    syncOffer();
    $('update-ack').disabled = pending;
    $('update-notice').hidden = !catalog.available || busy || body.deferred_release_id === catalog.available.release_id;
    $('update-notice-text').textContent = catalog.available ? `Reachy software ${catalog.available.version} is available` : '';
    completed = completion(body);
    // Polling redraws History; keep entries the user opened open.
    const expanded = new Set([...$('update-history').children].filter(item => item.open).map(item => item.dataset.job));
    $('update-history').replaceChildren();
    for (const history of [...(body.history || [])].reverse()) {
      const details = document.createElement('details');
      details.dataset.job = history.id;
      details.open = expanded.has(history.id);
      const summary = document.createElement('summary');
      summary.textContent = `Reachy ${history.from_version} → ${history.manifest.version} · ${history.state.replaceAll('_',' ')}`;
      details.appendChild(summary);
      if (history.error) {
        const error = document.createElement('p');
        error.textContent = history.error;
        details.appendChild(error);
      }
      if (completed && history.id === completed.id) {
        const reopen = document.createElement('button');
        reopen.type = 'button'; reopen.className = 'secondary'; reopen.textContent = 'Show completion';
        reopen.addEventListener('click', () => { if (completed?.id === history.id && !pending) preview(history.notes, history); });
        details.appendChild(reopen);
      }
      const content = document.createElement('div');
      renderNotes(content, history.notes);
      details.appendChild(content); $('update-history').appendChild(details);
    }
    railBase = {
      job: job?.state || '', acknowledged: job?.acknowledged === true,
      available: !!catalog.available, supported: body.supported !== false, checked: !!body.checked_at,
    };
    renderRail({...railBase, checking}, sequence);
    // Never displace an open dialog; a closed completion is not reopened automatically this session.
    if (completed && !shownSuccess.has(completed.id) && !dialog.open) {
      shownSuccess.add(completed.id); preview(completed.notes, completed);
    }
  }
  async function refresh() {
    if (pending) return;
    const sequence = ++readSequence;
    try { render(await api(), sequence); }
    catch (error) {
      renderRail({unreachable:true}, sequence);
      disable();
      $('update-notice').hidden = true;
      closeDialog();
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
        closeDialog();
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
      // The status line sits behind the modal: report there too, or close it when ownership was lost.
      if (dialog.open) {
        if (window.HomebodyOwner && window.HomebodyOwner.isOwner() !== true) closeDialog();
        else { offerWarning = false; dialogError(actionError); $('update-ack').disabled = false; syncOffer(); }
      }
      // An unconfirmed request says nothing new about the job: keep the last durable reading.
      renderRail({...railBase}, sequence);
      return false;
    } finally { pending = false; checking = false; }
  }
  $('update-check').addEventListener('click', () => action('/check', {}));
  $('update-preview').addEventListener('click', () => { if (catalog?.available) preview(catalog.notes); });
  $('update-open').addEventListener('click', () => { if (catalog?.available) preview(catalog.notes); });
  $('update-close').addEventListener('click', closeDialog);
  // Escape/cancel and any other native close path release the same ownership.
  dialog.addEventListener('close', () => { if (!dialog.open) released(); });
  $('update-later').addEventListener('click', () => {
    if (catalog?.available) action('/later', {release_id:catalog.available.release_id});
  });
  $('update-ack').addEventListener('click', async () => {
    if (!acknowledgedId) return;
    if (await action('/acknowledge', {job_id:acknowledgedId})) closeDialog();
  });
  $('update-install').addEventListener('click', async () => {
    const target = reviewed;
    if (pending || !catalog?.supported || !target || !sameRelease(target, catalog.available)) return;
    if (!window.confirm(`Install Homebody ${target.version} on Reachy (currently ${catalog.current.version})? This is one direct installation; releases in between are not installed separately. Its app and dashboard will restart. The Android app (APK) will not change. Reachy must be safely folded in Standby. Settings are preserved; failed startup is rolled back.`)) return;
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
