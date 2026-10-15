"""Bounded BlueZ Agent1 that authorizes only HID for one exact, bonded device object.

This module deliberately avoids ``from __future__ import annotations``: dbus-next
reads the D-Bus signature strings from the method annotations at class creation.
It is imported lazily so non-Linux hosts never need dbus-next.
"""

import asyncio
import concurrent.futures
import itertools
import re
import threading

from dbus_next import BusType, DBusError, Variant
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method

HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"
_REJECTED = "org.bluez.Error.Rejected"
_AGENT_IDS = itertools.count(1)
_DEVICE_PATH_RE = re.compile(r"/org/bluez/hci[0-9]+/dev_([0-9A-F]{2}(?:_[0-9A-F]{2}){5})")
_CANCELLED = (asyncio.CancelledError, concurrent.futures.CancelledError)


def _value(value):
    return value.value if isinstance(value, Variant) else value


class _AgentPolicy:
    """Decision and lifecycle state shared by the exported interface and the pairing session."""

    def __init__(self, target: str) -> None:
        self.target = target.upper()
        self.device_path = ""
        self.decisions: list = []
        self.inactive_reason = ""
        self.inactive = threading.Event()
        self.lock = threading.Lock()
        self.on_deactivate = None

    @property
    def released(self) -> bool:
        return self.inactive.is_set()

    def deactivate(self, reason: str) -> None:
        """Irreversibly end this lifecycle; every later callback is rejected."""
        with self.lock:
            if self.inactive.is_set():
                return
            self.inactive_reason = reason
            self.inactive.set()
            hook = self.on_deactivate
        if hook is not None:
            hook()

    def is_target(self, device: str) -> bool:
        match = _DEVICE_PATH_RE.fullmatch(device or "")
        return bool(
            match
            and match.group(1).replace("_", ":") == self.target
            and (not self.device_path or device == self.device_path)
        )

    def record(self, callback: str, device: str, detail: str, accepted: bool) -> None:
        with self.lock:
            self.decisions.append((callback, device, detail, "accepted" if accepted else "rejected"))

    def reject(self, callback: str, device: str, detail: str, reason: str):
        self.record(callback, device, detail, False)
        return DBusError(_REJECTED, reason)


class HomebodyPairingAgent(ServiceInterface):
    """org.bluez.Agent1 with an exact device-path and HID-UUID allowlist.

    Callback policy: AuthorizeService and RequestAuthorization can succeed only
    while active and only for the exact target (HID only for AuthorizeService).
    RequestConfirmation, RequestPinCode, RequestPasskey, DisplayPinCode and
    DisplayPasskey are always rejected, active or not. Release and Cancel are
    lifecycle notifications: they never authorize anything, always deactivate
    (idempotently) and return normally so BlueZ's teardown is not reported as
    an error.

    ``read_properties(path)`` is a coroutine returning BlueZ's current, bounded
    ``org.bluez.Device1`` properties for that exact object. AuthorizeService is
    a coroutine, so dbus-next's aio bus keeps dispatching Cancel/Release while
    the read is outstanding.
    """

    def __init__(self, policy: _AgentPolicy, read_properties) -> None:
        super().__init__("org.bluez.Agent1")
        self._policy = policy
        self._read_properties = read_properties

    def _require_active(self, callback: str, device: str, detail: str) -> None:
        if self._policy.inactive.is_set():
            raise self._policy.reject(callback, device, detail, "Pairing agent is no longer active")

    @method()
    def Release(self):
        self._policy.record("Release", "", "", False)
        self._policy.deactivate("released by BlueZ")

    @method()
    async def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F821
        policy = self._policy
        normalized = str(uuid).lower()
        self._require_active("AuthorizeService", device, normalized)
        if not policy.is_target(device):
            raise policy.reject("AuthorizeService", device, normalized, "Device is not the pairing target")
        if normalized != HID_UUID:
            raise policy.reject("AuthorizeService", device, normalized, "Only the HID service is authorized")
        try:
            properties = await self._read_properties(device)
        except Exception as exc:
            raise policy.reject(
                "AuthorizeService", device, normalized, f"Could not read device properties: {exc}"
            ) from exc
        # The lifecycle may have ended while the read was outstanding.
        self._require_active("AuthorizeService", device, normalized)
        if properties.get("Paired") is not True or properties.get("Bonded") is not True:
            raise policy.reject("AuthorizeService", device, normalized, "Target is not confirmed paired and bonded")
        policy.record("AuthorizeService", device, normalized, True)

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: F821
        # Just Works for a pairing this session initiated with the exact target.
        policy = self._policy
        self._require_active("RequestAuthorization", device, "")
        if not policy.is_target(device):
            raise policy.reject("RequestAuthorization", device, "", "Device is not the pairing target")
        policy.record("RequestAuthorization", device, "", True)

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F821
        raise self._policy.reject("RequestConfirmation", device, "", "Passkey confirmation is not supported")

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: F821
        raise self._policy.reject("RequestPinCode", device, "", "PIN pairing is not supported")

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: F821
        raise self._policy.reject("RequestPasskey", device, "", "Passkey pairing is not supported")

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: F821
        raise self._policy.reject("DisplayPinCode", device, "", "PIN pairing is not supported")

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: F821
        # NoInputNoOutput cannot display a passkey; refuse rather than silently succeed.
        raise self._policy.reject("DisplayPasskey", device, "", "Passkey display is not supported")

    @method()
    def Cancel(self):
        self._policy.record("Cancel", "", "", False)
        self._policy.deactivate("cancelled by BlueZ")


async def _system_bus():
    return await MessageBus(bus_type=BusType.SYSTEM).connect()


class BluezPairingAgent:
    """Synchronous facade for one agent lifecycle on a private event loop."""

    def __init__(self, target: str, *, bus_factory=_system_bus, property_read_timeout: float = 2.0) -> None:
        self.policy = _AgentPolicy(target)
        self.policy.on_deactivate = self._cancel_operations
        self.path = f"/org/homebody/pairing_agent_{next(_AGENT_IDS)}"
        self._bus_factory = bus_factory
        self._property_read_timeout = property_read_timeout
        self._interface = HomebodyPairingAgent(self.policy, self._read_device_properties)
        self._bus = None
        self._manager = None
        self._adapter_path = ""
        self._exported = False
        self._registered = False
        self._discovering = False
        self._closed = False
        self.cleanup_errors: list = []
        self._tasks: set = set()
        self._tasks_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="homebody-bluez-agent", daemon=True)
        self._thread.start()

    @property
    def decisions(self) -> list:
        with self.policy.lock:
            return list(self.policy.decisions)

    @property
    def inactive_reason(self) -> str:
        return self.policy.inactive_reason

    @property
    def released(self) -> bool:
        return self.policy.released

    @property
    def device_path(self) -> str:
        return self.policy.device_path

    def _cancel_operations(self) -> None:
        """Cancel every outstanding pair/discovery/property-read task (any thread)."""
        with self._tasks_lock:
            tasks = list(self._tasks)
        for task in tasks:
            task.get_loop().call_soon_threadsafe(task.cancel)

    async def _tracked(self, coroutine, timeout: float):
        task = asyncio.current_task()
        with self._tasks_lock:
            self._tasks.add(task)
        try:
            if self.policy.inactive.is_set():
                coroutine.close()
                raise asyncio.CancelledError()
            return await asyncio.wait_for(coroutine, timeout)
        finally:
            with self._tasks_lock:
                self._tasks.discard(task)

    def _call(self, coroutine, timeout: float, what: str):
        if self.policy.inactive.is_set():
            coroutine.close()
            raise RuntimeError(f"{what} was cancelled: pairing agent {self.policy.inactive_reason}")
        if threading.current_thread() is self._thread:
            coroutine.close()
            raise RuntimeError(f"{what} cannot block the agent's own event loop")
        future = asyncio.run_coroutine_threadsafe(self._tracked(coroutine, timeout), self._loop)
        try:
            return future.result(timeout + 1.0)
        except _CANCELLED as exc:
            reason = self.policy.inactive_reason or "cancelled"
            raise RuntimeError(f"{what} was cancelled: pairing agent {reason}") from exc
        except (TimeoutError, concurrent.futures.TimeoutError) as exc:
            future.cancel()
            raise RuntimeError(f"Timed out waiting for {what}") from exc
        except DBusError as exc:
            raise RuntimeError(f"{what} failed: {exc.type}: {exc.text}") from exc

    async def _interface_at(self, path: str, name: str):
        introspection = await self._bus.introspect("org.bluez", path)
        return self._bus.get_proxy_object("org.bluez", path, introspection).get_interface(name)

    async def _start(self) -> None:
        self._bus = await self._bus_factory()
        self._bus.export(self.path, self._interface)
        self._exported = True
        self._manager = await self._interface_at("/org/bluez", "org.bluez.AgentManager1")
        await self._manager.call_register_agent(self.path, "NoInputNoOutput")
        self._registered = True
        await self._manager.call_request_default_agent(self.path)

    def start(self, timeout: float) -> None:
        """Return only after BlueZ accepted both RegisterAgent and RequestDefaultAgent."""
        self._call(self._start(), timeout, "BlueZ to register the pairing agent")

    async def _get_device_properties(self, path: str) -> dict:
        properties = await self._interface_at(path, "org.freedesktop.DBus.Properties")
        values = await properties.call_get_all("org.bluez.Device1")
        return {key: _value(value) for key, value in values.items()}

    async def _read_device_properties(self, path: str) -> dict:
        """Bounded authoritative read, awaited by AuthorizeService on the agent loop.

        The read runs as its own tracked task, so Cancel/Release/close cancel it
        without cancelling the handler; the handler then replies with a rejection.
        """
        if self._bus is None:
            raise RuntimeError("agent bus is not connected")
        task = asyncio.ensure_future(self._tracked(self._get_device_properties(path), self._property_read_timeout))
        await asyncio.wait({task})
        if task.cancelled():
            raise RuntimeError(f"property read cancelled: pairing agent {self.policy.inactive_reason or 'cancelled'}")
        try:
            return task.result()
        except TimeoutError as exc:
            raise RuntimeError("Timed out waiting for BlueZ device properties") from exc

    async def _objects(self) -> dict:
        manager = await self._interface_at("/", "org.freedesktop.DBus.ObjectManager")
        return await manager.call_get_managed_objects()

    async def _discover(self, poll: float) -> str:
        while True:
            objects = await self._objects()
            if not self._adapter_path:
                adapters = sorted(path for path, ifaces in objects.items() if "org.bluez.Adapter1" in ifaces)
                if not adapters:
                    raise RuntimeError("BlueZ reported no Bluetooth adapter")
                self._adapter_path = adapters[0]
                adapter = await self._interface_at(self._adapter_path, "org.bluez.Adapter1")
                await adapter.call_start_discovery()
                self._discovering = True
            for path, interfaces in objects.items():
                if "org.bluez.Device1" not in interfaces or not path.startswith(f"{self._adapter_path}/"):
                    continue
                match = _DEVICE_PATH_RE.fullmatch(path)
                if match and match.group(1).replace("_", ":") == self.policy.target:
                    with self.policy.lock:
                        self.policy.device_path = path
                    return path
            await asyncio.sleep(poll)

    def discover(self, timeout: float, poll: float = 0.25) -> str:
        """Keep discovery alive on this client until the exact target object exists."""
        return self._call(
            self._discover(poll), timeout, f"{self.policy.target} to appear; put the controller in pairing mode"
        )

    async def _pair(self) -> None:
        device = await self._interface_at(self.policy.device_path, "org.bluez.Device1")
        try:
            await device.call_pair()
        except DBusError as exc:
            if exc.type != "org.bluez.Error.AlreadyExists":
                raise

    def pair(self, timeout: float) -> None:
        if not self.policy.device_path:
            raise RuntimeError("Pairing target has not been discovered")
        self._call(self._pair(), timeout, f"BlueZ to pair {self.policy.target}")

    def wait(self, seconds: float) -> None:
        """Wait, returning early if the lifecycle is cancelled, released or closed."""
        self.policy.inactive.wait(max(0.0, seconds))

    async def _stop_discovery(self) -> None:
        adapter = await self._interface_at(self._adapter_path, "org.bluez.Adapter1")
        await adapter.call_stop_discovery()

    async def _remote_cleanup(self, step_timeout: float) -> list:
        """Best-effort BlueZ RPCs, each independently bounded; one hang never skips the next."""
        errors: list = []
        steps = []
        if self._discovering:
            self._discovering = False
            steps.append(("StopDiscovery", self._stop_discovery))
        if self._registered:
            self._registered = False
            steps.append(("UnregisterAgent", lambda: self._manager.call_unregister_agent(self.path)))
        for name, step in steps:
            try:
                await asyncio.wait_for(step(), step_timeout)
            except TimeoutError:
                errors.append(f"{name}: timed out after {step_timeout:g}s")
            except BaseException as exc:  # noqa: BLE001 - includes CancelledError; continue teardown
                errors.append(f"{name}: {exc or type(exc).__name__}")
        return errors

    def _local_cleanup(self) -> list:
        """Local teardown: never waits on BlueZ, so it always completes."""
        errors: list = []
        if self._exported:
            self._exported = False
            try:
                self._bus.unexport(self.path, self._interface)
            except Exception as exc:
                errors.append(f"unexport: {exc}")
        if self._bus is not None:
            bus, self._bus = self._bus, None
            try:
                bus.disconnect()
            except Exception as exc:
                errors.append(f"disconnect: {exc}")
        return errors

    @staticmethod
    async def _cancel_remaining(timeout: float) -> None:
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    def _run_cleanup(self, coroutine, timeout: float, what: str, errors: list):
        future = asyncio.run_coroutine_threadsafe(asyncio.wait_for(coroutine, timeout), self._loop)
        try:
            return future.result(timeout + 1.0)
        except BaseException as exc:  # noqa: BLE001 - every cleanup step must still run
            future.cancel()
            errors.append(f"{what}: {exc or type(exc).__name__}")
            return None

    async def _local_cleanup_async(self) -> list:
        return self._local_cleanup()

    def close(self, timeout: float = 3.0) -> None:
        """Deactivate, cancel outstanding work, unregister, unexport and stop the loop.

        Guarantees: StopDiscovery and UnregisterAgent are attempted, each bounded
        by ``timeout``; a hung or failing RPC is recorded and never skips the
        local unexport/disconnect, which do not wait on BlueZ. The loop thread
        is then stopped and joined (bounded by ``timeout``). All failures are
        raised together and retained in ``cleanup_errors``; repeated close is a
        no-op. Calling close from the agent's own loop thread is refused.
        """
        if self._closed:
            return
        if threading.current_thread() is self._thread:
            self.policy.deactivate("closed")
            raise RuntimeError("BlueZ agent cannot be closed from its own event loop")
        self._closed = True
        self.policy.deactivate("closed")
        errors: list = []
        remote_bound = timeout * 2 + 1.0  # two independently bounded RPCs
        errors.extend(
            self._run_cleanup(self._remote_cleanup(timeout), remote_bound, "remote cleanup", errors) or []
        )
        local = self._run_cleanup(self._local_cleanup_async(), timeout, "local cleanup", errors)
        if local is None:
            # The loop did not run it in time; tear down locally rather than leak the bus/export.
            local = self._local_cleanup()
        errors.extend(local)
        self._run_cleanup(self._cancel_remaining(timeout), timeout, "task cancellation", errors)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            errors.append(f"{self._thread.name} did not stop")
        else:
            self._loop.close()
        self.cleanup_errors = list(errors)
        if errors:
            raise RuntimeError("BlueZ agent cleanup failed: " + "; ".join(errors))
