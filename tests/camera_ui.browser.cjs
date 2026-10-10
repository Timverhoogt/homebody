// Run: NODE_PATH=/path/to/node_modules node --test tests/camera_ui.browser.cjs
// All streams and HTTP responses are synthetic; never contacts a robot.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const staticDir = path.join(__dirname, '../homebody/static');

async function setup(t, { width = 900, height = 700, fallback = false, transportFallback = false } = {}) {
  const browser = await chromium.launch({ headless: true });
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width, height } });
  const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8')
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '')
    .replace(/<link\b[^>]*>/g, '');
  await page.route('**/*', route => route.request().url() === 'http://homebody.test/'
    ? route.fulfill({ contentType: 'text/html', body: html }) : route.abort());
  await page.goto('http://homebody.test/');
  await page.addStyleTag({ path: path.join(staticDir, 'style.css') });
  await page.evaluate(({ fallback, transportFallback }) => {
    document.querySelector('main').hidden = false;
    document.querySelector('main').inert = false;
    document.querySelector('#owner-pair-panel').hidden = true;
    document.querySelector('#panel-robot').hidden = false;
    document.querySelector('#panel-robot').classList.add('active');
    window.requests = [];
    window.fetch = async (url, options) => {
      window.requests.push({ url, body: options?.body ? JSON.parse(options.body) : null });
      if (url === "/api/camera/frame") return { ok: true, status: 200, blob: () => new Promise(resolve => canvas.toBlob(resolve)) };
      return { ok: true, json: async () => ({ session_id: 'mock-session' }) };
    };
    const canvas = document.createElement('canvas');
    canvas.width = 640; canvas.height = 480;
    const draw = () => {
      canvas.getContext('2d').fillStyle = window.pauseVideoFrames ? '#000' : `hsl(${Date.now() % 360}, 40%, 25%)`;
      if (!window.pauseVideoFrames) canvas.getContext('2d').fillRect(0, 0, 640, 480);
    };
    draw();
    window.syntheticFrameTimer = setInterval(draw, 100);
    window.GstWebRTCAPI = class {
      registerConnectionListener() {}
      unregisterConnectionListener() {}
      unregisterProducersListener() {}
      registerProducersListener(listener) {
        listener.producerAdded({ id: 'mock', meta: { name: 'reachymini' } });
      }
      createConsumerSession() {
        const session = new EventTarget();
        (window.cameraSessions ||= []).push(session);
        session.streams = [canvas.captureStream()];
        session.connect = () => session.dispatchEvent(new Event(transportFallback ? 'closed' : 'streamsChanged'));
        session.close = () => {};
        return session;
      }
    };
    if (fallback) document.querySelector('#camera-viewer').requestFullscreen = async () => { throw new Error('Unsupported'); };
    window.refreshRobotPose = window.refreshBluetooth = window.refreshGpio = () => {};
    window.policy = { enabled: true, controlsEnabled: false, powerMode: 'awake', motorsEnabled: true };
  }, { fallback, transportFallback });
  const main = fs.readFileSync(path.join(staticDir, 'main.js'), 'utf8');
  // Exercise the real tab navigation and its camera privacy stop, not a copied handler.
  await page.addScriptTag({ content: main.slice(main.indexOf('function visibleTabButtons()'), main.indexOf('const initialTab =')) });
  await page.addScriptTag({ path: path.join(staticDir, 'camera.js') });
  await page.evaluate(() => window.ReachyCamera.setPolicy(window.policy));
  await page.locator('#camera-live-start').click();
  await page.waitForFunction(() => !document.querySelector('#camera-live-fullscreen').disabled);
  await page.waitForTimeout(150);
  return page;
}

test('HTTPS fallback shows real decoded frames and stops requests on privacy/Stop', async t => {
  const page = await setup(t, { width: 412, height: 915, transportFallback: true });
  assert.equal(await page.locator('#camera-live-status').textContent(), 'Live · HTTPS');
  const video = await page.locator('#reachy-camera-video').evaluate(v => ({ width: v.videoWidth, ready: v.readyState }));
  assert.equal(video.width, 640);
  assert.ok(video.ready >= 2);
  await page.locator('#camera-live-stop').click();
  const count = await page.evaluate(() => window.requests.filter(r => r.url === '/api/camera/frame').length);
  await page.waitForTimeout(600);
  assert.equal(await page.evaluate(() => window.requests.filter(r => r.url === '/api/camera/frame').length), count);
  assert.equal(await page.locator('#reachy-camera-video').evaluate(v => v.srcObject), null);
  await page.locator('#camera-live-start').click();
  await page.waitForFunction(() => document.querySelector('#camera-live-status').textContent === 'Live · HTTPS');
  await page.evaluate(() => window.ReachyCamera.setPolicy({ ...window.policy, powerMode: 'meeting' }));
  const privacyCount = await page.evaluate(() => window.requests.filter(r => r.url === '/api/camera/frame').length);
  await page.waitForTimeout(600);
  assert.equal(await page.evaluate(() => window.requests.filter(r => r.url === '/api/camera/frame').length), privacyCount);
});

for (const outcome of ['reject', 'resolve']) {
  test(`late play ${outcome} cannot overwrite Stop or a new camera connection`, async t => {
    const page = await setup(t);
    await page.evaluate(() => {
      window.ReachyCamera.stop();
      document.querySelector('#reachy-camera-video').play = () => new Promise((resolve, reject) => { window.pendingPlay = { resolve, reject }; });
    });
    await page.locator('#camera-live-start').click();
    await page.evaluate(() => {
      window.oldSession = window.cameraSessions.at(-1);
      window.ReachyCamera.stop('Stopped for privacy.');
    });
    await page.evaluate(outcome => {
      if (outcome === 'reject') window.pendingPlay.reject(new DOMException('Interrupted by pause', 'AbortError'));
      else window.pendingPlay.resolve();
    }, outcome);
    assert.equal(await page.locator('#camera-live-status').textContent(), 'Off');
    assert.equal(await page.locator('#camera-message').textContent(), 'Stopped for privacy.');
    await page.evaluate(() => { document.querySelector('#reachy-camera-video').play = function () { return HTMLMediaElement.prototype.play.call(this); }; });
    await page.locator('#camera-live-start').click();
    await page.waitForFunction(() => document.querySelector('#camera-live-status').textContent === 'Live');
    await page.evaluate(() => {
      window.oldSession.dispatchEvent(new Event('closed'));
      window.oldSession.dispatchEvent(new Event('error'));
      window.oldSession.dispatchEvent(new Event('streamsChanged'));
    });
    assert.equal(await page.locator('#camera-live-status').textContent(), 'Live');
    assert.equal(await page.locator('#camera-live-fullscreen').isEnabled(), true);
  });
}

test('duplicate stream events do not restart pending playback; genuine autoplay denial remains visible', async t => {
  const page = await setup(t);
  await page.evaluate(() => {
    window.ReachyCamera.stop();
    window.playCalls = 0;
    document.querySelector('#reachy-camera-video').play = () => {
      window.playCalls++;
      return new Promise((resolve, reject) => { window.pendingPlay = { resolve, reject }; });
    };
  });
  await page.locator('#camera-live-start').click();
  await page.evaluate(() => window.cameraSessions.at(-1).dispatchEvent(new Event('streamsChanged')));
  assert.equal(await page.evaluate(() => window.playCalls), 1);
  assert.equal(await page.locator('#camera-live-stop').isEnabled(), true);
  await page.evaluate(() => window.pendingPlay.reject(new DOMException('Autoplay denied', 'NotAllowedError')));
  assert.equal(await page.locator('#camera-live-status').textContent(), 'Paused');
  assert.match(await page.locator('#camera-message').textContent(), /NotAllowedError/);
});

for (const fallback of [false, true]) {
  for (const viewport of [{ width: 900, height: 700 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
    test(`${fallback ? 'fallback' : 'native'} fullscreen ${viewport.width}x${viewport.height}: disabled guidance, opt-in, release and exit`, async t => {
      const page = await setup(t, { ...viewport, fallback });
      await page.locator('#camera-live-fullscreen').click();
      await page.waitForFunction(fallback => fallback
        ? document.querySelector('#camera-viewer').classList.contains('camera-app-fullscreen')
        : document.fullscreenElement?.id === 'camera-viewer', fallback);
      assert.equal(await page.locator('#camera-control-overlay').isVisible(), true);
      assert.equal(await page.locator('#camera-control-fullscreen-exit').evaluate(el => getComputedStyle(el).color), 'rgb(244, 237, 225)');
      assert.equal(await page.locator('#camera-control-status').isVisible(), true);
      assert.match(await page.locator('#camera-control-status').textContent(), /Enable Camera movement controls/);
      assert.equal(await page.locator('#camera-control-center').isDisabled(), true);
      for (const id of ['camera-control-fullscreen-exit', 'camera-control-stop', 'camera-control-settings', 'camera-joystick']) {
        assert.equal(await page.locator(`#${id}`).evaluate(el => {
          const r = el.getBoundingClientRect();
          const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
          return r.x >= 0 && r.y >= 0 && r.right <= innerWidth && r.bottom <= innerHeight && el.contains(top);
        }), true, `${id} must be on-screen and unobstructed`);
      }
      if (process.env.TEST_CAMERA_SCREENSHOT_DIR) {
        await page.screenshot({ path: path.join(process.env.TEST_CAMERA_SCREENSHOT_DIR, `${fallback ? 'fallback' : 'native'}-${viewport.width}.png`) });
      }
      await page.locator('#camera-joystick').dispatchEvent('pointerdown', { pointerId: 1 });
      await page.locator('#camera-joystick').press('ArrowRight');
      assert.deepEqual(await page.evaluate(() => window.requests), []);
      // Settings navigation must exit either fullscreen path and not silently opt in.
      await page.locator('#camera-control-settings').click();
      await page.waitForFunction(() => document.querySelector('[data-tab="settings"]').getAttribute('aria-selected') === 'true');
      assert.equal(await page.evaluate(() => Boolean(document.fullscreenElement) || document.body.classList.contains('camera-app-fullscreen-active')), false);
      assert.equal(await page.locator('#panel-settings').isVisible(), true);
      assert.equal(await page.locator('#camera_controls_enabled').isChecked(), false);
      assert.equal(await page.locator('#camera_controls_enabled').evaluate(el => el.closest('details').open), true);
      assert.equal(await page.locator('#camera_controls_enabled').evaluate(el => el === document.activeElement), true);
      assert.deepEqual(await page.evaluate(() => window.requests), []);
      // Simulate a subsequently saved setting and fresh camera start.
      await page.locator('[data-tab="robot"]').click();
      await page.evaluate(() => {
        window.policy.controlsEnabled = true;
        window.policy.handedness = 'left';
        window.ReachyCamera.setPolicy(window.policy);
      });
      await page.locator('#camera-live-start').click();
      await page.waitForFunction(() => !document.querySelector('#camera-live-fullscreen').disabled);
      await page.locator('#camera-live-fullscreen').click();
      await page.waitForFunction(fallback => fallback ? document.body.classList.contains('camera-app-fullscreen-active') : Boolean(document.fullscreenElement), fallback);
      assert.equal(await page.locator('#camera-control-overlay').getAttribute('data-handedness'), 'left');
      await page.waitForFunction(() => !document.querySelector('#camera-control-center').disabled);
      assert.equal(await page.locator('#camera-control-center').isEnabled(), true);
      await page.locator('#camera-viewer').evaluate(el => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
  await page.waitForTimeout(100);
  const box = await page.locator('#camera-joystick').boundingBox();
      await page.mouse.move(box.x + box.width * .8, box.y + box.height / 2);
      await page.mouse.down();
      await page.waitForFunction(() => window.requests.some(r => r.url.endsWith('/move')));
      await page.mouse.up();
      await page.waitForFunction(() => window.requests.some(r => r.url.endsWith('/end')));
      // Loss of the stream must not hide the only fullscreen exit.
      await page.evaluate(() => window.ReachyCamera.stop());
      assert.equal(await page.locator('#camera-control-fullscreen-exit').isVisible(), true);
      await page.locator('#camera-control-fullscreen-exit').click();
      await page.waitForFunction(() => !document.fullscreenElement && !document.body.classList.contains('camera-app-fullscreen-active'));
      assert.equal(await page.locator('#camera-control-overlay').isVisible(), false);
    });
  }
}

for (const fallback of [false, true]) {
  test(`${fallback ? 'fallback' : 'native'} exit does not wait for an unresponsive control release`, async t => {
    const page = await setup(t, { fallback });
    await page.evaluate(() => { window.policy.controlsEnabled = true; window.ReachyCamera.setPolicy(window.policy); });
    await page.locator('#camera-live-fullscreen').click();
    await page.waitForFunction(() => document.fullscreenElement || document.body.classList.contains('camera-app-fullscreen-active'));
    await page.evaluate(() => {
      const fetch = window.fetch;
      window.fetch = (url, options) => url.endsWith('/end') ? new Promise(() => {}) : fetch(url, options);
    });
    await page.locator('#camera-joystick').press('ArrowRight');
    await page.waitForFunction(() => window.requests.some(r => r.url.endsWith('/move')));
    await page.locator('#camera-control-fullscreen-exit').click();
    await page.waitForFunction(() => !document.fullscreenElement && !document.body.classList.contains('camera-app-fullscreen-active'), null, { timeout: 2000 });
    assert.equal(await page.locator('#camera-joystick').getAttribute('data-active'), 'false');
  });
}

test('runtime gating, policy revocation, Stop and keyboard escape remain fail-closed', async t => {
  const page = await setup(t, { fallback: true });
  await page.evaluate(() => { window.policy.controlsEnabled = true; window.policy.motorsEnabled = false; window.ReachyCamera.setPolicy(window.policy); });
  assert.match(await page.locator('#camera-control-status').textContent(), /motors/);
  await page.locator('#camera-joystick').press('ArrowLeft');
  assert.deepEqual(await page.evaluate(() => window.requests), []);
  await page.evaluate(() => { window.policy.motorsEnabled = true; window.policy.robotBusy = true; window.ReachyCamera.setPolicy(window.policy); });
  assert.match(await page.locator('#camera-control-status').textContent(), /busy/);
  assert.equal(await page.locator('#camera-control-center').isDisabled(), true);
  await page.evaluate(() => { window.policy.robotBusy = false; window.ReachyCamera.setPolicy(window.policy); });
  await page.locator('#camera-joystick').press('ArrowRight');
  await page.waitForFunction(() => window.requests.some(r => r.url.endsWith('/move')));
  await page.evaluate(() => { window.policy.controlsEnabled = false; window.ReachyCamera.setPolicy(window.policy); });
  await page.waitForFunction(() => window.requests.some(r => r.url.endsWith('/end')));
  assert.equal(await page.locator('#camera-control-center').isDisabled(), true);
  await page.locator('#camera-control-stop').click();
  await page.waitForFunction(() => window.requests.some(r => r.url === '/api/robot/stop'));
  await page.locator('#camera-live-fullscreen').click();
  await page.waitForFunction(() => document.body.classList.contains('camera-app-fullscreen-active'));
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.body.classList.contains('camera-app-fullscreen-active'));
});

test('decoded video freeze holds movement and requires a new gesture on recovery', async t => {
  const page = await setup(t, { width: 390, height: 844 });
  await page.evaluate(() => { window.policy.controlsEnabled = true; window.ReachyCamera.setPolicy(window.policy); });
  await page.waitForFunction(() => !document.querySelector('#camera-control-center').disabled);
  await page.locator('#camera-viewer').evaluate(el => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
  await page.waitForTimeout(100);
  const box = await page.locator('#camera-joystick').boundingBox();
  await page.mouse.move(box.x + box.width * .75, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForFunction(() => window.requests.some(r => r.url.endsWith('/move')));
  await page.evaluate(() => { window.pauseVideoFrames = true; });
  await page.waitForFunction(() => document.querySelector('#camera-feed-health').dataset.stale === 'true');
  assert.equal(await page.locator('#camera-joystick').getAttribute('data-active'), 'false');
  assert.equal(await page.locator('#camera-control-center').isDisabled(), true);
  assert.ok(await page.evaluate(() => window.requests.some(r => r.url.endsWith('/end'))));
  const count = await page.evaluate(() => window.requests.filter(r => r.url.endsWith('/move')).length);
  await page.evaluate(() => { window.pauseVideoFrames = false; });
  await page.waitForFunction(() => document.querySelector('#camera-feed-health').dataset.stale === 'false');
  await page.waitForTimeout(300);
  assert.equal(await page.evaluate(() => window.requests.filter(r => r.url.endsWith('/move')).length), count);
  await page.mouse.up();
});

test('Stop is sent immediately even when gesture release never responds', async t => {
  const page = await setup(t);
  await page.evaluate(() => {
    window.policy.controlsEnabled = true; window.ReachyCamera.setPolicy(window.policy);
    const fetch = window.fetch;
    window.fetch = (url, options) => url.endsWith('/end') ? new Promise(() => {}) : fetch(url, options);
  });
  await page.locator('#camera-joystick').press('ArrowRight');
  await page.waitForFunction(() => window.requests.some(r => r.url.endsWith('/move')));
  await page.locator('#camera-control-stop').click();
  await page.waitForFunction(() => window.requests.some(r => r.url === '/api/robot/stop'), null, { timeout: 1000 });
});

test('late movement rejection cannot cancel the next pointer gesture', async t => {
  const page = await setup(t);
  await page.evaluate(() => {
    window.policy.controlsEnabled = true; window.ReachyCamera.setPolicy(window.policy);
    const fetch = window.fetch;
    let intercepted = false;
    window.fetch = (url, options) => {
      if (url.endsWith('/move') && !intercepted) {
        intercepted = true;
        return new Promise((resolve, reject) => { window.rejectOldMove = reject; });
      }
      return fetch(url, options);
    };
  });
  await page.locator('#camera-viewer').evaluate(el => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
  await page.waitForTimeout(100);
  const box = await page.locator('#camera-joystick').boundingBox();
  await page.mouse.move(box.x + box.width * .75, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForFunction(() => Boolean(window.rejectOldMove));
  await page.mouse.up();
  await page.waitForFunction(() => !document.querySelector('#camera-control-center').disabled);
  await page.mouse.down();
  await page.waitForFunction(() => window.requests.some(r => r.url.endsWith('/move')));
  const ends = await page.evaluate(() => window.requests.filter(r => r.url.endsWith('/end')).length);
  await page.evaluate(() => window.rejectOldMove(new Error('old request rejected')));
  await page.waitForTimeout(120);
  assert.equal(await page.locator('#camera-joystick').getAttribute('data-active'), 'true');
  assert.equal(await page.evaluate(() => window.requests.filter(r => r.url.endsWith('/end')).length), ends);
  await page.mouse.up();
});

test('Center cannot start after Stop while authorization is delayed', async t => {
  const page = await setup(t);
  await page.evaluate(() => {
    window.policy.controlsEnabled = true; window.ReachyCamera.setPolicy(window.policy);
    const fetch = window.fetch;
    window.fetch = (url, options) => url.endsWith('/session')
      ? new Promise(resolve => { window.authorizeCenter = () => resolve({ ok: true, json: async () => ({ session_id: 'late-center' }) }); })
      : fetch(url, options);
  });
  await page.locator('#camera-control-center').click();
  await page.waitForFunction(() => Boolean(window.authorizeCenter));
  assert.equal(await page.locator('#camera-control-center').isDisabled(), true);
  await page.locator('#camera-control-stop').click();
  await page.evaluate(() => window.authorizeCenter());
  await page.waitForTimeout(150);
  assert.equal(await page.evaluate(() => window.requests.some(r => r.url.endsWith('/center'))), false);
  assert.ok(await page.evaluate(() => window.requests.some(r => r.body?.session_id === 'late-center' && r.url.endsWith('/end'))));
});

test('HTTPS backup defaults to Careful speed and phone controls have unobstructed thumb targets', async t => {
  const page = await setup(t, { width: 390, height: 844, transportFallback: true });
  await page.evaluate(() => { window.policy.controlsEnabled = true; window.ReachyCamera.setPolicy(window.policy); });
  assert.equal(await page.locator('#camera-control-speed').inputValue(), '0.3');
  await page.locator('#camera-viewer').evaluate(el => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
  await page.waitForTimeout(100);
  for (const id of ['camera-control-stop', 'camera-control-center', 'camera-control-speed', 'camera-joystick']) {
    assert.equal(await page.locator(`#${id}`).evaluate(el => {
      const r = el.getBoundingClientRect();
      return r.height >= 44 && el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
    }), true, id);
  }
  await page.locator('#camera-joystick').press('ArrowRight');
  await page.waitForFunction(() => window.requests.some(r => r.url.endsWith('/move')));
  assert.ok(await page.evaluate(() => Math.abs(window.requests.find(r => r.url.endsWith('/move')).body.pan - .165) < .0001));
  if (process.env.TEST_CAMERA_SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.TEST_CAMERA_SCREENSHOT_DIR, 'phone-inline.png'), fullPage: true });
});

test('track arrival alone is not Live; no decoded video falls back after bounded negotiation', async t => {
  const page = await setup(t);
  await page.evaluate(() => {
    window.ReachyCamera.stop();
    document.querySelector('#reachy-camera-video').play = () => Promise.resolve();
    document.querySelector('#reachy-camera-video').requestVideoFrameCallback = () => 9999;
  });
  await page.clock.install();
  await page.locator('#camera-live-start').click();
  await page.waitForFunction(() => document.querySelector('#camera-message').textContent.includes('waiting for a decoded frame'));
  assert.equal(await page.locator('#camera-live-status').textContent(), 'Connecting');
  assert.equal(await page.locator('#camera-control-center').isDisabled(), true);
  await page.clock.runFor(14900);
  assert.equal(await page.evaluate(() => window.requests.filter(r => r.url === '/api/camera/frame').length), 0);
  await page.clock.runFor(200);
  await page.waitForFunction(() => window.requests.some(r => r.url === '/api/camera/frame'));
  await page.locator('#camera-live-stop').click();
});

test('connection details use real peer stats references and stay private to the browser', async t => {
  const page = await setup(t);
  await page.evaluate(() => {
    const peer = new EventTarget();
    Object.assign(peer, { iceConnectionState: 'connected', connectionState: 'connected', getReceivers: () => [],
      getStats: async () => new Map([
        ['transport', { id: 'transport', type: 'transport', dtlsState: 'connected', selectedCandidatePairId: 'pair' }],
        ['pair', { id: 'pair', type: 'candidate-pair', state: 'succeeded', localCandidateId: 'local', remoteCandidateId: 'remote', currentRoundTripTime: .042 }],
        ['local', { type: 'local-candidate', candidateType: 'host', address: 'private-local-address' }],
        ['remote', { type: 'remote-candidate', candidateType: 'host', address: 'private-remote-address' }],
        ['codec', { type: 'codec', mimeType: 'video/VP8' }],
        ['video', { type: 'inbound-rtp', kind: 'video', codecId: 'codec', framesPerSecond: 30, framesDecoded: 120, packetsReceived: 220, jitterBufferDelay: 1, jitterBufferEmittedCount: 100, freezeCount: 0 }],
      ])
    });
    window.cameraSessions.at(-1).rtcPeerConnection = peer;
    window.cameraSessions.at(-1).dispatchEvent(new Event('rtcPeerConnectionChanged'));
  });
  await page.waitForFunction(() => document.querySelector('#camera-diag-video').textContent.includes('video/VP8'));
  assert.match(await page.locator('#camera-diag-network').textContent(), /42 ms/);
  assert.match(await page.locator('#camera-diag-video').textContent(), /120 decoded/);
  assert.match(await page.locator('#camera-diag-ice').textContent(), /DTLS connected/);
  assert.equal(await page.evaluate(() => document.body.textContent.includes('private-local-address')), false);
  assert.equal(await page.evaluate(() => window.requests.length), 0);
});


test('delayed Center authorization cannot dispatch against stale decoded video', async t => {
  const page = await setup(t);
  await page.evaluate(() => {
    window.policy.controlsEnabled = true; window.ReachyCamera.setPolicy(window.policy);
    const fetch = window.fetch;
    window.fetch = (url, options) => url.endsWith('/session')
      ? new Promise(resolve => { window.authorizeCenter = () => resolve({ ok: true, json: async () => ({ session_id: 'stale-center' }) }); })
      : fetch(url, options);
  });
  await page.locator('#camera-control-center').click();
  await page.waitForFunction(() => Boolean(window.authorizeCenter));
  await page.evaluate(() => { window.pauseVideoFrames = true; });
  await page.waitForFunction(() => document.querySelector('#camera-feed-health').dataset.stale === 'true');
  await page.evaluate(() => window.authorizeCenter());
  await page.waitForTimeout(150);
  assert.equal(await page.evaluate(() => window.requests.some(r => r.url.endsWith('/center'))), false);
  assert.ok(await page.evaluate(() => window.requests.some(r => r.url.endsWith('/end') && r.body?.session_id === 'stale-center')));
});
