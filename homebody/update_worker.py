"""Independent, unprivileged supervisor worker for explicitly configured immutable app slots.

The HTTP app can request a release identity, never a command, path, repository or URL.
Keep this worker in a separate venv: updating app slots cannot replace the running updater.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

import httpx

from .updates import ACTIVE, REPOSITORY, Store, compatible_host, get, validate_manifest, verify_wheel

UNIT = "homebody-app.service"
HEARTBEAT_INTERVAL = 2


def dependency_env() -> dict:
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def config(path: Path) -> dict:
    value = json.loads(path.read_text())
    daemon = value.get("topology") == "reachy-daemon-slots-v1"
    keys = {"topology", "state", "slots", "current", "python", "status_url", "health_token"}
    if daemon:
        keys |= {"site_packages", "metadata_link"}
    if set(value) != keys:
        raise ValueError("Invalid updater configuration")
    if value["topology"] not in {"systemd-user-slots-v1", "reachy-daemon-slots-v1"} or value["status_url"] != "http://127.0.0.1:8042/api/updates/health":
        raise ValueError("Unsupported install topology")
    for key in ("state", "slots", "current", "python"):
        p = Path(value[key])
        if not p.is_absolute() or ".." in p.parts:
            raise ValueError("Updater paths must be absolute")
    if not re.fullmatch(r"[0-9a-f]{64}", str(value["health_token"])):
        raise ValueError("An independent health token is required")
    slots = Path(value["slots"]).resolve()
    current = Path(value["current"])
    if not current.is_symlink() or current.resolve().parent != slots:
        raise ValueError("Current app must be an immutable managed slot")
    if Path(value["python"]).parent.name != "bin" or not Path(value["python"]).is_file():
        raise ValueError("An explicit dependency venv is required")
    venv = Path(value["python"]).parent.parent
    if not (venv / "pyvenv.cfg").is_file() or venv.resolve().is_relative_to(slots):
        raise ValueError("Dependency venv must be independent of app slots")
    if any(re.search(r"[\s;{}]", value[key]) for key in ("state", "slots", "current", "python")):
        raise ValueError("Managed paths cannot contain whitespace or systemd delimiters")
    if daemon:
        site = Path(value["site_packages"])
        metadata = Path(value["metadata_link"])
        valid_sites = {venv / "lib" / f"python{minor}" / "site-packages" for minor in ("3.11", "3.12")}
        if (site not in valid_sites or not site.is_dir() or metadata.parent != site
                or not re.fullmatch(r"reachy_mini_homebody-\d+\.\d+\.\d+\.dist-info", metadata.name)
                or not (site / "homebody").is_symlink()
                or (site / "homebody").readlink() != current / "homebody"
                or not metadata.is_symlink() or metadata.readlink() != current / ".homebody-dist-info"
                or list(site.glob("reachy_mini_homebody-*.dist-info")) != [metadata]
                or not (current / ".homebody-dist-info").is_symlink()
                or (current / ".homebody-dist-info").resolve().parent != current.resolve()):
            raise ValueError("Daemon app import/metadata links do not match the managed topology")
    return value


def driver_for(value: dict):
    if value.get("topology") == "reachy-daemon-slots-v1":
        from .update_daemon import DaemonSlots
        return DaemonSlots(value)
    return SystemdSlots(value)


class SystemdSlots:
    def __init__(self, value: dict):
        self.value = value
        self.current = Path(value["current"])
        self.slots = Path(value["slots"])
        self.python = value["python"]

    def command(self, *args: str) -> str:
        result = subprocess.run(["systemctl", "--user", *args, UNIT], check=True,
                                capture_output=True, text=True, timeout=120)
        return result.stdout.strip()

    def preflight(self) -> None:
        if not compatible_host():
            raise ValueError("Supported host is Linux / Python 3.11 or 3.12 / Reachy SDK 1.9.x")
        result = subprocess.run([self.python, "-c", "import importlib.metadata as m,sys; "
                                 "assert sys.version_info[:2] in ((3,11),(3,12)); "
                                 "print(m.version('reachy-mini'))"], check=True,
                                capture_output=True, text=True, timeout=15, env=dependency_env(), cwd="/")
        if not re.fullmatch(r"1\.9\.\d+", result.stdout.strip()):
            raise ValueError("Unsupported Reachy SDK")
        properties = dict(line.split("=", 1) for line in self.command(
            "show", "--property=ExecStart,Environment,EnvironmentFiles,WorkingDirectory,Restart,FragmentPath,"
            "ExecStartPre,ExecStartPost,ExecStop,PassEnvironment,UnsetEnvironment"
        ).splitlines() if "=" in line)
        command = re.fullmatch(r"\{ path=([^ ;]+) ; argv\[\]=(.*?) ; ignore_errors=no ;[^{}]*\}",
                               properties.get("ExecStart", ""))
        environment = shlex.split(properties.get("Environment", ""))
        env = dict(item.split("=", 1) for item in environment if "=" in item)
        cwd = properties.get("WorkingDirectory", "")
        if (not command or command[1] != self.python
                or shlex.split(command[2]) != [self.python, "-m", "homebody.main"]
                or env.get("PYTHONPATH") != str(self.current) or env.get("PYTHONNOUSERSITE") != "1"
                or "PYTHONHOME" in env or len(env) != len(environment)
                or properties.get("Restart") != "no" or properties.get("EnvironmentFiles") != ""
                or any(properties.get(key) != "" for key in
                       ("ExecStartPre", "ExecStartPost", "ExecStop", "PassEnvironment", "UnsetEnvironment"))
                or not Path(cwd).is_absolute()
                or Path(cwd).resolve().is_relative_to(self.slots.resolve())
                or not Path(properties.get("FragmentPath", "")).is_absolute()):
            raise ValueError("App service does not match the configured isolated slot topology")
        worker_pid = self.command("show", "--property=MainPID", "--value")
        if worker_pid == str(os.getpid()):
            raise ValueError("Updater must run outside the app service")

    def stop(self) -> None:
        self.command("stop")
        # A successful systemctl return alone does not prove the process is gone.
        if self.command("show", "--property=MainPID", "--value") != "0":
            raise ValueError("App did not stop")

    def start(self) -> None:
        self.command("start")

    def switch(self, slot: Path) -> None:
        if slot.resolve().parent != self.slots.resolve() or not slot.is_dir():
            raise ValueError("Unsafe app slot")
        temp = self.current.with_name(self.current.name + ".next")
        temp.unlink(missing_ok=True)
        temp.symlink_to(slot)
        os.replace(temp, self.current)
        directory = os.open(self.current.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def install(self, wheel: Path, slot: Path) -> None:
        # Dependencies/daemon/bridge are never upgraded. Check ALL declared requirements beforehand.
        from email.parser import BytesParser
        from zipfile import ZipFile

        from packaging.requirements import Requirement

        with ZipFile(wheel) as archive:
            meta = BytesParser().parsebytes(archive.read(next(n for n in archive.namelist()
                                                             if n.endswith(".dist-info/METADATA"))))
        script = (
            "import importlib.metadata as m,json,sys,os,platform; "
            "print(json.dumps({'installed':{d.metadata['Name'].lower():d.version for d in m.distributions()},"
            "'markers':{'os_name':os.name,'sys_platform':sys.platform,'platform_machine':platform.machine(),"
            "'platform_release':platform.release(),'platform_system':platform.system(),"
            "'platform_version':platform.version(),'python_version':'.'.join(platform.python_version_tuple()[:2]),"
            "'python_full_version':platform.python_version(),'implementation_name':sys.implementation.name,"
            "'implementation_version':platform.python_version(),'platform_python_implementation':"
            "platform.python_implementation(),'extra':''}}))"
        )
        probe = json.loads(subprocess.run([self.python, "-c", script], check=True, capture_output=True,
                                         text=True, timeout=30, env=dependency_env(), cwd="/").stdout)
        installed = probe["installed"]
        for line in meta.get_all("Requires-Dist", []):
            req = Requirement(line)
            if req.marker and not req.marker.evaluate(probe["markers"]):
                continue
            name = re.sub(r"[-_.]+", "-", req.name.lower())
            found = next((v for k, v in installed.items() if re.sub(r"[-_.]+", "-", k) == name), None)
            if req.url or found is None or found not in req.specifier:
                raise ValueError("Target dependencies differ from the provisioned runtime; update them manually")
        subprocess.run([self.python, "-m", "pip", "install", "--no-deps", "--no-index", "--no-compile",
                        "--target", str(slot), str(wheel)], check=True, capture_output=True, timeout=120,
                       env=dependency_env(), cwd="/")

    def check_manifest(self, manifest: dict) -> None:
        result = subprocess.run([self.python, "-c", "import sys; print('%d.%d'%sys.version_info[:2])"],
                                check=True, capture_output=True, text=True, timeout=15,
                                env=dependency_env(), cwd="/")
        if result.stdout.strip() not in manifest["python"].split(","):
            raise ValueError("Release Python compatibility does not include the target interpreter")

    def healthy(self, identity: dict, deadline: float = 90) -> bool:
        end = time.monotonic() + deadline
        successes = 0
        with httpx.Client(timeout=3, trust_env=False) as client:
            while time.monotonic() < end:
                try:
                    response = client.get(self.value["status_url"],
                                          headers={"Authorization": f"Bearer {self.value['health_token']}"})
                    response.raise_for_status()
                    payload = response.json()
                    exact = all(payload.get("release", {}).get(k) == identity.get(k)
                                for k in ("version", "release_id", "commit"))
                    if payload.get("app") == "homebody" and exact and payload.get("healthy") is True:
                        successes += 1
                        if successes >= 3:
                            return True
                    else:
                        successes = 0
                except (httpx.HTTPError, ValueError):
                    successes = 0
                time.sleep(1)
        return False


def run_job(store: Store, driver: SystemdSlots, download=None) -> None:
    """Resume queued work, or conservatively rollback any interrupted replacement."""
    job = store.read()["job"]
    if not job or job["state"] not in ACTIVE:
        return
    prior = job.get("prior")
    switched = bool(prior)
    try:
        driver.preflight()
        if job["state"] != "queued":
            # A killed worker cannot prove which install/start operation completed. Never guess success.
            if prior:
                raise ValueError("Interrupted update; restoring prior artifact")
            raise ValueError("Interrupted before app replacement; recovering unchanged app")
        manifest = validate_manifest(job["manifest"])
        driver.check_manifest(manifest)
        driver.slots.mkdir(parents=True, exist_ok=True)
        wheel = store.root / manifest["artifact"]
        prior = str(driver.current.resolve())
        prior_identity_path = driver.current / "homebody/release.json"
        prior_identity = json.loads(prior_identity_path.read_text()) if prior_identity_path.exists() else {
            "version": job["from_version"], "release_id": None, "commit": None}
        store.change("downloading", prior=prior, prior_identity=prior_identity)
        if download is None:
            manifest_url = f"https://github.com/{REPOSITORY}/releases/download/{manifest['tag']}/homebody-update.json"
            if validate_manifest(json.loads(get(manifest_url, 150000))) != manifest:
                raise ValueError("Published manifest changed since confirmation")
            url = f"https://github.com/{REPOSITORY}/releases/download/{manifest['tag']}/{manifest['artifact']}"
            wheel.write_bytes(get(url, manifest["size"]))
        else:
            download(wheel)
        verify_wheel(wheel, manifest)
        slot = driver.slots / job["id"]
        if slot.exists():
            raise ValueError("Target slot already exists")
        store.change("installing")
        driver.install(wheel, slot)
        # Persist rollback pointer BEFORE stopping or switching, so power loss always retains recovery.
        prior = str(driver.current.resolve())
        prior_identity_path = driver.current / "homebody/release.json"
        prior_identity = json.loads(prior_identity_path.read_text()) if prior_identity_path.exists() else {
            "version": job["from_version"], "release_id": None, "commit": None}
        store.change("stopping", prior=prior, prior_identity=prior_identity)
        switched = True
        driver.stop()
        driver.switch(slot)
        store.change("starting")
        driver.start()
        store.change("verifying")
        if not driver.healthy(manifest):
            raise ValueError("Exact target release startup/Standby health was not verified")
        store.finish("succeeded")
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
        if switched and prior:
            store.change("rolling_back", error=error)
            try:
                driver.stop()
                driver.switch(Path(prior))
                driver.start()
                identity = store.read()["job"].get("prior_identity", {})
                if not driver.healthy(identity):
                    raise ValueError("Prior artifact health could not be verified")
                store.finish("rolled_back", error)
            except Exception as rollback:
                store.finish("rollback_failed", error + f"; rollback: {type(rollback).__name__}")
        else:
            try:
                # The app deliberately quiesced on admission, even if download/install never completed.
                driver.stop()
                driver.start()
                identity_path = driver.current / "homebody/release.json"
                identity = store.read()["job"].get("prior_identity")
                if identity is None:
                    identity = json.loads(identity_path.read_text()) if identity_path.exists() else {
                        "version": job["from_version"], "release_id": None, "commit": None}
                if not driver.healthy(identity):
                    raise ValueError("Unchanged app did not recover")
                store.finish("failed", error)
            except Exception as recovery:
                store.finish("rollback_failed", error + f"; recovery: {type(recovery).__name__}")


def serve(path: Path, once: bool = False) -> None:
    value = config(path)
    store = Store(Path(value["state"]))
    store.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (store.root / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another updater process owns this deployment") from exc
        driver = driver_for(value)
        from .updates import atomic_json
        stopped = threading.Event()

        def heartbeat():
            while not stopped.is_set():
                atomic_json(store.root / "worker.json", {"pid": os.getpid(), "heartbeat": time.time()})
                stopped.wait(HEARTBEAT_INTERVAL)

        atomic_json(store.root / "worker.json", {"pid": os.getpid(), "heartbeat": time.time()})
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            while True:
                with contextlib.suppress(Exception):
                    run_job(store, driver)
                if once:
                    return
                time.sleep(2)
        finally:
            stopped.set()
            thread.join()
            (store.root / "worker.json").unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    serve(args.config, args.once)


if __name__ == "__main__":
    main()
