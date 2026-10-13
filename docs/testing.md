# Efficient test feedback without weakening the release gate

Use Python 3.11/3.12 and the project's test dependencies. Browser checks additionally need
Playwright + Chromium. The runner doesn't install packages or use production credentials.
On Linux/macOS it terminates its owned test process group if a batch exceeds its deadline.

## During development

```sh
python tools/checks.py changed homebody/voice_live.py --dry-run
python tools/checks.py changed --base HEAD
python tools/checks.py unit tests/test_kids_stream_contract.py
python tools/checks.py browser tests/test_ui_motion_browser.py
```

`changed` uses uncommitted/untracked changes relative to the specified commit when no paths are
provided. It chooses conservative risk families. Voice changes include Kids, adult engines,
runtime/session and Owner tests—not just the engine file's similarly named test. Shared UI
selects all browser panels; shared config, test fixtures, tooling and unknown source changes
fall back to complete coverage. It is a fast feedback tool, **not a release certification**.
Use explicit `--python /path/to/test/python` and `--browser-python /path/to/browser/python`
when those environments differ. File paths, not arbitrary shell commands, are accepted.

## Before release

```sh
python tools/checks.py release
```

This builds the wheel **before** all unit/artifact tests and then executes every browser test
in bounded batches of eight cases. Every batch must return success; browser skips/missing
collection fail the gate rather than producing an empty green result. Motion screenshots are
opt-in (`--screenshots`, or `HOMEBODY_TEST_SCREENSHOTS=1` for direct pytest): routine checks keep
all geometry/state/privacy assertions but avoid repeatedly rendering PNGs for human review.
Use that visual lane for changed artwork/layout and before visual acceptance. The batch deadline is
180 seconds by default, configurable with `--batch-timeout`; timing and JUnit reports remain
under `.artifacts/test-runs/`. CI keeps both Python unit lanes and a dedicated real Chromium
lane, so browser checks are no longer silently skipped by a Python-only environment.
Lint, JS syntax and independent safety review remain separate required checks. Synthetic SDK
edges do not prove real microphone/speaker/camera or physical safety acceptance.

## Feedback discipline

- Implement against the precise failing reproduction and affected-risk tests.
- Do not have the implementer and reviewer both rerun the full suite for every internal edit.
- Reviewer runs narrow adversarial boundary tests. Owner runs one complete gate on the final
  candidate before publication; any material fix invalidates that prior final gate.
- Keep Stop/End/privacy/expiry/replacement cases and cross-mode positive controls. Parameterize
  common invariants rather than proliferating near-duplicate setup code.
- Replace brittle source-string checks with handler-level behavior where possible. The Kids
  streaming test now exercises an actual authenticated loopback HTTP handler, fixed Flash/PCM
  payload, configured voice, ignored caller overrides and single-use approval replay rejection.

## Browser timeout corrected

The stalled updater race test awaited `HomebodyUpdates.refresh()` while its own mock deliberately
held that HTTP response. The test could never reach the line releasing the request. It now
starts the refresh without awaiting, waits for real request interception with a bounded
condition, and retains the stale-success-versus-rollback-failure assertions. This was a test
control-flow deadlock, not evidence of a product failure or a browser teardown problem.
