# Contributing to Homebody

Thanks for wanting to help Reachy Mini feel at home. Homebody is the always-on foundation, covering power, safety, privacy, family rules and a pluggable agent brain. It gets better every time someone brings in a new game, story, connector, integration or tested setup.

You don't need to be a robotics expert. Some of the most useful contributions are a hardware report, a translation or a bug found while living with the robot.

> Homebody was previously called *Reachy Mini Hermes*. The Python package is `reachy-mini-homebody` and the module is `homebody`.

## Ways to contribute

### Brains: agent connectors

Homebody owns the body and your agent provides the brain. Hermes Agent is the reference backend and OpenClaw works alongside it. Connectors for other agents, such as Grok, Muse Spark, dots or a local model, are very welcome.

- Start from [`companion/agent_backends.py`](companion/agent_backends.py) and the [OpenClaw section of the bridge README](companion/README.md#use-openclaw-instead-of-or-besides-hermes). They show the pattern: authenticated bridge, credentials kept on the agent host, and refusing configurations the bridge can't check.
- A connector should fail closed. If it can't confirm that an agent's tools are limited, it shouldn't expose them.
- Add tests next to [`tests/test_openclaw_bridge.py`](tests/test_openclaw_bridge.py).
- Make it [agent-led](docs/agent-setup.md) if the agent can run commands. Add a setup guide in [`homebody/agent_guides/`](homebody/agent_guides/) and its name to `BACKENDS` in [`homebody/agent_setup.py`](homebody/agent_setup.py). Follow the existing guides: ask the owner first, use a separate restricted profile, a check after every step, and pairing that the robot verifies. Then test it by giving a capable agent only the Settings message.

### Play: experiences for kids and family

Games, stories, quizzes and calm-down activities make Homebody a robot children want to talk to.

- Kids activities live in [`homebody/kids_mode.py`](homebody/kids_mode.py), currently buddy, story, quiz, riddles, calm and I Spy.
- Every Play experience runs under the [household promises](#household-promises), especially supervision, moderation and the privacy states.
- Have a bigger idea? An experience can also grow into its own project. [Reachy Mini I Spy](https://github.com/Timverhoogt/reachy-mini-i-spy) started here and now runs standalone with its own [safety contract](https://github.com/Timverhoogt/reachy-mini-i-spy/blob/main/docs/SAFETY_CONTRACT.md). Both paths are welcome.

### Home: home automation integrations

Homebody already speaks to Home Assistant through an ESPHome device bridge ([`homebody/home_assistant.py`](homebody/home_assistant.py)). Other systems are good additions: Homey, openHAB, MQTT, Matter and others. Keep them off by default, allowlist-based and honest. A value Homebody can't measure is reported as unavailable, never made up.

### Setups: hardware reports

The [supported setups table](README.md#supported-setups) separates ✅ reference-tested, ◐ supported and 🧪 experimental. Moving a setup from 🧪 to ◐ or ✅ takes real people running real robots.

- Open an issue with your hardware (robot, host computer, OS, accelerator), the commit or version you ran, and which [acceptance checks](OPERATIONS.md) passed or failed.
- Photos are welcome but optional. Leave out anything personal in the background.

### Languages and voices

A household speaks its own language. I Spy already has English and Dutch flows. More languages, wake phrases and voice presets help Homebody fit more homes.

### Fixes, docs and ideas

Bug reports, clearer docs and "this confused me" feedback are all valuable. If an idea doesn't fit a category above, open an issue and describe the moment in your home where it would help.

## Household promises

Homebody runs all day in places where families live, so everything that runs inside it keeps these promises. They are the contract that lets contributions in without everyone having to re-review the safety model.

1. **Respect the privacy state.** No microphone capture or wake detection in Meeting or Sleep. No camera frames unless the camera opt-in is on, and none in Meeting, Sleep or a blocked Kids state. Check the current state immediately before capturing, not only at start.
2. **Stop always wins.** Physical actions go through the existing bounded action worker and movement arbitration. No raw joint commands, no unbounded loops, and cooperative cancellation when Stop is pressed. Reachy folds before torque goes off.
3. **Kids are supervised.** Content for children uses Kids Mode prompts and moderation, stays within adult-configured settings, and never unlocks adult tools.
4. **Consequential actions are approved.** Anything that changes the world outside the robot (messages, calendar, files, media, devices) goes through the agent allowlists and the exact-approval flow on the trusted phone UI.
5. **Data stays home where it can.** Prefer local processing. Never send audio, frames or personal context to a provider unless the owner enabled that route, and never log transcripts or credentials.
6. **Be truthful.** Report unknown or unsupported states as unavailable. Mark features ✅, ◐ or 🧪 by what has actually been tested, and don't claim physical acceptance that hasn't happened.

When a promise and a feature conflict, the promise wins. If you think a promise is wrong, open an issue to discuss changing it. Please don't work around it in code.

## Development setup

```bash
uv sync --group dev
uv run ruff check .
uv run pytest
uv build --wheel
reachy-mini-app-assistant check .
```

The Reachy Mini SDK is stubbed in [`tests/conftest.py`](tests/conftest.py), so the automated suite runs without a robot. CI runs the same lint and test steps on Python 3.11 and 3.12 ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

## Pull requests

- **Keep it focused.** One feature, fix or connector per pull request is easiest to review.
- **Add tests** for new behaviour, especially privacy, Kids and approval gates. Existing `tests/test_*` files show the style.
- **Say what was tested.** List automated tests, and state plainly whether anything ran on a physical robot, on which setup, and with which acceptance checks. "Not tested on hardware" is a perfectly good answer.
- **Never run physical acceptance unattended.** Moving-robot checks need an adult present, clear space and Stop within reach (see [OPERATIONS.md](OPERATIONS.md)).
- **Update the docs** for anything users will see, and add a line to [`CHANGELOG.md`](CHANGELOG.md).
- **Credit your sources.** If you adapt code, models or assets from another project, add it to [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) with project, commit and license. The Reachy Mini community shares generously, and we keep that visible.

## Security and privacy issues

Please don't open a public issue for anything that could expose a bearer token, provider credential, private audio or video, a device-control path or a remote tool-execution path. Use GitHub Security Advisories as described in [SECURITY.md](SECURITY.md).

## Being a good neighbour

Homebody wants to sit alongside the other Reachy Mini apps, not compete with them. In issues, pull requests and docs, describe what Homebody does for a household rather than what other apps don't do. Be kind to newcomers. Many people's first robot is a Reachy Mini, and many contributors' first pull request might be here.

## License

By contributing, you agree that your contributions are licensed under the [Apache License 2.0](LICENSE), the same license as the project.
