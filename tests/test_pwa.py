from __future__ import annotations

import json
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "homebody" / "static"


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:24]
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_pwa_manifest_declares_standalone_root_scoped_app_and_icons() -> None:
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text())

    assert manifest["name"] == "Homebody"
    assert manifest["start_url"] == "/#dashboard"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    icons = {item["src"]: item for item in manifest["icons"]}
    assert icons["/static/icon-192.png"]["sizes"] == "192x192"
    assert icons["/static/icon-512.png"]["sizes"] == "512x512"
    assert icons["/static/icon-maskable-512.png"]["purpose"] == "maskable"
    assert _png_size(STATIC / "icon-192.png") == (192, 192)
    assert _png_size(STATIC / "icon-512.png") == (512, 512)
    assert _png_size(STATIC / "icon-maskable-512.png") == (512, 512)


def test_service_worker_caches_only_the_app_shell_and_bypasses_api() -> None:
    worker = (STATIC / "service-worker.js").read_text()

    assert 'url.pathname.startsWith("/api/")' in worker
    assert 'request.method !== "GET"' in worker
    assert 'caches.match("/")' in worker
    assert "/static/main.js" in worker
    assert "/static/camera.js" in worker
    assert "/static/gstwebrtc-api.js" in worker
    assert "/static/style.css" in worker


def test_dashboard_exposes_native_install_prompt_with_http_fallback() -> None:
    html = (STATIC / "index.html").read_text()
    javascript = (STATIC / "main.js").read_text()
    backend = (ROOT / "homebody" / "main.py").read_text()

    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert 'id="install-button"' in html
    assert "Add to Home screen" in html
    assert 'window.addEventListener("beforeinstallprompt"' in javascript
    assert 'navigator.serviceWorker.register("/service-worker.js"' in javascript
    assert 'window.isSecureContext' in javascript
    assert '@self.settings_app.get("/manifest.webmanifest"' in backend
    assert '@self.settings_app.get("/service-worker.js"' in backend
    assert '"Service-Worker-Allowed": "/"' in backend


def test_dashboard_exposes_bounded_agent_status_approval_and_stop_controls() -> None:
    html = (STATIC / "index.html").read_text()
    javascript = (STATIC / "main.js").read_text()

    for element_id in (
        "agent-profile-badge",
        "agent-capabilities",
        "agent-current-task",
        "agent-pending-approval",
        "agent-stop-button",
        "agent-approval-sheet",
        "agent-approval-arguments",
        "agent-approve-button",
        "agent-activity",
    ):
        assert f'id="{element_id}"' in html
    assert 'fetch("/api/agent/profile"' in javascript
    assert 'fetch("/api/agent/stop"' in javascript
    assert 'fetchWithTimeout("/api/agent/activity"' in javascript
    assert 'fetchWithTimeout("/api/agent/pending-approval"' in javascript
    assert 'fetch("/api/agent/approve-pending"' in javascript
    assert 'document.querySelector(".agent-card").hidden = kidsActive || kidsLocked' in javascript


def test_v44_ui_uses_dedicated_agent_workspace_and_contextual_offers() -> None:
    html = (STATIC / "index.html").read_text()
    css = (STATIC / "style.css").read_text()
    script = (STATIC / "main.js").read_text()
    worker = (STATIC / "service-worker.js").read_text()

    assert 'id="tab-agent"' in html
    assert 'id="panel-agent"' in html
    assert html.index('id="panel-agent"') < html.index('id="panel-kids"')
    assert 'id="presence-enabled"' in html
    assert 'id="presence-acknowledgement-enabled"' in html
    assert "Silent acknowledgement only · no proactive speech" in html
    assert 'id="initiative-policy-enabled"' in html
    assert 'id="initiative-mode"' in html
    assert 'id="initiative-quiet-hours-start"' in html
    assert 'id="initiative-hourly-budget"' in html
    assert 'id="contextual-offers-enabled"' in html
    assert 'id="contextual-offer-response-window"' in html
    assert 'id="contextual-offer-explanation"' in html
    assert 'id="contextual-offer-yes"' in html
    assert 'id="contextual-offer-no"' in html
    assert "Offers never execute an action" in html
    assert 'id="shared-physical-context-enabled"' in html
    assert 'id="presentation-window-seconds"' in html
    assert 'id="presentation-start"' in html
    assert 'id="presentation-stop"' in html
    assert "zero frames retained" in html
    assert "no cloud vision during this window" in html
    assert '<details class="control-group precision-group disclosure">' in html
    assert '<details class="card bluetooth-card disclosure-card">' in html
    assert '<details id="install-card" class="card install-card disclosure-card">' in html
    assert 'grid-template-columns: repeat(6, 1fr)' in css
    assert '.presence-status-grid' in css
    assert '.initiative-status-grid' in css
    assert '.presentation-status-grid' in css
    assert '.agent-step-list' in css
    assert 'proactive_presence_enabled' in script
    assert 'presence_acknowledgement_enabled' in script
    assert 'initiative_policy_enabled' in script
    assert 'initiative_quiet_hours_start' in script
    assert 'contextual_offers_enabled' in script
    assert 'shared_physical_context_enabled' in script
    assert 'fetch(`/api/presentation/${active ? "start" : "stop"}`' in script
    assert 'fetch("/api/initiative/offers/respond"' in script
    assert '"X-Reachy-Adult-UI": "unlocked"' in script
    assert 'if (!initiativeEditActive)' in script
    assert '$("initiative-badge").textContent = "Offline"' in script
    assert 'homebody-shell-v55' in worker


def test_shell_versions_agree_between_page_and_service_worker() -> None:
    import re

    html = (STATIC / "index.html").read_text()
    worker = (STATIC / "service-worker.js").read_text()
    cache_version = re.search(r'homebody-shell-v(\d+)"', worker)
    assert cache_version is not None
    page_versions = set(re.findall(r'/static/[\w.-]+\?v=(\d+)', html))
    worker_versions = set(re.findall(r'/static/[\w.-]+\?v=(\d+)', worker))
    # A deploy that bumps one place but not another serves new HTML with stale cached JS.
    assert page_versions == worker_versions == {cache_version.group(1)}


def test_service_worker_shell_has_no_fragment_duplicates() -> None:
    import re

    worker = (STATIC / "service-worker.js").read_text()
    shell = re.search(r"const APP_SHELL = \[(.*?)\];", worker, re.S)
    assert shell is not None
    entries = re.findall(r'"([^"]+)"', shell.group(1))
    # cache.addAll rejects requests that differ only by #fragment.
    assert len({entry.split("#", 1)[0] for entry in entries}) == len(entries)


def test_service_worker_never_caches_error_responses() -> None:
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("node is not installed")
    harness = r"""
const fs = require("fs");
const vm = require("vm");
const puts = [];
const listeners = {};
const cache = { put: (key, value) => { puts.push([String(key), value.status]); return Promise.resolve(); },
                addAll: () => Promise.resolve() };
const sandbox = {
  URL,
  console,
  self: { location: { origin: "https://reachy.local" }, addEventListener: (name, fn) => { listeners[name] = fn; },
          skipWaiting() {}, clients: { claim() {} } },
  caches: { open: () => Promise.resolve(cache), match: () => Promise.resolve(undefined),
            keys: () => Promise.resolve([]) },
  fetch: () => Promise.resolve({ ok: false, status: 502, clone() { return this; } }),
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), sandbox);
async function dispatch(url, mode) {
  let responded;
  listeners.fetch({ request: { method: "GET", url, mode }, respondWith: (p) => { responded = p; } });
  await responded;
  await new Promise((resolve) => setTimeout(resolve, 10));
}
(async () => {
  await dispatch("https://reachy.local/", "navigate");
  await dispatch("https://reachy.local/static/main.js?v=1", "no-cors");
  process.stdout.write(JSON.stringify(puts));
})();
"""
    result = subprocess.run(
        [node, "-e", harness, str(STATIC / "service-worker.js")],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert json.loads(result.stdout) == []


def test_dashboard_polling_backs_off_pauses_and_times_out() -> None:
    javascript = (STATIC / "main.js").read_text()
    assert "setInterval(refreshStatus" not in javascript
    assert 'document.addEventListener("visibilitychange"' in javascript
    assert "Math.min(1500 * 2 ** statusPollFailures, 15000)" in javascript
    assert "controller.abort()" in javascript
    assert "if (agentActivityPending) return;" in javascript


def test_kids_locked_tabs_cannot_be_reached_by_keyboard_or_history() -> None:
    javascript = (STATIC / "main.js").read_text()
    assert "const target = requested && !requested.hidden ? requested : visibleTabButtons()[0];" in javascript
    assert "const visible = visibleTabButtons();" in javascript


def test_power_actions_report_failures_and_countdowns_are_not_live_regions() -> None:
    javascript = (STATIC / "main.js").read_text()
    html = (STATIC / "index.html").read_text()
    assert javascript.count("if (!response.ok) throw new Error(await responseDetail(response));") >= 2
    assert '<section class="card kids-hero" aria-live' not in html
    assert '<div class="presentation-status-grid" aria-live' not in html


def test_initiative_card_explains_decisions_and_exposes_learned_preferences_safely() -> None:
    html = (STATIC / "index.html").read_text()
    javascript = (STATIC / "main.js").read_text()
    for element_id in (
        "initiative-explanation",
        "initiative-preferences",
        "initiative-preferences-reset",
        "contextual-offer-later",
    ):
        assert f'id="{element_id}"' in html
    assert "Why did Reachy do that?" in html
    assert "never learns new permissions" in html
    assert 'respondToContextualOffer("later")' in javascript
    assert 'fetchWithTimeout(path, {' in javascript
    assert '"/api/initiative/preferences/reset"' in javascript
    # Preference labels and explanations come from the runtime: render them as text only.
    renderer = javascript[javascript.index("function renderInitiativePreferences") :]
    renderer = renderer[: renderer.index("async function updateInitiativePreferences")]
    assert "innerHTML" not in renderer
    assert "textContent" in renderer


def test_pre_rename_browser_storage_is_carried_over_once() -> None:
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("node is not installed")
    main = (STATIC / "main.js").read_text(encoding="utf-8")
    start = main.index("// Carry browser settings saved before the Homebody rename")
    block = main[start : main.index("let agentRunId = ", start)]
    harness = r"""
const vm = require("vm");
function fakeStorage(entries) {
  const data = new Map(Object.entries(entries));
  return {
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => { data.set(key, String(value)); },
    removeItem: (key) => { data.delete(key); },
    dump: () => Object.fromEntries(data),
  };
}
const window = {
  localStorage: fakeStorage({
    "reachy-hermes-tab": "robot",
    "reachy-hermes-kids-profile": "{\"activity\":\"story\"}",
    "homebody-tab": "settings",
  }),
  sessionStorage: fakeStorage({ "reachy-hermes-announcement-draft": "Dinner is ready" }),
};
vm.runInNewContext(process.argv[1], { window });
process.stdout.write(JSON.stringify({ local: window.localStorage.dump(), session: window.sessionStorage.dump() }));
"""
    result = subprocess.run([node, "-e", harness, block], capture_output=True, text=True, check=True)
    stored = json.loads(result.stdout)

    # A value saved under the new key wins; the old keys are always dropped.
    assert stored["local"] == {"homebody-tab": "settings", "homebody-kids-profile": '{"activity":"story"}'}
    assert stored["session"] == {"homebody-announcement-draft": "Dinner is ready"}
    assert '"reachy-hermes-' not in main[: start] + main[main.index("let agentRunId = ", start) :]
