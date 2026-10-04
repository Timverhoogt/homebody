/* Owner sessions live only in Secure HttpOnly cookies. CSRF stays in memory. */
(() => {
  const byId = id => document.getElementById(id);
  const rawFetch = window.fetch.bind(window);
  let session = { owner: false };
  let ready;
  function render() {
    window.HomebodyNotifications?.setSuppressed("owner", !session.owner);
    byId('owner-pair-panel').hidden = session.owner;
    byId('owner-device-controls').hidden = !session.owner;
    document.querySelector('main').hidden = !session.owner;
    document.querySelector('main').inert = !session.owner;
    byId('owner-device-name').textContent = session.owner ? `Owner · ${session.name}` : '';
    if (!session.owner) window.ReachyCamera?.stop('Owner session ended. Pair this device to continue.');
    const keyField = byId('current_api_key');
    if (keyField) keyField.closest('label')?.setAttribute('hidden', '');
    // The Ask box uses the owner session; hide the key field when paired.
    const visionKey = byId('local-vision-key');
    if (visionKey) {
      visionKey.setAttribute('hidden', session.owner ? '' : 'hidden');
      const visionKeyLabel = document.querySelector('label[for="local-vision-key"]');
      if (visionKeyLabel) visionKeyLabel.setAttribute('hidden', session.owner ? '' : 'hidden');
    }
  }
  async function check() {
    try {
      const response = await rawFetch('/api/owner/session', { cache: 'no-store', credentials: 'same-origin' });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
      session = body;
      byId('owner-message').textContent = session.owner ? '' : 'Use a one-time code generated locally on your robot. No camera or microphone starts when you pair.';
    } catch (error) {
      session = { owner: false };
      byId('owner-message').textContent = String(error.message || error);
    }
    render();
    return session;
  }
  // One transport wrapper covers every existing control and camera fetch, including keepalive.
  window.fetch = async (input, options = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith('/api/')) return rawFetch(input, options);
    await ready;
    const headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
    const method = (options.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && session.csrf) headers.set('X-Homebody-CSRF', session.csrf);
    const response = await rawFetch(input, { ...options, headers, credentials: 'same-origin' });
    if (response.status === 401 && !url.pathname.startsWith('/api/owner/')) {
      session = { owner: false };
      byId('owner-message').textContent = 'This device session expired or was revoked. Pair again to continue.';
      render();
    }
    return response;
  };
  byId('owner-pair-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = byId('owner-pair-button');
    button.disabled = true;
    byId('owner-message').textContent = 'Pairing this device…';
    try {
      const response = await rawFetch('/api/owner/pair', {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: byId('owner-pair-code').value.trim(), name: byId('owner-name').value.trim() || 'Owner device' }),
      });
      byId('owner-pair-code').value = '';
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
      await check();
      if (session.owner && typeof refreshStatus === 'function') await refreshStatus();
    } catch (error) {
      byId('owner-message').textContent = String(error.message || error);
    } finally { button.disabled = false; }
  });
  byId('owner-logout').addEventListener('click', async () => {
    try {
      // End this browser's gesture/media first; logout itself never wakes or starts anything.
      window.ReachyCamera?.stop('Camera stopped before forgetting this device.');
      const response = await window.fetch('/api/owner/logout', { method: 'POST' });
      if (!response.ok) throw new Error('Could not forget this device. Try again.');
      location.reload();
    } catch (error) {
      byId('owner-devices-message').textContent = String(error.message || error);
      window.HomebodyNotifications?.show(`Owner devices: ${error.message || error}`, { id: 'owner-devices', kind: 'error' });
    }
  });
  byId('owner-devices-refresh').addEventListener('click', async () => {
    const list = byId('owner-device-list');
    try {
      const response = await window.fetch('/api/owner/devices', { cache: 'no-store' });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || 'Device list unavailable');
      list.replaceChildren();
      for (const device of body.devices) {
        const row = document.createElement('li');
        const name = document.createElement('span');
        name.textContent = `${device.name}${device.id === session.device_id ? ' (this device)' : ''} `;
        const button = document.createElement('button');
        button.type = 'button'; button.textContent = 'Revoke';
        button.addEventListener('click', async () => {
          button.disabled = true;
          try {
            const result = await window.fetch('/api/owner/revoke', {
              method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ device_id: device.id }),
            });
            if (!result.ok) throw new Error('Revocation failed; retry.');
            if (device.id === session.device_id) location.reload();
            else row.remove();
          } catch (error) {
            button.disabled = false;
            byId('owner-devices-message').textContent = error.message;
            window.HomebodyNotifications?.show(`Owner devices: ${error.message || error}`, { id: 'owner-devices', kind: 'error' });
          }
        });
        row.append(name, button); list.append(row);
      }
    } catch (error) {
      byId('owner-devices-message').textContent = String(error.message || error);
      window.HomebodyNotifications?.show(`Owner devices: ${error.message || error}`, { id: 'owner-devices', kind: 'error' });
    }
  });
  ready = check();
})();
