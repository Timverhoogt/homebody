// Run: NODE_PATH=/path/to/node_modules node --test tests/camera_ui.browser.cjs
// All streams and HTTP responses are synthetic; never contacts a robot.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const staticDir = path.join(__dirname, '../homebody/static');

async function setup(t, { width = 900, height = 700, fallback = false } = {}) {
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
  await page.evaluate(({ fallback }) => {
    document.querySelector('main').hidden = false;
    document.querySelector('main').inert = false;
    document.querySelector('#owner-pair-panel').hidden = true;
    document.querySelector('#panel-robot').hidden = false;
    document.querySelector('#panel-robot').classList.add('active');
    window.requests = [];
    window.fetch = async (url, options) => {
      window.requests.push({ url, body: options?.body ? JSON.parse(options.body) : null });
      return { ok: true, json: async () => ({ session_id: 'mock-session' }) };
    };
    const canvas = document.createElement('canvas');
    canvas.width = 640; canvas.height = 480;
    canvas.getContext('2d').fillRect(0, 0, 640, 480);
    const stream = canvas.captureStream();
    window.GstWebRTCAPI = class {
      registerConnectionListener() {}
      unregisterConnectionListener() {}
      unregisterProducersListener() {}
      registerProducersListener(listener) {
        listener.producerAdded({ id: 'mock', meta: { name: 'reachymini' } });
      }
      createConsumerSession() {
        const session = new EventTarget();
        session.streams = [stream];
        session.connect = () => session.dispatchEvent(new Event('streamsChanged'));
        session.close = () => {};
        return session;
      }
    };
    if (fallback) document.querySelector('#camera-viewer').requestFullscreen = async () => { throw new Error('Unsupported'); };
    window.refreshRobotPose = window.refreshBluetooth = window.refreshGpio = () => {};
    window.policy = { enabled: true, controlsEnabled: false, powerMode: 'awake', motorsEnabled: true };
  }, { fallback });
  const main = fs.readFileSync(path.join(staticDir, 'main.js'), 'utf8');
  // Exercise the real tab navigation and its camera privacy stop, not a copied handler.
  await page.addScriptTag({ content: main.slice(main.indexOf('function visibleTabButtons()'), main.indexOf('const initialTab =')) });
  await page.addScriptTag({ path: path.join(staticDir, 'camera.js') });
  await page.evaluate(() => window.ReachyCamera.setPolicy(window.policy));
  await page.locator('#camera-live-start').click();
  await page.waitForFunction(() => !document.querySelector('#camera-live-fullscreen').disabled);
  return page;
}

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
      assert.equal(await page.locator('#camera-control-center').isEnabled(), true);
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
