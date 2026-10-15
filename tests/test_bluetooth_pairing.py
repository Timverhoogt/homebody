"""Synthetic BlueZ pairing lifecycle tests; no system bus, radio, process, or device access."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

pytest.importorskip("dbus_next")

from dbus_next import DBusError, Message, MessageType, Variant  # noqa: E402

from homebody.bluetooth import BluetoothGamepadService, InputNode, PairTimeouts  # noqa: E402
from homebody.bluetooth_agent import (  # noqa: E402
    HID_UUID,
    BluezPairingAgent,
    HomebodyPairingAgent,
    _AgentPolicy,
)

TARGET = "A4:AE:12:A6:9F:BE"
PHONE = "1A:31:F9:1F:20:ED"
OTHER_PAD = "11:22:33:44:55:66"
A2DP = "0000110b-0000-1000-8000-00805f9b34fb"
ADAPTER = "/org/bluez/hci0"


def device_path(mac: str, adapter: str = ADAPTER) -> str:
    return f"{adapter}/dev_{mac.replace(':', '_')}"


def auth(target: Any, path: str, uuid: str) -> None:
    """Invoke the coroutine AuthorizeService exactly as dbus-next does (method.fn, awaited).

    For a live BluezPairingAgent it runs on the agent's own loop, like a real bus callback.
    """
    from dbus_next.service import ServiceInterface

    interface = target._interface if isinstance(target, BluezPairingAgent) else target
    fn = {item.name: item for item in ServiceInterface._get_methods(interface)}["AuthorizeService"].fn
    loop = target._loop if isinstance(target, BluezPairingAgent) else None
    if loop is None or loop.is_closed():
        asyncio.run(fn(interface, path, uuid))
    else:
        asyncio.run_coroutine_threadsafe(fn(interface, path, uuid), loop).result(5.0)


FAST = PairTimeouts(start=0.5, discover=0.5, pair=0.5, connect=0.3, input_node=0.2, poll_interval=0.01)


@dataclass
class FakeResult:
    returncode: int
    stdout: str
    stderr: str = ""


@dataclass
class FakeDevice:
    name: str
    paired: bool = False
    bonded: bool | None = False
    trusted: bool = False
    connected: bool = False
    uuids: tuple[str, ...] = (HID_UUID,)


@dataclass
class FakeBlueZ:
    """Model the logged sequence: pair succeeds, then the controller opens HID PSM 17."""

    devices: dict[str, FakeDevice] = field(default_factory=dict)
    advertising: dict[str, str] = field(default_factory=dict)
    agent_registers: bool = True
    pair_error: str = ""
    hid_requests: list[tuple[str, str]] = field(default_factory=list)
    input_delay_polls: int = 0
    nodes: list[InputNode] = field(default_factory=list)
    extra_nodes: list[InputNode] = field(default_factory=list)
    runner_calls: list[list[str]] = field(default_factory=list)
    agents: list[FakeAgent] = field(default_factory=list)
    node_polls: int = 0
    events: list[str] = field(default_factory=list)
    close_error: str = ""
    trust_failure: str = ""  # "command" (applies, then fails) or "readback"

    def runner(self, command: list[str], _input: str | None, _timeout: float) -> FakeResult:
        self.runner_calls.append(command)
        args = command[1:]
        if args == ["power", "on"]:
            return FakeResult(0, "Changing power on succeeded")
        if args == ["show"]:
            return FakeResult(0, "Controller F0:68:E3:32:F5:B6\n\tPowered: yes\n")
        if args == ["devices"]:
            return FakeResult(0, "".join(f"Device {mac} {d.name}\n" for mac, d in self.devices.items()))
        if args == ["devices", "Paired"]:
            return FakeResult(0, "".join(f"Device {m} {d.name}\n" for m, d in self.devices.items() if d.paired))
        if args == ["devices", "Connected"]:
            return FakeResult(0, "".join(f"Device {m} {d.name}\n" for m, d in self.devices.items() if d.connected))
        verb, mac = (args + ["", ""])[:2]
        device = self.devices.get(mac)
        if verb in {"info", "trust", "untrust", "connect", "remove"} and device is None:
            return FakeResult(1, f"Device {mac} not available")
        assert device is not None
        if verb == "info" and self.trust_failure == "readback" and device.trusted:
            self.trust_failure = ""
            return FakeResult(1, "org.freedesktop.DBus.Error.NoReply")
        if verb == "info":
            yes = {True: "yes", False: "no"}
            lines = [f"Device {mac} (public)", f"\tName: {device.name}", f"\tPaired: {yes[device.paired]}"]
            if device.bonded is not None:  # BlueZ builds without the property omit the line.
                lines.append(f"\tBonded: {yes[device.bonded]}")
            lines += [f"\tTrusted: {yes[device.trusted]}", f"\tConnected: {yes[device.connected]}"]
            lines += [f"\tUUID: Service ({uuid})" for uuid in device.uuids]
            return FakeResult(0, "\n".join(lines))
        self.events.append(f"{verb} {mac} paired={device.paired} bonded={device.bonded}")
        if verb == "trust":
            device.trusted = True  # BlueZ state changes even when the command then reports failure.
            if self.trust_failure == "command":
                return FakeResult(1, "org.bluez.Error.Failed: timeout")
        elif verb == "untrust":
            device.trusted = False
        elif verb == "remove":
            del self.devices[mac]
        elif verb == "connect":
            return FakeResult(1, "Failed to connect: org.bluez.Error.Failed br-connection-refused")
        return FakeResult(0, f"Changing {mac} {verb} succeeded")

    def input_nodes(self) -> list[InputNode]:
        self.node_polls += 1
        ready = self.node_polls > self.input_delay_polls
        return [*self.extra_nodes, *(self.nodes if ready else [])]

    def agent_factory(self, target: str) -> FakeAgent:
        agent = FakeAgent(self, target)
        self.agents.append(agent)
        return agent


class FakeAgent:
    """Lifecycle stand-in whose callback decisions go through the real HomebodyPairingAgent."""

    def __init__(self, bluez: FakeBlueZ, target: str) -> None:
        self.bluez = bluez
        self.policy = _AgentPolicy(target)
        self.interface = HomebodyPairingAgent(self.policy, self.read_properties)
        self.registered = False
        self.closed = False
        self.calls: list[str] = []
        self.reads: list[str] = []

    async def read_properties(self, path: str) -> dict[str, object]:
        """Authoritative BlueZ Device1 state for the exact object (bonded=None omits Bonded)."""
        self.reads.append(path)
        device = self.bluez.devices[self.policy.target]
        properties: dict[str, object] = {"Paired": device.paired, "Trusted": device.trusted}
        if device.bonded is not None:
            properties["Bonded"] = device.bonded
        return properties

    @property
    def inactive_reason(self) -> str:
        return self.policy.inactive_reason

    def start(self, timeout: float) -> None:
        self.calls.append("start")
        if not self.bluez.agent_registers:
            raise RuntimeError("Timed out waiting for BlueZ to register the pairing agent")
        self.registered = True

    def discover(self, timeout: float) -> str:
        self.calls.append("discover")
        assert self.registered
        mac = self.policy.target
        if mac not in self.bluez.devices:
            if mac not in self.bluez.advertising:
                raise RuntimeError(f"Timed out waiting for {mac} to appear; put the controller in pairing mode")
            self.bluez.devices[mac] = FakeDevice(name=self.bluez.advertising[mac])
        self.policy.device_path = device_path(mac)
        return self.policy.device_path

    def pair(self, timeout: float) -> None:
        self.calls.append("pair")
        assert self.registered, "agent must be registered before pairing"
        mac = self.policy.target
        device = self.bluez.devices[mac]
        self.bluez.events.append(f"pair {mac} trusted={device.trusted}")
        if self.bluez.pair_error:
            raise RuntimeError(f"BlueZ to pair {mac} failed: {self.bluez.pair_error}")
        device.paired = True
        device.bonded = True if device.bonded is not None else None
        device.connected = True
        # 15:31:40: the controller opens HID; bluetoothd asks the default agent.
        requests = self.bluez.hid_requests or [(device_path(mac), HID_UUID)]
        for path, uuid in requests:
            self._authorize(path, uuid)

    def _authorize(self, path: str, uuid: str) -> None:
        mac = self.policy.target
        try:
            auth(self.interface, path, uuid)
        except DBusError:
            if mac.replace(":", "_") in path and uuid == HID_UUID:
                # "Access denied: org.bluez.Error.Rejected" then disconnect.
                self.bluez.devices[mac].connected = False
            return
        if path == device_path(mac) and uuid == HID_UUID:
            self.bluez.nodes.append(InputNode("/dev/input/js1", "Wireless Controller", "054c", mac.lower()))

    def wait(self, seconds: float) -> None:
        assert not self.closed, "agent must remain registered while waiting for HID/input"
        self.policy.inactive.wait(min(seconds, 0.005))

    def close(self, timeout: float = 3.0) -> None:
        self.calls.append("close")
        self.closed = True
        self.registered = False
        self.policy.deactivate("closed")
        if self.bluez.close_error:
            raise RuntimeError(self.bluez.close_error)


def make_service(bluez: FakeBlueZ, actions: list[tuple[str, str, str]] | None = None) -> BluetoothGamepadService:
    recorded = actions if actions is not None else []
    return BluetoothGamepadService(
        lambda kind, action, value: recorded.append((kind, action, value)) or True,
        command_runner=bluez.runner,
        adapter_available=True,
        agent_factory=bluez.agent_factory,
        input_nodes=bluez.input_nodes,
        pair_timeouts=FAST,
    )


def operation(status: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], status["operation"])


# --- Agent1 callback policy -------------------------------------------------------------


class Reader:
    """Authoritative-property stand-in that records every exact-object read."""

    def __init__(self, properties: dict[str, Any] | None = None) -> None:
        self.properties = {"Paired": True, "Bonded": True} if properties is None else properties
        self.paths: list[str] = []
        self.delay = 0.0

    async def __call__(self, path: str) -> dict[str, Any]:
        self.paths.append(path)
        if self.delay:
            await asyncio.sleep(self.delay)
        return dict(self.properties)


def _bonded_policy(reader: Reader | None = None) -> tuple[_AgentPolicy, HomebodyPairingAgent]:
    policy = _AgentPolicy(TARGET)
    policy.device_path = device_path(TARGET)
    return policy, HomebodyPairingAgent(policy, reader or Reader())


def test_authorize_service_accepts_only_exact_device_path_and_hid_uuid() -> None:
    reader = Reader()
    policy, agent = _bonded_policy(reader)
    auth(agent,device_path(TARGET), HID_UUID)
    auth(agent,device_path(TARGET), HID_UUID.upper())

    rejected = [
        (device_path(PHONE), HID_UUID),  # unrelated MAC
        (device_path(OTHER_PAD), HID_UUID),  # other controller
        (device_path(TARGET), A2DP),  # wrong service on target
        (device_path(TARGET, "/org/bluez/hci1"), HID_UUID),  # same MAC on another adapter object
        (device_path(TARGET) + "/service0001", HID_UUID),  # child object, not the device
        ("/org/bluez/hci0/dev_a4_ae_12_a6_9f_be", HID_UUID),  # not BlueZ's canonical path
        ("", HID_UUID),
    ]
    for path, uuid in rejected:
        with pytest.raises(DBusError) as raised:
            auth(agent,path, uuid)
        assert raised.value.type == "org.bluez.Error.Rejected"
    assert [entry[3] for entry in policy.decisions] == ["accepted"] * 2 + ["rejected"] * len(rejected)
    # Wrong target/service is rejected before any property access.
    assert reader.paths == [device_path(TARGET)] * 2


@pytest.mark.parametrize(
    "properties",
    [{}, {"Paired": True}, {"Paired": True, "Bonded": False}, {"Paired": False, "Bonded": True},
     {"Paired": "yes", "Bonded": "yes"}, {"Paired": 1, "Bonded": 1}],
)
def test_hid_is_rejected_without_positive_paired_and_bonded(properties: dict[str, Any]) -> None:
    _policy, agent = _bonded_policy(Reader(properties))
    with pytest.raises(DBusError):
        auth(agent,device_path(TARGET), HID_UUID)


def test_property_read_failure_rejects() -> None:
    async def failing(_path: str) -> dict[str, Any]:
        raise RuntimeError("Timed out waiting for BlueZ device properties")

    policy = _AgentPolicy(TARGET)
    agent = HomebodyPairingAgent(policy, failing)
    with pytest.raises(DBusError, match="Could not read device properties"):
        auth(agent,device_path(TARGET), HID_UUID)


@pytest.mark.parametrize("ending", ["Cancel", "Release", "close"])
def test_cancel_release_or_close_irreversibly_rejects_all_later_callbacks(ending: str) -> None:
    reader = Reader()
    policy, agent = _bonded_policy(reader)
    agent.RequestAuthorization(device_path(TARGET))
    auth(agent,device_path(TARGET), HID_UUID)
    if ending == "close":
        policy.deactivate("closed")
    else:
        getattr(agent, ending)()
    assert policy.inactive.is_set() and policy.inactive_reason
    for _ in range(2):
        with pytest.raises(DBusError, match="no longer active"):
            auth(agent,device_path(TARGET), HID_UUID)
        with pytest.raises(DBusError, match="no longer active"):
            agent.RequestAuthorization(device_path(TARGET))
    assert reader.paths == [device_path(TARGET)]  # no reads once inactive
    policy.deactivate("something else")
    assert policy.inactive_reason != "something else"  # first reason is kept


def test_lifecycle_ending_during_authoritative_read_rejects() -> None:
    policy = _AgentPolicy(TARGET)

    async def read_then_cancelled(_path: str) -> dict[str, Any]:
        policy.deactivate("cancelled by BlueZ")  # ends while the read is in flight
        return {"Paired": True, "Bonded": True}

    agent = HomebodyPairingAgent(policy, read_then_cancelled)
    with pytest.raises(DBusError, match="no longer active"):
        auth(agent,device_path(TARGET), HID_UUID)
    assert policy.decisions[-1][3] == "rejected"


def test_pairing_secrets_and_unrelated_pairing_requests_are_rejected() -> None:
    policy, agent = _bonded_policy()
    agent.RequestAuthorization(device_path(TARGET))
    for call in (
        lambda: agent.RequestAuthorization(device_path(PHONE)),
        lambda: agent.RequestConfirmation(device_path(TARGET), 123456),
        lambda: agent.RequestPinCode(device_path(TARGET)),
        lambda: agent.RequestPasskey(device_path(TARGET)),
        lambda: agent.DisplayPinCode(device_path(TARGET), "0000"),
        lambda: agent.DisplayPasskey(device_path(TARGET), 123456, 0),
    ):
        with pytest.raises(DBusError):
            call()
    assert [entry[3] for entry in policy.decisions] == ["accepted"] + ["rejected"] * 6


def test_explicit_policy_for_every_exported_callback_after_deactivation() -> None:
    reader = Reader()
    policy, agent = _bonded_policy(reader)
    agent.Cancel()
    rejecting = {
        "AuthorizeService": lambda: auth(agent, device_path(TARGET), HID_UUID),
        "RequestAuthorization": lambda: agent.RequestAuthorization(device_path(TARGET)),
        "RequestConfirmation": lambda: agent.RequestConfirmation(device_path(TARGET), 1),
        "RequestPinCode": lambda: agent.RequestPinCode(device_path(TARGET)),
        "RequestPasskey": lambda: agent.RequestPasskey(device_path(TARGET)),
        "DisplayPinCode": lambda: agent.DisplayPinCode(device_path(TARGET), "0000"),
        "DisplayPasskey": lambda: agent.DisplayPasskey(device_path(TARGET), 1, 0),
    }
    for name, call in rejecting.items():
        with pytest.raises(DBusError) as raised:
            call()
        assert raised.value.type == "org.bluez.Error.Rejected", name
    # Lifecycle notifications authorize nothing, stay inactive and return normally.
    agent.Release()
    agent.Cancel()
    assert policy.inactive_reason == "cancelled by BlueZ"
    assert reader.paths == []
    exported = {item.name for item in HomebodyPairingAgent(_AgentPolicy(TARGET), Reader()).introspect().methods}
    assert exported == set(rejecting) | {"Release", "Cancel"}
    assert all(entry[3] == "rejected" for entry in policy.decisions)


def test_agent1_interface_exports_bluez_signatures() -> None:
    introspection = HomebodyPairingAgent(_AgentPolicy(TARGET), Reader()).introspect()
    assert introspection.name == "org.bluez.Agent1"
    signatures = {
        item.name: ("".join(a.signature for a in item.in_args), "".join(a.signature for a in item.out_args))
        for item in introspection.methods
    }
    assert signatures == {
        "Release": ("", ""),
        "AuthorizeService": ("os", ""),
        "RequestAuthorization": ("o", ""),
        "RequestConfirmation": ("ou", ""),
        "RequestPinCode": ("o", "s"),
        "RequestPasskey": ("o", "u"),
        "DisplayPinCode": ("os", ""),
        "DisplayPasskey": ("ouq", ""),
        "Cancel": ("", ""),
    }


class SendReply:
    """Minimal stand-in for dbus-next's reply sender: callable and error-catching context."""

    def __init__(self, message: Message, replies: list[Message]) -> None:
        self.message, self.replies = message, replies

    def __call__(self, reply: Message) -> None:
        self.replies.append(reply)

    def __enter__(self) -> SendReply:
        return self

    def __exit__(self, exc_type: Any, exc: Any, _tb: Any) -> bool:
        if isinstance(exc, DBusError):
            self.replies.append(exc._as_message(self.message))
            return True
        return False


def test_real_aio_message_bus_dispatch_awaits_coroutine_callback_with_path_and_uuid() -> None:
    """Drive AuthorizeService through dbus_next.aio.MessageBus's coroutine handler (no socket)."""
    from dbus_next.aio import MessageBus
    from dbus_next.service import ServiceInterface

    reader = Reader()
    policy, agent = _bonded_policy(reader)
    method = {item.name: item for item in ServiceInterface._get_methods(agent)}["AuthorizeService"]
    assert asyncio.iscoroutinefunction(method.fn)
    bus = MessageBus.__new__(MessageBus)  # real aio class, never connected to any bus
    handler = bus._make_method_handler(agent, method)

    async def dispatch(path: str, uuid: str, during: Any = None) -> Message:
        replies: list[Message] = []
        message = Message(
            destination=":1.1", path="/org/homebody/pairing_agent_1", interface="org.bluez.Agent1",
            member="AuthorizeService", signature="os", body=[path, uuid], serial=1,
        )
        handler(message, SendReply(message, replies))
        if during is not None:
            await asyncio.sleep(0.01)
            during()  # e.g. BlueZ Cancel dispatched on the same loop while the read is pending
        for _ in range(500):
            if replies:
                return replies[0]
            await asyncio.sleep(0.002)
        raise AssertionError("no reply")

    async def scenario() -> None:
        assert (await dispatch(device_path(TARGET), HID_UUID)).message_type == MessageType.METHOD_RETURN
        for path, uuid in ((device_path(PHONE), HID_UUID), (device_path(TARGET), A2DP)):
            reply = await dispatch(path, uuid)
            assert reply.message_type == MessageType.ERROR
            assert reply.error_name == "org.bluez.Error.Rejected"
        assert reader.paths == [device_path(TARGET)]  # no read for wrong target/service
        reader.properties = {"Paired": True}  # authoritative Bonded missing
        assert (await dispatch(device_path(TARGET), HID_UUID)).message_type == MessageType.ERROR

        # A read is in flight; the loop is not blocked, so Cancel is processed meanwhile.
        reader.properties = {"Paired": True, "Bonded": True}
        reader.delay = 0.1
        reply = await dispatch(device_path(TARGET), HID_UUID, during=agent.Cancel)
        assert reply.message_type == MessageType.ERROR
        assert "no longer active" in str(reply.body)

    asyncio.run(scenario())
    assert [(entry[0], entry[1], entry[2], entry[3]) for entry in policy.decisions][:3] == [
        ("AuthorizeService", device_path(TARGET), HID_UUID, "accepted"),
        ("AuthorizeService", device_path(PHONE), HID_UUID, "rejected"),
        ("AuthorizeService", device_path(TARGET), A2DP, "rejected"),
    ]
    assert policy.decisions[-2][0] == "Cancel"


# --- BluezPairingAgent runtime protocol over a fake async MessageBus --------------------


class FakeInterface:
    def __init__(self, bus: FakeBus, path: str, name: str) -> None:
        self.bus, self.path, self.name = bus, path, name

    def __getattr__(self, attribute: str):
        if attribute == "on_properties_changed":
            return lambda callback: self.bus.property_callbacks.append((self.path, callback))
        if not attribute.startswith("call_"):
            raise AttributeError(attribute)

        async def call(*args):
            member = attribute[len("call_"):]
            self.bus.log.append((self.name, self.path, member, args))
            behavior = self.bus.behavior.get(member)
            if isinstance(behavior, BaseException):
                raise behavior
            if behavior == "hang":
                await asyncio.sleep(3600)
            if member == "get_managed_objects":
                self.bus.polls += 1
                return self.bus.objects()
            if member == "pair":
                self.bus.device_props.update(Paired=Variant("b", True), Bonded=Variant("b", True))
            if member == "get_all":
                return dict(self.bus.device_props)
            return None

        return call


class FakeProxy:
    def __init__(self, bus: FakeBus, path: str) -> None:
        self.bus, self.path = bus, path

    def get_interface(self, name: str) -> FakeInterface:
        return FakeInterface(self.bus, self.path, name)


class FakeBus:
    def __init__(self) -> None:
        self.log: list[tuple] = []
        self.exported: dict[str, object] = {}
        self.behavior: dict[str, object] = {}
        self.property_callbacks: list = []
        # Authoritative GetAll state, and the (possibly stale) discovery snapshot.
        self.device_props: dict[str, Variant] = {"Paired": Variant("b", False)}
        self.object_props: dict[str, Variant] = {"Paired": Variant("b", False)}
        self.appear_after_polls = 0
        self.polls = 0
        self.disconnected = False

    def objects(self) -> dict:
        objects: dict = {
            ADAPTER: {"org.bluez.Adapter1": {"Powered": Variant("b", True)}},
            device_path(PHONE): {"org.bluez.Device1": {"Paired": Variant("b", False)}},
        }
        if self.polls > self.appear_after_polls:
            objects[device_path(TARGET)] = {"org.bluez.Device1": dict(self.object_props)}
        return objects

    def export(self, path: str, interface: object) -> None:
        self.log.append(("bus", path, "export", ()))
        self.exported[path] = interface

    def unexport(self, path: str, interface: object = None) -> None:
        self.log.append(("bus", path, "unexport", ()))
        self.exported.pop(path, None)

    async def introspect(self, name: str, path: str) -> str:
        return path

    def get_proxy_object(self, name: str, path: str, introspection: str) -> FakeProxy:
        assert name == "org.bluez"
        return FakeProxy(self, path)

    def disconnect(self) -> None:
        self.log.append(("bus", "", "disconnect", ()))
        self.disconnected = True

    def members(self) -> list[str]:
        return [entry[2] for entry in self.log]


def make_agent(bus: FakeBus, **kwargs: Any) -> BluezPairingAgent:
    async def factory() -> FakeBus:
        return bus

    return BluezPairingAgent(TARGET, bus_factory=factory, **kwargs)


def in_thread(function: Any) -> tuple[threading.Thread, dict[str, Any]]:
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["value"] = function()
        except BaseException as exc:  # noqa: BLE001 - surfaced through result
            result["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


def wait_until(predicate: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.005)


def test_runtime_registration_is_ready_before_discovery_and_pair_then_cleaned_up() -> None:
    bus = FakeBus()
    bus.appear_after_polls = 2  # transient target appears while discovery stays alive
    agent = make_agent(bus)
    agent.start(1.0)
    path = agent.path
    assert bus.exported[path] is agent._interface
    assert bus.log[:3] == [
        ("bus", path, "export", ()),
        ("org.bluez.AgentManager1", "/org/bluez", "register_agent", (path, "NoInputNoOutput")),
        ("org.bluez.AgentManager1", "/org/bluez", "request_default_agent", (path,)),
    ]
    assert agent.discover(1.0, poll=0.01) == device_path(TARGET)
    assert ("org.bluez.Adapter1", ADAPTER, "start_discovery", ()) in bus.log
    assert bus.polls == 3
    agent.pair(1.0)
    assert ("org.bluez.Device1", device_path(TARGET), "pair", ()) in bus.log
    # The callback reads BlueZ's current Paired/Bonded for the exact object.
    auth(agent,device_path(TARGET), HID_UUID)
    assert ("org.freedesktop.DBus.Properties", device_path(TARGET), "get_all", ("org.bluez.Device1",)) in bus.log
    reads = bus.members().count("get_all")
    with pytest.raises(DBusError):
        auth(agent,device_path(PHONE), HID_UUID)
    assert bus.members().count("get_all") == reads  # no read for the wrong target
    assert path in bus.exported  # still registered after HID authorization
    agent.close()
    assert bus.members()[-4:] == ["stop_discovery", "unregister_agent", "unexport", "disconnect"]
    assert bus.exported == {}
    assert not agent._thread.is_alive()
    with pytest.raises(DBusError, match="no longer active"):
        auth(agent,device_path(TARGET), HID_UUID)
    agent.close()  # idempotent


def test_missing_agent_registration_fails_and_unexports() -> None:
    bus = FakeBus()
    bus.behavior["register_agent"] = DBusError("org.bluez.Error.AlreadyExists", "Already Exists")
    agent = make_agent(bus)
    with pytest.raises(RuntimeError, match="register the pairing agent failed: org.bluez.Error.AlreadyExists"):
        agent.start(1.0)
    agent.close()
    assert "request_default_agent" not in bus.members()
    assert "unregister_agent" not in bus.members()
    assert bus.members()[-2:] == ["unexport", "disconnect"]

    bus = FakeBus()
    bus.behavior["request_default_agent"] = DBusError("org.bluez.Error.DoesNotExist", "Does Not Exist")
    agent = make_agent(bus)
    with pytest.raises(RuntimeError, match="DoesNotExist"):
        agent.start(1.0)
    agent.close()
    assert bus.members()[-3:] == ["unregister_agent", "unexport", "disconnect"]


def test_authoritative_read_beats_stale_discovery_snapshot_in_both_directions() -> None:
    # Callback before any property signal: snapshot says unpaired, BlueZ now says bonded.
    bus = FakeBus()
    bus.device_props = {"Paired": Variant("b", True), "Bonded": Variant("b", True)}
    agent = make_agent(bus)
    agent.start(1.0)
    agent.discover(1.0, poll=0.01)
    auth(agent,device_path(TARGET), HID_UUID)
    agent.close()

    # Stale snapshot says bonded; authoritative Bonded is false, then missing.
    for current in ({"Paired": Variant("b", True), "Bonded": Variant("b", False)}, {"Paired": Variant("b", True)}):
        bus = FakeBus()
        bus.object_props = {"Paired": Variant("b", True), "Bonded": Variant("b", True)}
        bus.device_props = current
        agent = make_agent(bus)
        agent.start(1.0)
        agent.discover(1.0, poll=0.01)
        with pytest.raises(DBusError, match="not confirmed paired and bonded"):
            auth(agent,device_path(TARGET), HID_UUID)
        agent.close()


def test_authoritative_read_is_bounded() -> None:
    bus = FakeBus()
    bus.device_props = {"Paired": Variant("b", True), "Bonded": Variant("b", True)}
    agent = make_agent(bus, property_read_timeout=0.1)
    agent.start(1.0)
    agent.discover(1.0, poll=0.01)
    bus.behavior["get_all"] = "hang"
    started = time.monotonic()
    with pytest.raises(DBusError, match="Could not read device properties: Timed out"):
        auth(agent,device_path(TARGET), HID_UUID)
    assert time.monotonic() - started < 1.0
    agent.close()


def test_held_pair_is_cancelled_promptly_by_bluez_cancel_then_replacement_works() -> None:
    bus = FakeBus()
    bus.behavior["pair"] = "hang"
    agent = make_agent(bus)
    agent.start(1.0)
    agent.discover(1.0, poll=0.01)
    started = time.monotonic()
    thread, result = in_thread(lambda: agent.pair(30.0))
    wait_until(lambda: "pair" in bus.members())
    agent._interface.Cancel()  # BlueZ cancels the outstanding request
    thread.join(2.0)
    assert not thread.is_alive()
    assert time.monotonic() - started < 2.0
    assert isinstance(result.get("error"), RuntimeError)
    assert "cancelled" in str(result["error"])
    with pytest.raises(RuntimeError, match="cancelled"):
        agent.discover(1.0, poll=0.01)  # inactive lifecycle starts no new work
    agent.close()
    assert not agent._thread.is_alive()
    # The old agent can never authorize, even for a later bonded target.
    bus.device_props = {"Paired": Variant("b", True), "Bonded": Variant("b", True)}
    with pytest.raises(DBusError, match="no longer active"):
        auth(agent,device_path(TARGET), HID_UUID)

    replacement_bus = FakeBus()
    replacement = make_agent(replacement_bus)
    replacement.start(1.0)
    replacement.discover(1.0, poll=0.01)
    replacement.pair(1.0)
    auth(replacement,device_path(TARGET), HID_UUID)
    assert replacement.path != agent.path
    replacement.close()


def test_close_cancels_an_in_flight_authoritative_read_and_rejects() -> None:
    bus = FakeBus()
    agent = make_agent(bus, property_read_timeout=30.0)
    agent.start(1.0)
    agent.discover(1.0, poll=0.01)
    bus.behavior["get_all"] = "hang"
    thread, result = in_thread(lambda: auth(agent,device_path(TARGET), HID_UUID))
    wait_until(lambda: "get_all" in bus.members())
    started = time.monotonic()
    agent.close()
    thread.join(2.0)
    assert not thread.is_alive()
    assert time.monotonic() - started < 3.0
    assert isinstance(result.get("error"), DBusError)
    assert agent.decisions[-1][3] == "rejected"
    assert not agent._thread.is_alive()


def test_bluez_cancel_on_agent_loop_interrupts_in_flight_read() -> None:
    bus = FakeBus()
    agent = make_agent(bus, property_read_timeout=30.0)
    agent.start(1.0)
    agent.discover(1.0, poll=0.01)
    bus.behavior["get_all"] = "hang"
    thread, result = in_thread(lambda: auth(agent, device_path(TARGET), HID_UUID))
    wait_until(lambda: "get_all" in bus.members())
    started = time.monotonic()
    agent._loop.call_soon_threadsafe(agent._interface.Cancel)  # dispatched like a bus message
    thread.join(2.0)
    assert not thread.is_alive()
    assert time.monotonic() - started < 1.0
    assert isinstance(result.get("error"), DBusError)
    assert agent.inactive_reason == "cancelled by BlueZ"
    with pytest.raises(DBusError, match="no longer active"):
        auth(agent, device_path(TARGET), HID_UUID)
    agent.close()


@pytest.mark.parametrize("hanging", [["stop_discovery"], ["unregister_agent"], ["stop_discovery", "unregister_agent"]])
def test_hanging_cleanup_rpc_never_skips_local_teardown(hanging: list[str]) -> None:
    bus = FakeBus()
    agent = make_agent(bus)
    agent.start(1.0)
    agent.discover(1.0, poll=0.01)
    for member in hanging:
        bus.behavior[member] = "hang"  # an RPC that never replies
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="BlueZ agent cleanup failed") as raised:
        agent.close(timeout=0.1)
    assert time.monotonic() - started < 2.0
    for member, name in (("stop_discovery", "StopDiscovery"), ("unregister_agent", "UnregisterAgent")):
        assert member in bus.members()  # both remote steps were attempted
        if member in hanging:
            assert f"{name}: timed out after 0.1s" in str(raised.value)
    assert bus.members()[-2:] == ["unexport", "disconnect"]
    assert bus.exported == {} and bus.disconnected is True
    assert agent._bus is None
    assert not agent._thread.is_alive() and agent._loop.is_closed()
    assert agent.cleanup_errors and all("timed out" in error for error in agent.cleanup_errors)
    calls = len(bus.log)
    agent.close(timeout=0.1)  # repeated close is a no-op, error stays retained
    assert len(bus.log) == calls
    assert agent.cleanup_errors
    with pytest.raises(DBusError, match="no longer active"):
        auth(agent, device_path(TARGET), HID_UUID)


def test_close_continues_after_a_failing_step_and_reports_it() -> None:
    bus = FakeBus()
    agent = make_agent(bus)
    agent.start(1.0)
    agent.discover(1.0, poll=0.01)
    bus.behavior["stop_discovery"] = DBusError("org.bluez.Error.Failed", "boom")
    bus.behavior["unregister_agent"] = DBusError("org.bluez.Error.DoesNotExist", "gone")
    with pytest.raises(RuntimeError, match="StopDiscovery.*UnregisterAgent"):
        agent.close()
    assert bus.members()[-2:] == ["unexport", "disconnect"]
    assert bus.exported == {}
    assert not agent._thread.is_alive()


def test_discovery_and_pair_timeouts_and_errors_still_clean_up() -> None:
    bus = FakeBus()
    bus.appear_after_polls = 10_000
    agent = make_agent(bus)
    agent.start(1.0)
    with pytest.raises(RuntimeError, match="pairing mode"):
        agent.discover(0.1, poll=0.01)
    with pytest.raises(RuntimeError, match="not been discovered"):
        agent.pair(0.1)
    agent.close()
    assert bus.members()[-4:] == ["stop_discovery", "unregister_agent", "unexport", "disconnect"]

    bus = FakeBus()
    bus.behavior["pair"] = "hang"
    agent = make_agent(bus)
    agent.start(1.0)
    agent.discover(1.0, poll=0.01)
    with pytest.raises(RuntimeError, match="Timed out waiting for BlueZ to pair"):
        agent.pair(0.1)
    agent.close()
    assert bus.members()[-3:] == ["unregister_agent", "unexport", "disconnect"]

    bus = FakeBus()
    bus.behavior["pair"] = DBusError("org.bluez.Error.AuthenticationFailed", "Authentication Failed")
    agent = make_agent(bus)
    agent.start(1.0)
    agent.discover(1.0, poll=0.01)
    with pytest.raises(RuntimeError, match="AuthenticationFailed"):
        agent.pair(1.0)
    agent.close()
    assert bus.disconnected is True


def test_already_paired_pair_reply_is_not_an_error() -> None:
    bus = FakeBus()
    bus.behavior["pair"] = DBusError("org.bluez.Error.AlreadyExists", "Already Exists")
    bus.device_props.update(Paired=Variant("b", True), Bonded=Variant("b", True))
    agent = make_agent(bus)
    agent.start(1.0)
    agent.discover(1.0, poll=0.01)
    agent.pair(1.0)
    auth(agent,device_path(TARGET), HID_UUID)
    agent.close()


# --- Service lifecycle using the real callback policy -----------------------------------


def test_logged_failure_sequence_is_fixed_by_exact_hid_agent_callback() -> None:
    bluez = FakeBlueZ(advertising={TARGET: "Wireless Controller"}, input_delay_polls=2)
    actions: list[tuple[str, str, str]] = []
    service = make_service(bluez, actions)

    status = service.pair(TARGET.lower())

    agent = bluez.agents[0]
    assert agent.calls == ["start", "discover", "pair", "close"]
    assert agent.policy.decisions == [("AuthorizeService", device_path(TARGET), HID_UUID, "accepted")]
    # No trust before pairing: trust only after positive Paired and Bonded.
    assert bluez.events == [f"pair {TARGET} trusted=False", f"trust {TARGET} paired=True bonded=True"]
    assert operation(status) == {
        "name": "pair", "address": TARGET, "phase": "input_ready", "error": "", "input_path": "/dev/input/js1",
    }
    device = next(item for item in cast(list[dict[str, object]], status["devices"]) if item["address"] == TARGET)
    assert device["paired"] is True and device["bonded"] is True and device["connected"] is True
    # Bluetooth success never enables controller movement or claims a running reader.
    assert status["gamepad_enabled"] is False
    assert status["gamepad_connected"] is False
    assert service._dispatch_suspended is True
    assert actions == []


def test_late_hid_request_for_unrelated_device_or_service_does_not_make_target_ready() -> None:
    bluez = FakeBlueZ(
        advertising={TARGET: "Wireless Controller"},
        hid_requests=[(device_path(PHONE), HID_UUID), (device_path(TARGET), A2DP)],
    )
    service = make_service(bluez)
    with pytest.raises(RuntimeError, match="no input device appeared"):
        service.pair(TARGET)
    agent = bluez.agents[0]
    assert [entry[3] for entry in agent.policy.decisions] == ["rejected", "rejected"]
    assert f"untrust {TARGET} paired=True bonded=True" in bluez.events
    assert bluez.devices[TARGET].trusted is False
    assert agent.closed is True


def test_pair_success_then_rejected_hid_disconnect_is_failure_with_retained_error() -> None:
    """15:31:37 pair Success -> 15:31:40 HID rejected -> disconnect is not usable pairing."""
    bluez = FakeBlueZ(
        advertising={TARGET: "Wireless Controller"},
        hid_requests=[(device_path(TARGET, "/org/bluez/hci1"), HID_UUID)],
    )
    service = make_service(bluez)
    with pytest.raises(RuntimeError, match="did not confirm connection"):
        service.pair(TARGET)
    assert bluez.agents[0].closed is True
    assert bluez.devices[TARGET].trusted is False
    status = service.refresh()
    assert status["last_error"] == ""
    assert operation(status)["phase"] == "failed"
    assert "did not confirm connection" in str(operation(status)["error"])


def test_missing_agent_registration_fails_before_discovery_trust_or_pair() -> None:
    bluez = FakeBlueZ(advertising={TARGET: "Wireless Controller"}, agent_registers=False)
    service = make_service(bluez)
    with pytest.raises(RuntimeError, match="register the pairing agent"):
        service.pair(TARGET)
    assert bluez.agents[0].calls == ["start", "close"]
    assert bluez.events == []
    assert "register the pairing agent" in str(operation(service.refresh())["error"])


@pytest.mark.parametrize("bonded", [False, None])
def test_no_trust_when_bonded_is_absent_or_no(bonded: bool | None, monkeypatch) -> None:
    bluez = FakeBlueZ(advertising={TARGET: "Wireless Controller"})
    bluez.devices[TARGET] = FakeDevice(name="Wireless Controller", bonded=bonded)

    def pair_without_bond(self: FakeAgent, timeout: float) -> None:
        self.calls.append("pair")
        self.bluez.devices[TARGET].paired = True  # Paired, but no positive Bonded

    monkeypatch.setattr(FakeAgent, "pair", pair_without_bond)
    with pytest.raises(RuntimeError, match="did not create a bond"):
        make_service(bluez).pair(TARGET)
    assert not any(event.startswith("trust") for event in bluez.events)
    assert bluez.agents[0].closed is True


def test_delayed_exact_input_node_is_selected_over_other_controller_node() -> None:
    bluez = FakeBlueZ(advertising={TARGET: "Wireless Controller"}, input_delay_polls=3)
    bluez.extra_nodes = [InputNode("/dev/input/js0", "Wireless Controller", "054c", OTHER_PAD.lower())]
    status = make_service(bluez).pair(TARGET)
    assert operation(status)["input_path"] == "/dev/input/js1"
    assert bluez.node_polls >= 4
    assert bluez.agents[0].calls[-1] == "close"


def test_only_wrong_controller_node_times_out_and_reverts_trust() -> None:
    bluez = FakeBlueZ(advertising={TARGET: "Wireless Controller"}, input_delay_polls=10_000)
    bluez.extra_nodes = [InputNode("/dev/input/js0", "Wireless Controller", "054c", OTHER_PAD.lower())]
    service = make_service(bluez)
    with pytest.raises(RuntimeError, match="no input device appeared"):
        service.pair(TARGET)
    assert bluez.devices[TARGET].trusted is False
    assert operation(service.status())["input_path"] == ""


def test_cancel_during_held_input_wait_fails_promptly_and_rolls_back() -> None:
    slow = PairTimeouts(start=0.5, discover=0.5, pair=0.5, connect=0.3, input_node=10.0, poll_interval=5.0)
    bluez = FakeBlueZ(advertising={TARGET: "Wireless Controller"}, input_delay_polls=10_000)
    service = BluetoothGamepadService(
        lambda *_args: True, command_runner=bluez.runner, adapter_available=True,
        agent_factory=bluez.agent_factory, input_nodes=bluez.input_nodes, pair_timeouts=slow,
    )
    original_wait = FakeAgent.wait

    def real_wait(self: FakeAgent, seconds: float) -> None:
        if not self.policy.inactive.is_set():
            threading.Timer(0.05, self.interface.Cancel).start()
        self.policy.inactive.wait(seconds)  # held wait that only Cancel can end early

    FakeAgent.wait = real_wait  # type: ignore[method-assign]
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="agent was cancelled by BlueZ before HID input was ready"):
            service.pair(TARGET)
    finally:
        FakeAgent.wait = original_wait  # type: ignore[method-assign]
    assert time.monotonic() - started < 2.0
    assert bluez.agents[0].closed is True
    assert bluez.devices[TARGET].trusted is False
    assert service.status()["gamepad_enabled"] is False


@pytest.mark.parametrize("mode", ["command", "readback"])
def test_trust_partial_failure_is_rolled_back(mode: str) -> None:
    bluez = FakeBlueZ(advertising={TARGET: "Wireless Controller"}, trust_failure=mode)
    service = make_service(bluez)
    with pytest.raises(RuntimeError):
        service.pair(TARGET)
    assert bluez.events[-1] == f"untrust {TARGET} paired=True bonded=True"
    assert bluez.devices[TARGET].trusted is False
    assert bluez.agents[0].closed is True
    assert operation(service.status())["phase"] == "failed"


def test_originally_trusted_device_is_not_untrusted_on_failure() -> None:
    bluez = FakeBlueZ(advertising={TARGET: "Wireless Controller"}, input_delay_polls=10_000)
    bluez.devices[TARGET] = FakeDevice(name="Wireless Controller", trusted=True)
    with pytest.raises(RuntimeError, match="no input device appeared"):
        make_service(bluez).pair(TARGET)
    assert not any(event.startswith(("trust", "untrust")) for event in bluez.events)
    assert bluez.devices[TARGET].trusted is True


def test_agent_close_failure_still_rolls_back_and_keeps_failed_status() -> None:
    bluez = FakeBlueZ(
        advertising={TARGET: "Wireless Controller"}, input_delay_polls=10_000, close_error="UnregisterAgent: boom"
    )
    service = make_service(bluez)
    with pytest.raises(RuntimeError, match="no input device appeared") as raised:
        service.pair(TARGET)
    assert any("agent cleanup failed: UnregisterAgent: boom" in note for note in raised.value.__notes__)
    assert bluez.devices[TARGET].trusted is False  # rollback ran despite the close failure
    error = str(operation(service.refresh())["error"])
    assert "no input device appeared" in error and "agent cleanup failed" in error
    assert operation(service.status())["phase"] == "failed"

    # A cleanup failure after an otherwise ready controller is not reported as success.
    ready = FakeBlueZ(advertising={TARGET: "Wireless Controller"}, close_error="disconnect: boom")
    service = make_service(ready)
    with pytest.raises(RuntimeError, match="Pairing cleanup failed: agent cleanup failed: disconnect: boom"):
        service.pair(TARGET)
    assert operation(service.status())["phase"] == "failed"
    assert "agent cleanup failed: disconnect: boom" in str(operation(service.status())["error"])
    assert ready.devices[TARGET].trusted is False  # trust added by this attempt is rolled back
    assert service.status()["gamepad_enabled"] is False


def test_cancel_after_node_is_already_present_is_not_success(monkeypatch) -> None:
    bluez = FakeBlueZ(advertising={TARGET: "Wireless Controller"})
    original = FakeAgent.pair

    def pair_then_cancel(self: FakeAgent, timeout: float) -> None:
        original(self, timeout)  # HID authorized, exact node present, device connected
        self.interface.Cancel()

    monkeypatch.setattr(FakeAgent, "pair", pair_then_cancel)
    service = make_service(bluez)
    with pytest.raises(RuntimeError, match="agent was cancelled by BlueZ before"):
        service.pair(TARGET)
    assert bluez.nodes  # the node existed; presence alone did not produce success
    assert operation(service.status())["phase"] == "failed"
    assert bluez.devices[TARGET].trusted is False


def test_new_operation_after_failed_one_uses_a_fresh_agent_and_succeeds() -> None:
    bluez = FakeBlueZ(advertising={TARGET: "Wireless Controller"}, pair_error="org.bluez.Error.AuthenticationFailed")
    service = make_service(bluez)
    with pytest.raises(RuntimeError):
        service.pair(TARGET)
    old = bluez.agents[0]
    bluez.pair_error = ""
    status = service.pair(TARGET)
    assert operation(status)["phase"] == "input_ready"
    assert bluez.agents[1] is not old and old.closed is True
    with pytest.raises(DBusError, match="no longer active"):
        auth(old.interface,device_path(TARGET), HID_UUID)


def test_target_never_discovered_and_pair_failure_clean_up() -> None:
    bluez = FakeBlueZ(advertising={PHONE: "Android-5453"})
    with pytest.raises(RuntimeError, match="pairing mode"):
        make_service(bluez).pair(TARGET)
    assert bluez.agents[0].calls == ["start", "discover", "close"]
    assert bluez.events == []

    failed = FakeBlueZ(advertising={TARGET: "Wireless Controller"}, pair_error="org.bluez.Error.AuthenticationFailed")
    with pytest.raises(RuntimeError, match="AuthenticationFailed"):
        make_service(failed).pair(TARGET)
    assert failed.agents[0].closed is True
    assert not any(event.startswith("trust") for event in failed.events)


def test_released_agent_and_unexpected_exception_close_and_fail() -> None:
    bluez = FakeBlueZ(advertising={TARGET: "Wireless Controller"}, input_delay_polls=10_000)
    original = FakeAgent.pair

    def pair_then_release(self: FakeAgent, timeout: float) -> None:
        original(self, timeout)
        self.interface.Release()

    service = make_service(bluez)
    FakeAgent.pair = pair_then_release  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="agent was released by BlueZ before"):
            service.pair(TARGET)
    finally:
        FakeAgent.pair = original  # type: ignore[method-assign]
    assert bluez.agents[0].closed is True

    broken = FakeBlueZ(advertising={TARGET: "Wireless Controller"})
    service = make_service(broken)

    def broken_nodes() -> list[InputNode]:
        raise OSError("sysfs unavailable")

    service._input_nodes = broken_nodes
    with pytest.raises(OSError):
        service.pair(TARGET)
    assert broken.agents[0].closed is True
    assert broken.devices[TARGET].trusted is False
    assert operation(service.status())["phase"] == "failed"


def test_stale_unbonded_target_is_removed_then_rediscovered_by_the_agent() -> None:
    bluez = FakeBlueZ(advertising={TARGET: "Wireless Controller"})
    bluez.devices[TARGET] = FakeDevice(name="Wireless Controller", paired=True, bonded=False, trusted=True)
    status = make_service(bluez).pair(TARGET)
    assert bluez.events[0] == f"remove {TARGET} paired=True bonded=False"
    assert bluez.events[1] == f"pair {TARGET} trusted=False"
    assert operation(status)["phase"] == "input_ready"


def test_generic_non_hid_device_reports_connected_without_input_claim(monkeypatch) -> None:
    bluez = FakeBlueZ(advertising={PHONE: "Headphones"})
    bluez.devices[PHONE] = FakeDevice(name="Headphones", uuids=(A2DP,))
    monkeypatch.setattr(FakeAgent, "_authorize", lambda self, path, uuid: None)
    status = make_service(bluez).pair(PHONE)
    assert operation(status)["phase"] == "connected"
    assert operation(status)["input_path"] == ""
    assert bluez.node_polls == 0


def test_scan_discoveries_stay_selectable_after_bluez_drops_them() -> None:
    bluez = FakeBlueZ()

    def runner(command: list[str], input_text: str | None, timeout: float) -> FakeResult:
        if command[1:] == ["--timeout", "5", "scan", "on"]:
            return FakeResult(0, f"[NEW] Device {PHONE} Android-5453\n[NEW] Device {TARGET} Wireless Controller\n")
        return bluez.runner(command, input_text, timeout)

    service = make_service(bluez)
    service._command_runner = runner
    service.scan(seconds=5)
    for _ in range(3):
        addresses = [item["address"] for item in cast(list[dict[str, object]], service.refresh()["devices"])]
        assert addresses == [PHONE, TARGET]
