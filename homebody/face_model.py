"""Offline readiness check and explicit preprovisioning of the SDK's pinned face-detection model.

Head tracking runs in the Reachy Mini daemon, not in this app: ``start_head_tracking`` only sends
a command. The daemon's ``FaceDetector`` calls ``hf_hub_download(repo, file, revision=<commit>)``
without ``token=False``, so a stale implicit credential, a proxy or no network can make that first
download fail (an HTTP 401 was observed) and the tracker thread then crashes. With the exact pinned
commit already in the daemon user's Hugging Face cache, that call is answered from the cache.

This module checks and fills such a cache: pinned repository/file/commit, anonymous download
(``token=False``) and SHA-256 verification. It never opens a camera, loads the model, reads or
writes credentials, or changes services. ``check`` is offline and cheap (one ~230 kB hash).

Usage, run as the daemon's user with its cache (see docs/face-tracking-model.md)::

    python -m homebody.face_model check [--cache-dir DIR]
    python -m homebody.face_model provision [--cache-dir DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

# Exactly the SDK constants (reachy_mini/vision/face_detector.py) plus the verified public LFS digest.
REPO = "pollen-robotics/face_detection_yunet_2026may"
FILENAME = "face_detection_yunet_2026may.onnx"
REVISION = "2b8e922362946a0db67e861bae0f77826980effd"
SHA256 = "ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0"
SIZE = 229_738
STATES = ("verified", "missing", "mismatch", "unreadable")


def default_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """The hub cache ``hf_hub_download`` uses for this process's user and environment."""
    env = os.environ if env is None else env
    if env.get("HF_HUB_CACHE"):
        return Path(env["HF_HUB_CACHE"]).expanduser()
    if env.get("HF_HOME"):
        return Path(env["HF_HOME"]).expanduser() / "hub"
    base = Path(env["XDG_CACHE_HOME"]).expanduser() if env.get("XDG_CACHE_HOME") else Path.home() / ".cache"
    return base / "huggingface" / "hub"


def cached_path(cache_dir: Path) -> Path:
    """Where the pinned commit's file lives in a Hugging Face hub cache."""
    return Path(cache_dir) / f"models--{REPO.replace('/', '--')}" / "snapshots" / REVISION / FILENAME


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def check(cache_dir: Path | None = None) -> str:
    """Return ``verified``, ``missing``, ``mismatch`` or ``unreadable``; offline, no model load."""
    path = cached_path(default_cache_dir() if cache_dir is None else cache_dir)
    try:
        if path.is_symlink() and not path.exists():
            return "unreadable"  # dangling snapshot link: needs manual inspection
        if not path.exists():
            return "missing"
        if path.stat().st_size != SIZE:
            return "mismatch"
        return "verified" if _digest(path) == SHA256 else "mismatch"
    except OSError:
        return "unreadable"


def provision(cache_dir: Path | None = None, *, download: Callable[..., str] | None = None) -> str:
    """Anonymously fetch the pinned file, verify it, then install it into ``cache_dir``.

    Never sends a token (``token=False``). The download lands in a private staging cache and only
    bytes matching the pinned size and SHA-256 are installed (hub layout: content-addressed blob plus
    the exact-commit snapshot link), so an unverified file never reaches the daemon's cache. An
    existing entry is never overwritten: a mismatch there is reported for manual removal.
    """
    target = default_cache_dir() if cache_dir is None else Path(cache_dir)
    existing = check(target)
    if existing in {"verified", "mismatch", "unreadable"}:
        return existing
    if download is None:
        from huggingface_hub import hf_hub_download as download
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".homebody-face-model-", dir=target) as staging:
        downloaded = Path(download(REPO, FILENAME, revision=REVISION, cache_dir=staging, token=False))
        if downloaded.resolve() != cached_path(Path(staging)).resolve() or check(Path(staging)) != "verified":
            return "mismatch"  # staging is discarded; nothing was installed
        repo_dir = Path(target) / f"models--{REPO.replace('/', '--')}"
        blob = repo_dir / "blobs" / SHA256
        blob.parent.mkdir(parents=True, exist_ok=True)
        if not blob.exists():
            partial = blob.with_name(f"{SHA256}.incomplete-homebody")
            shutil.copyfile(downloaded, partial)
            os.replace(partial, blob)
        link = cached_path(target)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(os.path.relpath(blob, link.parent))
    return check(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m homebody.face_model", description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("check", "provision"))
    parser.add_argument("--cache-dir", type=Path, help="Hugging Face hub cache (default: HF_HUB_CACHE/HF_HOME)")
    args = parser.parse_args(argv)
    cache = args.cache_dir or default_cache_dir()
    try:
        state = provision(cache) if args.action == "provision" else check(cache)
    except Exception as exc:  # categorical only: never print provider bodies or credentials
        print(f"{args.action}: failed ({type(exc).__name__}); cache {cache}", file=sys.stderr)
        return 2
    # Only this cache was checked: the daemon is confirmed only by supervised face following.
    print(f"{REPO}@{REVISION[:12]} {FILENAME}: {state}; cache {cache} (not a daemon health check)")
    return 0 if state == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
