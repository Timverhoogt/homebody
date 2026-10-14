# Installation and onboarding roadmap

## End state

A new Reachy owner finds Homebody in **Reachy Mini Control → Applications → Discover Apps**, installs and starts it, opens a safe guided setup, configures a working conversation backend and knows what is ready, optional or experimental. Pi, Windows/Linux, Mac and Jetson use one app and one wizard with platform profiles. Manual/community-host installation is the alternative, not a separate product.

The controller/browser, actual robot host and optional AI backend host are distinct roles. Do not make users reproduce the maintainer's private network, model inventory or service layout.

## Current development slice: 0.5.15 candidate

Implemented in the candidate branch, **not published, merged or deployed**:
- Quiet genuinely fresh startup and conservative motion/tool defaults; preserve established saved installs.
- Owner/CSRF-protected resumable welcome → connection → review flow.
- Marketplace/manual entry awareness without duplicate installation or new authority.
- Actual host/architecture/Python/SDK display and platform-specific starting advice.
- Language save and navigation into existing backend settings/pairing.
- Silent bridge health plus authenticated model-catalog check with a private, expiring, settings-bound and generation-fenced proof.
- Explicit review before saving setup for next Control Stop/Start; no automatic media activation from a setup response.
- Browser privacy/owner/Kids fences, honest microphone Off/setup and explicit untested-speech wording.
- Import-time Unix-locking blocker correction; unsupported managed writes remain refused.
- Candidate wheel/PWA assets and marketplace quick-start documentation.

These foundations **do not complete any platform's fresh-install/hardware certification**. Read [current quick start and limits](../marketplace-quickstart.md).

## Four delivery gates

### 1. Platform contract and preflight — partial

Finish native import/install/start tests for each supported OS/architecture and verify dependency/media wheels. Produce plain-language version, permissions, device, port, network and runtime blockers. Pi 4 and Pi 5, Apple Silicon and Intel, native Windows and Linux, and each Jetson L4T/CUDA tuple need distinct evidence. Detecting a host is not certification.

### 2. Repeatable installation and ownership — open

Validate the actual marketplace artifact and Control Install/Start/Stop/UI handoff. Preserve the registered app identifier and reconcile listing/release identity. Add platform bootstrap/service/repair recipes only where upstream tooling is insufficient; make retries idempotent and uninstall preserve data unless explicitly approved.

**Next product blocker:** secure terminal-free initial owner provisioning for marketplace users. Do not solve it with unauthenticated LAN claiming, a public pairing token, copied credentials or a browser-supplied installation-source hint. Reuse trusted local/physical or launcher-owned authority once its real contract is verified.

### 3. Complete guided first run — partial

Add ordinary non-agent basic provider/companion setup with native secret storage and bounded real-account access checks. The current slice still uses existing restricted agent setup/manual bridge instructions. Keep text-model connectivity, speech API access, microphone/speaker acceptance and physical controls separate; preserve a visible Stop/privacy path and per-feature consent. Resume/recheck without resetting owner devices.

### 4. Optional AI packs and maintenance — open

Version/checksum/license compatible local-model packs; report download, verified weights, runtime test, integration and activation separately. Measure combined ASR/LLM/VLM RAM, do not infer capacity from SSD. Make fallback and rollback explicit. Distinguish marketplace-owned updates from managed slots and prevent competing updaters. Publish redacted diagnostics and tested profiles.

## Definition of done

An unfamiliar owner completes the actual marketplace route without developer tooling for the baseline, securely pairs, configures conversation, understands data locality/costs, supervises first speech, operates Stop/privacy, restarts/repairs/updates and can submit redacted diagnostics. Native install, browser fixtures, provider access and physical acceptance are separately reported. No public release until exact-head regression/package checks, independent security review and the required acceptance/approval gates pass.

## Verification artifacts

Local candidate evidence lives under ignored `.artifacts/`. Unit, browser, wheel, source review and supervised hardware acceptance must each have their own status. Passing fixtures are not evidence of Control installation, a real account's model access or room/child-speech accuracy.
