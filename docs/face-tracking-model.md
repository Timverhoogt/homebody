# Face following: pinned model readiness and recovery

**Local face following** asks the Reachy Mini daemon to track faces. `start_head_tracking` only sends a command to the daemon; the tracker thread runs inside the daemon process. Homebody therefore reports the state as **Requested · not confirmed running**, never as running. If the command cannot be sent, it reports **command failed**. No face in view is not a failure.

## The dependency

The daemon's SDK `FaceDetector` downloads one pinned file on first use:

| Field | Value |
| --- | --- |
| Repository | `pollen-robotics/face_detection_yunet_2026may` (public, not gated) |
| File | `face_detection_yunet_2026may.onnx` |
| Revision | `2b8e922362946a0db67e861bae0f77826980effd` |
| Size / SHA-256 | 229,738 bytes / `ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0` |

The SDK calls `hf_hub_download(repo, file, revision=...)` without `token=False`. If that request fails (an HTTP 401 was observed on a robot), the daemon logs `Face tracker crashed.` and tracking does not run. Possible causes include a stale implicit Hugging Face credential, a cache owned by another user or a proxy; none of these has been proven. Anonymous access to the pinned file was verified from a separate host.

With the exact commit already present in the **daemon user's** Hugging Face cache, `hf_hub_download` is answered from that cache. A cached file that does not match the pinned digest is reported as a mismatch, never as ready.

## Check and preprovision (manual, owner-supervised)

Homebody does not change services, credentials or the daemon's environment. Run these manually as the user that runs the Reachy Mini daemon, with the same `HF_HOME`/`HF_HUB_CACHE` as that daemon (inspect its service definition first):

```bash
# Offline: no network, camera or model load. Exit 0 = verified, 1 = missing/mismatch.
python -m homebody.face_model check --cache-dir "<daemon HF hub cache>"
# Anonymous (token=False) pinned download into a private staging directory; only bytes matching
# the pinned size and SHA-256 are installed into the cache. Existing entries are never overwritten.
python -m homebody.face_model provision --cache-dir "<daemon HF hub cache>"
```

`mismatch` or `unreadable` on an existing entry means the cached file (or its snapshot link) must be inspected and removed by hand before provisioning again. Neither command reports daemon health.

After `verified`, restart face following through the normal supervised app/daemon controls and confirm on the robot that it follows a face. The app-status cache check (`face_model_app_cache`) covers **this app's** user cache only. The daemon may use a different cache, so it is advice, not proof.

Source-only changes do not repair a deployed robot. Recovery counts as complete only after an owner has verified the daemon cache and supervised face following on the robot.
