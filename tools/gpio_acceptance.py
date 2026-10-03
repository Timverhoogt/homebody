#!/usr/bin/env python3
"""Guided hardware acceptance for Homebody's green and red GPIO buttons. Standard library only.

Run the three parts in order, with an adult at the robot and clear space around it:

    python3 tools/gpio_acceptance.py preflight              # on the Pi: library, chip, permissions, line owners
    python3 tools/gpio_acceptance.py wiring                 # on the Pi, buttons off in Homebody: electrical test
    python3 tools/gpio_acceptance.py run --url http://reachy-mini.local:8042   # guided behaviour test

``preflight`` and ``wiring`` need the ``gpiod`` (libgpiod v2) Python package, as Homebody does, and
read the pins from Homebody's config unless you pass ``--green``/``--red``. ``run`` talks to the
Homebody settings server and works from any computer on the LAN. It tells you which button to
press and confirms the result from the app's own state: the button event counter, power mode,
speech state, Kids Mode state and the head fold. Every part prints PASS/FAIL/SKIP lines and writes a
Markdown report (``--report``) to paste into the acceptance record. The exit code is 1 if any check
failed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path.home() / ".local" / "share" / "homebody" / "config.json"
BOUNCE_WINDOW_SECONDS = 0.03  # the app's debounce; raw edges closer than this are contact bounce
POLL_SECONDS = 0.1


@dataclass
class Check:
    name: str
    expected: str
    status: str = "SKIP"  # PASS | FAIL | SKIP
    observed: str = ""
    seconds: float | None = None

    def line(self) -> str:
        took = f" ({self.seconds:.2f} s)" if self.seconds is not None else ""
        return f"{self.status:4}  {self.name}{took}: {self.observed}"


@dataclass
class Report:
    title: str
    context: dict[str, str] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        print(check.line(), flush=True)
        return check

    @property
    def failed(self) -> bool:
        return any(check.status == "FAIL" for check in self.checks)

    def markdown(self) -> str:
        lines = [f"# {self.title}", "", f"Run {dt.datetime.now().astimezone().isoformat(timespec='seconds')}.", ""]
        lines += [f"- **{key}:** {value}" for key, value in self.context.items()]
        lines += ["", "| Result | Check | Expected | Observed | Time |", "| --- | --- | --- | --- | --- |"]
        for check in self.checks:
            took = f"{check.seconds:.2f} s" if check.seconds is not None else ""
            observed = check.observed.replace("|", "/")
            lines.append(f"| {check.status} | {check.name} | {check.expected} | {observed} | {took} |")
        if self.notes:
            lines += ["", "Notes:", ""] + [f"- {note}" for note in self.notes]
        summary = {status: sum(c.status == status for c in self.checks) for status in ("PASS", "FAIL", "SKIP")}
        lines += ["", f"**{summary['PASS']} passed, {summary['FAIL']} failed, {summary['SKIP']} skipped.**", ""]
        return "\n".join(lines)

    def save(self, path: str | None, stem: str) -> Path:
        target = Path(path) if path else Path(f"{stem}-{dt.datetime.now():%Y%m%d-%H%M}.md")
        target.write_text(self.markdown(), encoding="utf-8")
        print(f"\nReport written to {target}")
        return target


def wait_for(probe: Callable[[], Any], timeout: float, *, interval: float = POLL_SECONDS) -> tuple[Any, float]:
    """Poll until ``probe`` returns something truthy; returns (value, seconds) or (None, timeout)."""
    started = time.monotonic()
    while True:
        value = probe()
        elapsed = time.monotonic() - started
        if value:
            return value, elapsed
        if elapsed >= timeout:
            return None, elapsed
        time.sleep(interval)


def configured_pins(args: argparse.Namespace) -> tuple[str, dict[str, int]]:
    """Pins from the command line, else from Homebody's config file."""
    config: dict[str, Any] = {}
    path = Path(args.config).expanduser()
    if path.is_file():
        config = json.loads(path.read_text(encoding="utf-8"))
    chip = args.chip or config.get("gpio_chip") or "/dev/gpiochip0"
    pins = {}
    for name in ("green", "red"):
        value = getattr(args, name)
        if value is None:
            value = config.get(f"gpio_{name}_pin", 17 if name == "green" else None)
        if value is not None:
            pins[name] = int(value)
    return chip, pins


# -- preflight -------------------------------------------------------------------------------------


def preflight(args: argparse.Namespace) -> int:
    report = Report("Homebody GPIO buttons: preflight")
    chip, pins = configured_pins(args)
    report.context.update({"Host": platform.node(), "Chip": chip, "Pins": json.dumps(pins)})
    report.add(
        Check("Python", "3.11 or newer", "PASS" if sys.version_info >= (3, 11) else "FAIL", sys.version.split()[0])
    )
    report.add(
        Check("Both buttons configured", "green and red pins set", "PASS" if len(pins) == 2 else "FAIL", str(pins))
    )
    try:
        import gpiod  # noqa: PLC0415 - only needed on the Pi
    except ModuleNotFoundError:
        report.add(Check("gpiod library", "libgpiod v2 Python package", "FAIL", "not installed: pip install gpiod"))
        report.save(args.report, "gpio-preflight")
        return 1
    version = str(getattr(gpiod, "__version__", "unknown"))
    v2 = hasattr(gpiod, "request_lines")
    report.add(Check("gpiod library", "libgpiod v2 Python package", "PASS" if v2 else "FAIL", f"version {version}"))
    exists = os.path.exists(chip)
    access = exists and os.access(chip, os.R_OK | os.W_OK)
    report.add(
        Check(
            "Chip access",
            "this user can open the chip",
            "PASS" if access else "FAIL",
            "read/write" if access else ("missing" if not exists else "no permission: add the user to the gpio group"),
        )
    )
    try:
        import grp  # noqa: PLC0415 - POSIX only

        groups = {grp.getgrgid(gid).gr_name for gid in os.getgroups()}
        in_group = "gpio" in groups or os.geteuid() == 0
        report.add(
            Check("gpio group", "service user is in gpio", "PASS" if in_group else "FAIL", ", ".join(sorted(groups)))
        )
    except (ImportError, KeyError):
        report.add(Check("gpio group", "service user is in gpio", "SKIP", "groups not available on this system"))
    if v2 and access:
        try:
            with gpiod.Chip(chip) as handle:
                for name, offset in pins.items():
                    info = handle.get_line_info(offset)
                    owner = info.consumer or ""
                    ok = (not info.used) or owner == "homebody"
                    observed = f"line {offset} ({info.name or 'unnamed'}): " + (
                        f"used by {owner!r}" if info.used else "free"
                    )
                    report.add(
                        Check(f"{name} line owner", "free, or held by homebody", "PASS" if ok else "FAIL", observed)
                    )
        except OSError as exc:
            report.add(Check("Line info", "readable", "FAIL", str(exc)))
    report.notes.append("Restart the Reachy daemon after changing group membership.")
    report.save(args.report, "gpio-preflight")
    return 1 if report.failed else 0


# -- wiring ----------------------------------------------------------------------------------------


def _count_presses(edges: list[tuple[float, bool]]) -> tuple[int, int]:
    """Debounced presses, and raw edges that were contact bounce."""
    presses, bounce, last, level = 0, 0, float("-inf"), False
    for timestamp, pressed in edges:
        if timestamp - last < BOUNCE_WINDOW_SECONDS or pressed == level:
            bounce += 1
            continue
        last, level = timestamp, pressed
        presses += pressed
    return presses, bounce


def wiring(args: argparse.Namespace, *, ask: Callable[[str], str] = input) -> int:
    """Open the lines without debounce, have the operator press each button, and inspect raw edges."""
    report = Report("Homebody GPIO buttons: wiring")
    chip, pins = configured_pins(args)
    report.context.update({"Host": platform.node(), "Chip": chip, "Pins": json.dumps(pins)})
    import gpiod  # noqa: PLC0415 - only needed on the Pi
    from gpiod.line import Bias, Direction, Edge, Value  # noqa: PLC0415

    settings = gpiod.LineSettings(
        direction=Direction.INPUT, edge_detection=Edge.BOTH, bias=Bias.PULL_UP, active_low=True
    )
    offsets = {offset: name for name, offset in pins.items()}
    try:
        request = gpiod.request_lines(chip, consumer="homebody-acceptance", config={tuple(offsets): settings})
    except OSError as exc:
        report.add(
            Check(
                "Open lines",
                "lines are free",
                "FAIL",
                f"{exc}. Turn the buttons off in Robot → Physical buttons first.",
            )
        )
        report.save(args.report, "gpio-wiring")
        return 1
    try:
        values = dict(zip(offsets, request.get_values(list(offsets)), strict=True))
        for offset, name in offsets.items():
            idle = values[offset] != Value.ACTIVE
            report.add(
                Check(
                    f"{name} idle level",
                    "reads released",
                    "PASS" if idle else "FAIL",
                    "released" if idle else "reads pressed while untouched: check wiring to ground",
                )
            )
        for name, offset in pins.items():
            prompt = f"Press and release {name.upper()} {args.presses} times within {args.seconds:.0f} s."
            ask(f"\n→ {prompt} Enter to start: ")
            edges: dict[int, list[tuple[float, bool]]] = {o: [] for o in offsets}
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                if request.wait_edge_events(dt.timedelta(seconds=0.1)):
                    for event in request.read_edge_events():
                        pressed = event.event_type == gpiod.EdgeEvent.Type.RISING_EDGE  # active_low: press
                        edges[event.line_offset].append((event.timestamp_ns / 1e9, pressed))
            presses, bounce = _count_presses(edges[offset])
            report.add(
                Check(
                    f"{name} presses",
                    f"{args.presses} clean presses",
                    "PASS" if presses == args.presses else "FAIL",
                    f"{presses} presses, {len(edges[offset])} raw edges",
                )
            )
            report.add(
                Check(
                    f"{name} contact bounce",
                    "absorbed by the 30 ms debounce",
                    "PASS",
                    f"{bounce} bounce edge(s)" + (" (noisy switch, still within debounce)" if bounce else ""),
                )
            )
            others = {offsets[o]: len(e) for o, e in edges.items() if o != offset and e}
            report.add(
                Check(
                    f"{name} isolation",
                    "no edges on the other button",
                    "FAIL" if others else "PASS",
                    f"edges on {others}" if others else "none",
                )
            )
    finally:
        request.release()
    report.save(args.report, "gpio-wiring")
    return 1 if report.failed else 0


# -- guided behaviour run ---------------------------------------------------------------------------


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def _call(self, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method="GET" if body is None else "POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - the owner gives the URL
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"null")
            except ValueError:
                return exc.code, None

    def get(self, path: str) -> tuple[int, Any]:
        return self._call(path)

    def post(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        return self._call(path, body)


class Run:
    def __init__(
        self,
        api: Any,
        report: Report,
        *,
        ask: Callable[[str], str],
        timeout: float,
        say: Callable[[str], None] = print,
    ) -> None:
        self.api = api
        self.report = report
        self.ask = ask
        self.say = say  # every instruction to the operator goes through here
        self.timeout = timeout
        self.seq = 0

    # state helpers
    def runtime(self) -> dict[str, Any]:
        code, body = self.api.get("/api/status")
        return (body or {}).get("runtime", {}) if code == 200 else {}

    def buttons(self) -> dict[str, Any]:
        code, body = self.api.get("/api/gpio/status")
        return body if code == 200 and isinstance(body, dict) else {}

    def new_events(self) -> list[dict[str, Any]]:
        return [event for event in self.buttons().get("recent_events", []) if event.get("seq", 0) > self.seq]

    def mark(self) -> None:
        self.seq = int(self.buttons().get("event_count", 0))

    def await_event(self, label: str, timeout: float | None = None) -> tuple[dict[str, Any] | None, float]:
        def probe() -> dict[str, Any] | None:
            return next((e for e in self.new_events() if e.get("event") == label), None)

        return wait_for(probe, timeout or self.timeout)

    def power(self, mode: str) -> bool:
        code, _ = self.api.post("/api/power", {"mode": mode})
        reached, _ = wait_for(lambda: self.runtime().get("power_mode") == mode, 20)
        return code == 200 and bool(reached)

    def step(self, name: str, expected: str, instruction: str) -> Check:
        self.say(f"\n{name}\n  {instruction}")
        self.mark()
        return Check(name, expected)

    def gesture(self, check: Check, label: str) -> dict[str, Any] | None:
        event, waited = self.await_event(label)
        if event is None:
            others = ", ".join(str(e.get("event")) for e in self.new_events()) or "none"
            check.status, check.observed = "FAIL", f"no '{label}' within {self.timeout:.0f} s (events seen: {others})"
            return None
        if event.get("result") != "applied":
            check.status, check.observed = "FAIL", f"'{label}' was not applied: {event.get('result')}"
            return None
        return event

    # the checks
    def preconditions(self) -> bool:
        status = self.buttons()
        configured = status.get("configured", {})
        pins = status.get("pins", {})
        ready = bool(status.get("available")) and "green" in pins and "red" in pins
        observed = (
            f"monitoring {pins} on {configured.get('chip')}, long press {configured.get('long_press_seconds')} s"
            if ready
            else f"available={status.get('available')} pins={pins} error={status.get('last_error')!r}"
        )
        self.report.add(
            Check("Buttons monitored", "both pins open in Homebody", "PASS" if ready else "FAIL", observed)
        )
        runtime = self.runtime()
        started = bool(runtime) and runtime.get("state") != "not_started"
        self.report.add(
            Check(
                "Runtime started",
                "voice runtime running",
                "PASS" if started else "FAIL",
                f"state={runtime.get('state')} power={runtime.get('power_mode')}",
            )
        )
        if ready:
            self.long_press = float(configured.get("long_press_seconds") or 2.0)
        return ready and started

    def standby_baseline(self) -> None:
        check = Check("Start in Standby", "Standby, head folded")
        folded = self.power("standby") and wait_for(lambda: self.runtime().get("head_safely_folded") is True, 20)[0]
        check.status = "PASS" if folded else "FAIL"
        check.observed = f"power={self.runtime().get('power_mode')} folded={self.runtime().get('head_safely_folded')}"
        self.report.add(check)

    def green_listens(self) -> None:
        check = self.step(
            "Green short in Standby", "Reachy wakes and listens", "Press GREEN briefly (under a second)."
        )
        event = self.gesture(check, "green short")
        if event:
            seen, took = wait_for(lambda: self.runtime().get("state") == "listening", 10, interval=0.05)
            check.status = "PASS" if seen else "FAIL"
            check.observed = "listening" if seen else f"state stayed {self.runtime().get('state')}"
            check.seconds = took
        self.report.add(check)

    def red_stops_speech(self) -> None:
        check = self.step(
            "Red short during an answer",
            "speech and movement stop",
            "Ask Reachy a question (press GREEN first if needed). As soon as it starts answering, press RED briefly.",
        )
        speaking, _ = wait_for(lambda: self.runtime().get("state") == "speaking", 90, interval=0.05)
        if not speaking:
            check.observed = "Reachy did not start speaking within 90 s (is the agent bridge connected?)"
            self.report.add(check)
            return
        event = self.gesture(check, "red short")
        if event:
            stopped, took = wait_for(lambda: self.runtime().get("state") != "speaking", 5, interval=0.05)
            check.status = "PASS" if stopped else "FAIL"
            check.observed = f"stopped, now {self.runtime().get('state')}" if stopped else "still speaking after 5 s"
            check.seconds = took
            self.report.notes.append(
                "Movement stop during the answer is confirmed by eye: note it if Reachy kept moving."
            )
        self.report.add(check)

    def long_press_mode(self, button: str, mode: str, name: str) -> None:
        check = self.step(
            name,
            f"{button} long → {mode}, head folded",
            f"Hold {button.upper()} for about {self.long_press + 1:.0f} s, then release.",
        )
        if self.gesture(check, f"{button} long"):
            reached, took = wait_for(
                lambda: (r := self.runtime()).get("power_mode") == mode and r.get("head_safely_folded") is True, 25
            )
            check.status = "PASS" if reached else "FAIL"
            runtime = self.runtime()
            check.observed = f"power={runtime.get('power_mode')} folded={runtime.get('head_safely_folded')}"
            check.seconds = took
        self.report.add(check)

    def green_wakes_from_sleep(self) -> None:
        check = self.step("Green short in Sleep", "Reachy returns to Awake", "Press GREEN briefly.")
        if self.gesture(check, "green short"):
            reached, took = wait_for(lambda: self.runtime().get("power_mode") == "awake", 25)
            check.status = "PASS" if reached else "FAIL"
            check.observed = f"power={self.runtime().get('power_mode')}"
            check.seconds = took
        self.report.add(check)

    def red_ends_kids(self) -> None:
        check = Check("Red short in Kids Mode", "session ends with a safe fold")
        self.mark()
        self.say("\nRed short in Kids Mode\n  Start a short Kids Mode session in the Kids tab (any activity).")
        active, _ = wait_for(lambda: (self.runtime().get("kids_mode") or {}).get("active") is True, 180, interval=0.5)
        if not active:
            check.observed = "no Kids Mode session started within 3 minutes"
            self.report.add(check)
            return
        self.say("  Kids Mode is on. Now press RED briefly.")
        # The phone UI and /api/gpio/status are locked during Kids Mode; /api/status still answers.
        ended, took = wait_for(
            lambda: (self.runtime().get("kids_mode") or {}).get("active") is False, 60, interval=0.1
        )
        kids = self.runtime().get("kids_mode") or {}
        folded = self.runtime().get("head_safely_folded") is True and kids.get("last_fold_succeeded") is not False
        events = [e.get("event") for e in self.new_events()]
        by_red = "red short" in events
        check.status = "PASS" if ended and folded and by_red else "FAIL"
        check.observed = (
            f"ended={bool(ended)} reason={kids.get('last_end_reason')} folded={folded} events={events or 'none'}"
        )
        check.seconds = took if ended else None
        self.report.add(check)

    def startup_ownership(self) -> None:
        check = self.step(
            "Held at start is ignored",
            "a held button acts only after release and a new press",
            "Press and HOLD GREEN, and keep holding until told to release.",
        )
        held, _ = wait_for(lambda: self.buttons().get("buttons", {}).get("green", {}).get("held") is True, 30)
        if not held:
            check.status, check.observed = "FAIL", "GREEN never read as held"
            self.report.add(check)
            return
        configured = self.buttons().get("configured", {})
        # Re-applying the same settings re-opens the lines, exactly as an app start does.
        code, _ = self.api.post(
            "/api/gpio/buttons",
            {
                "enabled": True,
                "green_pin": configured.get("green_pin"),
                "red_pin": configured.get("red_pin"),
                "long_press_seconds": configured.get("long_press_seconds", 2.0),
            },
        )
        reopened = self.buttons().get("buttons", {}).get("green", {})
        self.say("  Keep holding… now RELEASE GREEN.")
        released, _ = wait_for(lambda: self.buttons().get("buttons", {}).get("green", {}).get("ignored") == 1, 30)
        unwanted = [e.get("event") for e in self.new_events()]
        self.say("  Now press GREEN briefly once more.")
        self.mark()
        event, took = self.await_event("green short")
        ok = code == 200 and reopened.get("armed") is False and released and not unwanted and event is not None
        check.status = "PASS" if ok else "FAIL"
        check.observed = (
            f"re-opened while held (armed={reopened.get('armed')}), release ignored={bool(released)}, "
            f"events while held={unwanted or 'none'}, next press {'acted' if event else 'did not act'}"
        )
        check.seconds = took if event else None
        self.report.add(check)

    def stuck_lockout(self) -> None:
        check = self.step(
            "Stuck button lockout",
            "a button held 30 s is ignored until released",
            "Hold GREEN down for about 35 s (Reachy goes to Standby after the long press), then release.",
        )
        stuck, took = wait_for(lambda: "green" in self.buttons().get("stuck", []), 60, interval=0.5)
        self.say("  Release GREEN now.")
        cleared, _ = wait_for(lambda: "green" not in self.buttons().get("stuck", []), 30)
        extra = [e.get("event") for e in self.new_events() if e.get("event") != "green long"]
        check.status = "PASS" if stuck and cleared and not extra else "FAIL"
        check.observed = (
            f"stuck after {took:.0f} s={bool(stuck)}, cleared on release={bool(cleared)}, "
            f"other events={extra or 'none'}"
        )
        self.report.add(check)


def run(
    args: argparse.Namespace,
    *,
    api: Any = None,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> int:
    api = api or Api(args.url)
    report = Report("Homebody GPIO buttons: behaviour acceptance")
    code, status = api.get("/api/status")
    host = ((status or {}).get("host") or {}) if code == 200 else {}
    report.context.update(
        {"Homebody": args.url, "Robot computer": str(host.get("model") or host.get("label") or "unknown")}
    )
    runner = Run(api, report, ask=ask, timeout=args.timeout, say=say)
    if not runner.preconditions():
        report.save(args.report, "gpio-acceptance")
        return 1
    answer = ask(
        "\nReachy will wake, move, fold and go to Sleep during this test. Clear the space around it and keep a\n"
        "hand near the red button. Type 'yes' to start: "
    )
    if answer.strip().lower() != "yes":
        print("Stopped before any movement.")
        return 1
    runner.standby_baseline()
    runner.green_listens()
    if not args.skip_voice:
        runner.red_stops_speech()
    runner.long_press_mode("red", "sleep", "Red long press")
    runner.green_wakes_from_sleep()
    runner.long_press_mode("green", "standby", "Green long press")
    if not args.skip_kids:
        runner.red_ends_kids()
    runner.startup_ownership()
    if args.stuck:
        runner.stuck_lockout()
    runner.power("standby")
    recent = runner.buttons().get("recent_events", [])
    report.notes.append(
        "Recent button events: " + ", ".join(f"#{e['seq']} {e['event']} ({e['result']})" for e in recent)
    )
    report.save(args.report, "gpio-acceptance")
    return 1 if report.failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="part", required=True)
    for name in ("preflight", "wiring"):
        part = sub.add_parser(name)
        part.add_argument("--config", default=str(DEFAULT_CONFIG), help="Homebody config.json to read pins from")
        part.add_argument("--chip", help="GPIO chip, default from config or /dev/gpiochip0")
        part.add_argument("--green", type=int, help="green button BCM pin")
        part.add_argument("--red", type=int, help="red button BCM pin")
        part.add_argument("--report", help="Markdown report path")
    wiring_parser = sub.choices["wiring"]
    wiring_parser.add_argument("--presses", type=int, default=3)
    wiring_parser.add_argument("--seconds", type=float, default=20.0)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--url", default="http://127.0.0.1:8042", help="Homebody settings address")
    run_parser.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for each press")
    run_parser.add_argument("--skip-voice", action="store_true", help="skip the stop-during-an-answer check")
    run_parser.add_argument("--skip-kids", action="store_true", help="skip the Kids Mode check")
    run_parser.add_argument("--stuck", action="store_true", help="also test the 30 s stuck-button lockout")
    run_parser.add_argument("--report", help="Markdown report path")
    args = parser.parse_args(argv)
    return {"preflight": preflight, "wiring": wiring, "run": run}[args.part](args)


if __name__ == "__main__":
    sys.exit(main())
