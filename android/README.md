# Homebody for Android

A small native Android companion (Java + system WebView) that opens your existing private
Homebody HTTPS dashboard. It does **not** run the Python robotics backend and duplicates no
dashboard UI. Package `io.github.timverhoogt.homebody`, version 0.1.0 (code 1), minSdk 26,
target/compile SDK 36. Icon generated from `homebody/static/icon-maskable-512.png`
(`tools/generate_icons.py`).

## Architecture

| File | Role |
|---|---|
| `policy/DashboardOrigin.java` | Strict HTTPS origin parser + exact origin compare (pure Java) |
| `policy/NavigationPolicy.java` | Top-level / frame / subresource decisions (pure Java) |
| `policy/BackPolicy.java`, `ConnectionProblem.java`, `DashboardSession.java` | Back, error categories, remembered tab token |
| `web/WebViewSetup.java` | Locked-down WebView + service-worker restrictions |
| `web/DashboardWebViewClient.java`, `DashboardChromeClient.java`, `BlockedResponse.java` | Applies policies; TLS/permission refusal |
| `MainActivity.java` | Lifecycle, error panel, back/exit dialog, fullscreen |
| `SettingsActivity.java`, `DashboardPrefs.java`, `SiteData.java`, `ErrorText.java` | Server address, clear site data |
| `apktool/.../ApkTool.java` | Host tool: package, sign (apksig v2+v3), verify |

### Origin and navigation policy
- Default `https://timverhoogtnano.tail51823f.ts.net/`; editable under **Back → Server settings**
  (exit dialog) or the **Server settings** button on the error screen.
- Accepted input: `https://host[:port][/]` only. Rejected: other schemes, userinfo, `?query`,
  `#fragment`, paths, whitespace/controls/backslash/non-ASCII (use `xn--`), trailing dots, invalid
  labels, IPv6 literals, non-canonical IPv4 (`0x7f.0.0.1`, `0177…`, `127.1`), ports outside 1–65535.
  `:443` and case are canonicalised. Comparison is the exact scheme + host + port; no
  prefix/suffix allowlist.
- Top-level navigation stays on that origin. Other http(s) links open in the system browser
  **only after a user gesture**; `javascript:`, `file:`, `content:`, `intent:`, `data:`, `market:`
  etc. are blocked. Cross-origin iframes and subresources (and service-worker fetches) get an
  empty 403. POST navigations/redirects are covered by request interception; a foreign committed
  document is stopped. WebSockets and WebRTC media cannot be intercepted by WebView; the camera
  uses same-origin `wss://` signaling and direct WebRTC to the robot.
- No JS-to-native interface, no file/content/universal file access, mixed content never, no
  multiple windows, third-party cookies off, console output not logged, no app logging at all,
  WebView metrics opted out, Safe Browsing off (single private origin), WebView debugging off,
  not `debuggable`.
- TLS errors are always cancelled (no proceed path); cleartext disabled, system CAs only.
- Every WebView permission request (camera, mic, MIDI, protected media), geolocation and file
  chooser is denied. The app requests only `INTERNET`: robot viewing is receive-only WebRTC.

### Lifecycle / privacy
- `onPause`: leave fullscreen, navigate to `about:blank` (page unloads → camera WebRTC, sockets,
  polls end; the dashboard's own `visibilitychange`/`pagehide` camera stop also fires), hide and
  pause the WebView. `onStop` (background, screen-off, Settings open): **destroy** the WebView.
  `onResume`: reload the dashboard (last simple tab token only, e.g. `#robot`). Reopening never
  starts the camera; that stays an explicit in-dashboard opt-in.
- Owner session stays in the dashboard's Secure/HttpOnly/SameSite=Strict cookie (CookieManager);
  pairing/CSRF are unchanged server-side. No tokens or secrets are built in.
- Backup and device transfer excluded for all domains. API 33+: recents screenshot disabled.
- Back walks dashboard history; at the root or on an error it asks *Leave / Stay / Server settings*.
- Errors: host not found (offline / Tailscale disconnected hint for `.ts.net` and 100.64/10),
  not answering, server 5xx, TLS refused, blocked navigation, renderer crash — with Retry.
- Settings → *Clear sign-in and site data* removes cookies, web storage and (next start) HTTP
  cache on the phone. Revoke the device on the robot with *Forget this device* in the dashboard.

## Install and use
1. Sideload `homebody-release.apk` (allow "install unknown apps" for your file manager or use
   `adb install`). Verify the SHA-256 below first.
2. Install the Tailscale app on the phone and connect it to the robot's tailnet (tailnet URLs
   are unreachable otherwise). The robot must be exposed with `tailscale serve` (see main README).
3. Open Homebody and pair in the dashboard with a one-time code from
   `python3 -m homebody.owner_auth pair --origin https://<robot>.ts.net` on the robot.
4. Another robot: Back → Server settings, enter its HTTPS origin, Save.

## Toolchain and why it is not the standard Android SDK
The Google SDK Platform/Build-Tools packages require interactive acceptance of the Android SDK
License; that acceptance was **not** given or automated. Instead this build uses only
open-source tooling under its respective licenses:
- Temurin JDK 21.0.12.1 and Gradle 9.8.1 (SHA-256 verified; wrapper pins the distribution sum).
- `org.robolectric:android-all:16-robolectric-13921718` (AOSP-built Android 16 framework, Apache-2.0,
  Maven Central) as compile/library jar and aapt2 framework include (contains `resources.arsc`).
- From Google Maven (artifacts AGP itself downloads): aapt2 9.4.1-15978811 (Apache-2.0),
  r8/d8 9.4.28, apksig 9.4.1 (Apache-2.0, the library behind `apksigner`).
- Tests: JUnit 4.13.2, Robolectric 4.17 (MIT) with android-all-instrumented for SDK 36 and 26.

Pipeline (`app/build.gradle`, no AGP): aapt2 compile/link → javac (Java 11) → d8 `--min-api 26`
→ package (fixed timestamps) → apksig v2+v3 → verify (+ alignment) → `aapt2 dump`. Source layout
follows AGP conventions; switching to AGP after accepting the SDK license is straightforward
(manifest currently carries `package`/`uses-sdk` that AGP would move into build config).

```sh
. /home/timv/.hermes/cache/scratch/homebody-android-toolchain/env.sh   # JAVA_HOME, GRADLE_USER_HOME
cd android
./gradlew :app:test
./gradlew -Phomebody.signingDir=<private dir outside repo> :app:createReleaseKeystore   # once
./gradlew -Phomebody.signingDir=<private dir outside repo> :app:assembleRelease
```
Outputs in `app/build/outputs/apk/release/`: `homebody-release.apk`,
`signature-verification.txt`, `manifest-badging.txt`, `manifest-xmltree.txt`.

## Signing key — keep it
Release signing uses a private local PKCS12 key (RSA 3072, `CN=Homebody Android (private
sideload build)`), created with 0700/0600 permissions outside the repo, never committed
(`.gitignore` blocks `*.p12`, `*.password`). **The scratch directory is temporary**: the key
has been preserved at `/home/timv/.local/share/homebody/android-signing` and APKs at
`/home/timv/.local/share/homebody/android-builds`. Android only installs updates signed by the
same key; if it is lost, users must uninstall (losing the Owner cookie) and re-pair.

## Verified results (2026-10-12)
- Parent-run final-source clean build `clean :app:test :app:assembleRelease`: BUILD SUCCESSFUL.
  The distributed APK was rebuilt after the final source changes; its SHA-256 is
  `39ab34d5c8a5b46911811764856b871c95189889df3b19355c80c560c2b005aa`.
- apksig verify: verified, v1 false, v2 true, v3 true, 1 signer, cert SHA-256
  `e54751757dd82eaa4397fe1fa53a7ea8ff151be9c3581592bd94cb909a04fb59`, stored entries 4-byte aligned.
- aapt2: minSdk 26, targetSdk 36, sole permission `android.permission.INTERNET`,
  `allowBackup=false`, `usesCleartextTraffic=false`, only `MainActivity` exported (launcher),
  `SettingsActivity` not exported, no services/receivers/providers.
- Latest test run: **90 tests, 0 failures, 0 skipped** (MainActivityTest 48, SettingsActivityTest 12,
  StaticPolicyTest 8, DashboardOriginTest 12, NavigationPolicyTest 6,
  SessionBackAndErrorPolicyTest 4); Robolectric tests run on SDK 26 and 36. Fixtures are synthetic;
  no network, robot or production route was used.
- Final rebuild includes the API-33 compatibility guard and the non-deprecated apksig signer
  builder. Build/test reports and the signed APK were checked independently by the parent.

## Limitations
- Not run on a physical Android device or emulator. Still needed: install, Tailscale reachability,
  Owner pairing, camera WebRTC playback and fullscreen, pause/screen-off/background teardown
  observed from the robot side, back/exit, TLS-error and offline screens, edge-to-edge layout.
- Built against an AOSP implementation jar, not the official SDK stub jar; hidden APIs are not
  compile-time blocked (code uses public APIs only, guarded by review/tests). No lint run.
- Android 8–12 may show a dashboard snapshot in Recents (best-effort hiding on pause only).
- WebSockets/WebRTC are outside WebView request interception. IPv6-literal origins unsupported.
  Private CAs/self-signed robot certificates are refused by design.
