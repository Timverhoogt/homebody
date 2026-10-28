# Desloppify cleanup: first implementation pass

## Intended outcome

Make Homebody easier to maintain without weakening Owner/Kids privacy, speech approval,
Stop/cancellation, or motor safety. Scanner scores are navigation aids, not acceptance criteria.
This pass changes a developer diagnostic's HTTP boundary and extracts one existing bridge
state machine. It does not publish, deploy, restart, or exercise a physical device.

Baseline: `d36ef5f9991c63d8b48dee549d2e11125899f860` (GitHub main, 2026-10-28).

## Implemented

### MCP diagnostic transport

`tools/mcp_check.py` previously used urllib's default opener. A loopback reproduction using
only a synthetic token demonstrated that a 302 redirect forwarded Authorization to another
origin, and that an inherited HTTP proxy received the token.

The diagnostic now:

- Rejects redirects, including same-origin redirects: the operator must select the direct endpoint.
- Disables inherited HTTP proxies for this credential-bearing diagnostic only.
- Bounds response bodies to 1 MiB plus one overflow-detection byte.
- Requires JSON-RPC 2.0, the exact request ID/type and an object result.
- Reports bounded local failures instead of echoing arbitrary upstream error messages.

It retains explicit HTTP LAN support, status/authentication guidance, the read-only default,
and opt-in `--say` / `--emotion` behavior. No server authorization policy changed.
Real HTTP regression tests cover redirect codes 301/302/303/307/308, direct authenticated
success, proxies, response size, envelope validation and upstream diagnostic privacy.

### First bridge extraction

`RealtimeResponseLifecycle` now lives in `companion/realtime_lifecycle.py`, independent of
bridge routes/providers. The bridge re-exports it for compatibility and preserves direct-script
imports. The class AST is identical to the baseline: this is an extraction, not a change to
barge-in, generation, pending-send or cancellation semantics.

New focused tests cover single-create serialization, each terminal event, stale generations,
barge-in and close/join behavior. Existing bridge and proxy continuation tests remain intact.

## Security triage

Do not interpret scanner warning labels as verified vulnerabilities:

- Four JavaScript high-labelled secrets are fetch's `credentials: 'same-origin'` options.
- The Python high-labelled secret is an explicit isolated benchmark key.
- `companion/household_smoke.py` prints a constructed allowlisted check result, not its input
  credential or arbitrary server response. Its own no-proxy/no-redirect boundary is retained.
- The ESPHome `0.0.0.0` bind is a deliberate LAN service, not automatically a vulnerability.
- Unpinned recorded-move dataset downloads remain a supply-chain/reproducibility follow-up;
  changing motion assets needs its own compatibility and physical acceptance scope.
- JUnit parsing consumes local check output. It is not an exposed arbitrary-XML endpoint;
  parser hardening is lower priority than credential handling.
- GPIO acceptance uses an operator-selected URL and sends no bearer credential. Its transport
  deserves a separate bounded-response/redirect review, but is not this MCP credential leak.
- Low-severity assert, subprocess-import and subprocess-execution warnings require contextual
  review; neither indiscriminate suppression nor blanket rewrites are appropriate.

## Verification and limitations

- Focused suite: **55 passed** on the final edited source.
- Repository Ruff check and `git diff --check`: passed.
- Direct-script bridge `--help` import: passed without constructing a robot.
- Development-only wheel built; all **175 `homebody/` resources** match candidate source bytes,
  including the new `homebody/bridge/realtime_lifecycle.py`.
- This wheel retains the baseline version solely for local packaging tests; it is not the
  canonical published 0.5.54 artifact and must not replace that release.
- Complete unit identities were collected and reconciled across bounded runs, replacing
  complete rerun modules rather than adding duplicate test counts: **3,792 cases = 3,776 passed,
  11 failed, 5 skipped**, with no missing or extra identities.
- All 11 failures reproduce on unmodified baseline main with the same complete dependency
  environment. Five expect candidate-language hint moderation during selection; the other six
  expect older candidate-pool bounds or local object eligibility vetoes. Reconcile these tests
  against the accepted model-judgment/speech-screening contract separately; do not restore local
  object blacklists or erase failures merely to obtain a green run.
- Four skipped native discussion E2E cases require a Hermes source exposing that native
  discussion contract, absent from the currently installed source. The fifth skip is an optional
  non-shipped probe. The normal unit environment had further dependency skips; a separate
  dependency-complete retry replaced those outcomes rather than claiming skips as passes.
- The initial whole-unit process reached the runner's 300-second bound. Final evidence uses
  completed bounded batches, not that timed-out attempt. The second batch also requires the
  existing `test_check_profiles` namespace initialization before the native adapter imports;
  otherwise Hermes' `tools` namespace can shadow a later Homebody helper import.
- Browser, independent release review, real backend, acoustic and physical acceptance were not
  run. This is not a release-ready or fully green candidate.

Machine-readable receipts, logs and JUnit remain untracked in `.artifacts/desloppify/` and
`.artifacts/test-runs/`. `reconciled-unit-results.json` names the exact contributing reports and
unchanged-main failure comparison.

## Next bounded work

1. Reconcile and strengthen the 11 baseline contract tests against current intended I Spy
   behavior, retaining moderation, provider refusal, cancellation and no-fabrication checks.
2. Extract route families from `homebody/main.py` and the bridge one family at a time, preserving
   route inventory, auth middleware, late-result fencing and package/direct-script imports.
3. Fix confirmed dictionary-shape/error-handling issues in those paths, then rescan only to
   verify findings. Avoid broad renaming, safety-path rewrites or score-target suppression.
