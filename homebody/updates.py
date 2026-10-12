"""Trusted release discovery and atomic update journal. No installer runs in the app process."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import uuid
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

REPOSITORY = "Timverhoogt/homebody"
API = f"https://api.github.com/repos/{REPOSITORY}/releases"
ACTIVE = {"queued", "downloading", "stopping", "installing", "starting", "verifying", "rolling_back"}
MANIFEST_KEYS = {"schema", "repository", "version", "tag", "release_id", "commit", "python", "platform",
                 "sdk", "artifact", "sha256", "size", "notes"}
MAX_WHEEL = 100 * 1024 * 1024


def version(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value):
        raise ValueError("Only stable three-part release versions are supported")
    return tuple(map(int, value.split(".")))


def validate_manifest(data: dict) -> dict:
    if not isinstance(data, dict) or set(data) != MANIFEST_KEYS:
        raise ValueError("Invalid release manifest schema")
    version(data["version"])
    if (data["schema"] != 1 or type(data["schema"]) is not int or data["repository"] != REPOSITORY
            or data["tag"] != "v" + data["version"] or type(data["release_id"]) is not int
            or data["release_id"] <= 0 or not re.fullmatch(r"[0-9a-f]{40}", str(data["commit"]))
            or data["python"] not in {"3.11", "3.12", "3.11,3.12"}
            or data["platform"] != "linux" or data["sdk"] != "1.9"
            or data["artifact"] != f"reachy_mini_homebody-{data['version']}-py3-none-any.whl"
            or not re.fullmatch(r"[0-9a-f]{64}", str(data["sha256"]))
            or type(data["size"]) is not int or not 0 < data["size"] <= MAX_WHEEL
            or not isinstance(data["notes"], str) or len(data["notes"]) > 100000):
        raise ValueError("Incompatible or untrusted release manifest")
    return data


def get(url: str, limit: int) -> bytes:
    """Bound downloads and verify every HTTPS redirect before following it."""
    hosts = {"api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        for _ in range(6):
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.port:
                raise ValueError("Untrusted release download URL")
            with client.stream("GET", url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "Homebody-Updater/1"}) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                response.raise_for_status()
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > limit:
                        raise ValueError("Release download exceeds size limit")
                return bytes(chunks)
    raise ValueError("Too many release download redirects")


def asset_url(release: dict, name: str) -> str:
    matches = [a for a in release.get("assets", []) if a.get("name") == name]
    expected = f"https://github.com/{REPOSITORY}/releases/download/{release['tag_name']}/{name}"
    if len(matches) != 1 or matches[0].get("browser_download_url") != expected:
        raise ValueError("Missing or untrusted release artifact")
    return expected


def discover(current: str) -> dict:
    """Fetch complete old-to-new notes; never treat drafts/prereleases or a branch as a release."""
    old = version(current)
    releases = []
    for page in range(1, 21):
        batch = json.loads(get(f"{API}?per_page=100&page={page}", 3 * 1024 * 1024))
        if not isinstance(batch, list):
            raise ValueError("Invalid published release listing")
        releases.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise ValueError("Release history is incomplete; cannot preview accumulated notes")
    stable = []
    for release in releases:
        if release.get("draft") is not False or release.get("prerelease") is not False:
            continue
        try:
            tag = release["tag_name"]
            v = version(tag[1:] if tag.startswith("v") else "")
        except (KeyError, ValueError):
            continue
        if v > old:
            stable.append(release)
    stable.sort(key=lambda r: version(r["tag_name"][1:]))
    if not stable:
        return {"available": None, "notes": []}
    release = stable[-1]
    manifest = validate_manifest(json.loads(get(asset_url(release, "homebody-update.json"), 150000)))
    if manifest["release_id"] != release["id"] or manifest["tag"] != release["tag_name"]:
        raise ValueError("Manifest release identity mismatch")
    # GitHub's annotated tag may target a branch name; resolve the actual immutable commit via API.
    commit = json.loads(get(f"https://api.github.com/repos/{REPOSITORY}/commits/{manifest['tag']}", 500000))
    if commit.get("sha") != manifest["commit"]:
        raise ValueError("Manifest commit identity mismatch")
    asset_url(release, manifest["artifact"])
    notes = [{"version": r["tag_name"][1:], "notes": str(r.get("body") or "No release notes provided.")[:100000]}
             for r in stable]
    return {"available": manifest, "notes": notes}


def require_safe(runtime: dict) -> None:
    kids = runtime.get("kids_mode", {})
    if (runtime.get("power_mode") != "standby" or runtime.get("motors_enabled") is not False
            or runtime.get("head_safely_folded") is not True
            or runtime.get("state") not in {"waiting_for_wake_word", "waiting_for_configuration"}
            or runtime.get("last_error") != ""
            or runtime.get("robot_action_busy") is not False or runtime.get("camera_control_active") is not False
            or runtime.get("announcement_busy") is not False or runtime.get("announcement_queue_depth") != 0
            or kids.get("active") is not False or kids.get("locked") is not False):
        raise ValueError("Update requires verified folded Standby with no Kids, voice, camera or movement activity")


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        with temp.open("x", encoding="utf-8") as stream:
            os.chmod(temp, 0o600)
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "journal.json"

    @contextlib.contextmanager
    def transaction(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / "journal.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = json.loads(self.path.read_text()) if self.path.exists() else {"job": None, "history": []}
            yield data
            atomic_json(self.path, data)

    def _snapshot(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"job": None, "history": []}

    def read(self) -> dict:
        # Observation must not enter the write transaction: a privileged
        # maintenance read used to atomically replace the journal as root.
        # Reuse an existing lock read-only, but never create a directory/lock.
        try:
            lock = (self.root / "journal.lock").open("r")
        except FileNotFoundError:
            # Every writer replaces a fully fsynced file atomically, so a
            # snapshot is coherent even before the first lock is created.
            return self._snapshot()
        with lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            return self._snapshot()

    def enqueue(self, manifest: dict, current: str, notes: list) -> dict:
        validate_manifest(manifest)
        if version(manifest["version"]) <= version(current):
            raise ValueError("Target is not a newer stable version")
        with self.transaction() as data:
            if data["job"] and data["job"]["state"] in ACTIVE:
                raise ValueError("An update is already running")
            job = {"id": uuid.uuid4().hex, "state": "queued", "manifest": manifest,
                   "from_version": current, "notes": notes, "error": "", "acknowledged": False}
            data["job"] = job
        return job

    def change(self, state: str, **fields: Any) -> None:
        with self.transaction() as data:
            data["job"].update(state=state, **fields)

    def finish(self, state: str, error: str = "") -> None:
        with self.transaction() as data:
            data["job"].update(state=state, error=error)
            if not any(h["id"] == data["job"]["id"] for h in data["history"]):
                data["history"].append(dict(data["job"]))

    def acknowledge(self, job_id: str) -> None:
        with self.transaction() as data:
            for job in [data["job"], *data["history"]]:
                if job and job["id"] == job_id and job["state"] == "succeeded":
                    job["acknowledged"] = True


def verify_wheel(path: Path, manifest: dict) -> None:
    validate_manifest(manifest)
    if path.stat().st_size != manifest["size"] or hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValueError("Artifact size or digest mismatch")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if any(n.startswith("/") or ".." in Path(n).parts or "\\" in n for n in names):
            raise ValueError("Unsafe wheel path")
        if sum(i.file_size for i in archive.infolist()) > 300 * 1024 * 1024:
            raise ValueError("Wheel expansion exceeds limit")
        metadata = [n for n in names if n.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ValueError("Invalid wheel metadata")
        meta = BytesParser().parsebytes(archive.read(metadata[0]))
        if (re.sub(r"[-_.]+", "-", str(meta["Name"]).lower()) != "reachy-mini-homebody"
                or meta["Version"] != manifest["version"]):
            raise ValueError("Wheel package identity mismatch")
        identity = json.loads(archive.read("homebody/release.json"))
        if any(identity.get(k) != manifest[k] for k in ("version", "release_id", "commit")):
            raise ValueError("Wheel release identity mismatch")


def installed_identity() -> dict:
    from . import __version__
    path = Path(__file__).with_name("release.json")
    if not path.exists():
        return {"version": __version__, "release_id": None, "commit": None}
    identity = json.loads(path.read_text())
    if identity.get("version") != __version__:
        return {"version": __version__, "release_id": None, "commit": None}
    return identity


def configured_path() -> Path | None:
    value = os.environ.get("HOMEBODY_UPDATER_CONFIG")
    if value:
        return Path(value)
    # Existing daemon HOME/config ownership is retained; no startup environment mutation.
    from .config import default_config_path
    candidate = default_config_path().with_name("updater.json")
    return candidate if candidate.is_file() else None


def compatible_host() -> bool:
    return sys.platform == "linux" and sys.version_info[:2] in {(3, 11), (3, 12)}
