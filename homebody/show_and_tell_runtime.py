"""Kids Chat Show and Tell for the voice runtime: Owner permission, Look now and voice requests.

``ShowAndTellMixin`` is mixed into ``HermesVoiceRuntime``. Show and Tell is available in an
eligible Kids Chat automatically while the Owner's saved "Camera access for AI" approval
(``camera_enabled``) and the persistent Show and Tell setting (``kids_show_and_tell_enabled``,
an opt-out) both allow it. Nothing here captures, wakes or listens on startup, on enabling or on
a permission change: only an individual explicit request can start a look.

It never takes the motor lock and never moves Reachy. Lock order: the gate snapshots its
authority (``_kids_lock`` then ``_conversation_stop_generation_lock``) before its own lock, and it
is never called while ``_conversation_stop_generation_lock`` is held.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace

from .config import AppConfig
from .show_and_tell import (
    Authority,
    Grant,
    LookResult,
    Permit,
    SessionGate,
    Unavailable,
    explicit_look_request,
    run_look,
)

_LOGGER = logging.getLogger("homebody.runtime")


class ShowAndTellUnavailable(RuntimeError):
    """A safe categorical caregiver-facing error; never carries child or provider data."""


class _Look:
    """One bridge permit and the client that owns its exact speech approvals."""

    __slots__ = ("announcement", "client", "permit")

    def __init__(self, client: object, permit: str, announcement: str) -> None:
        self.client = client
        self.permit = permit
        self.announcement = announcement

    def __repr__(self) -> str:
        return "_Look(<private>)"


class _RuntimeRoutes:
    """Real adapters: bridge permits/vision/moderation, the single camera owner and Kids TTS."""

    def __init__(self, runtime: ShowAndTellMixin, generation: int, config: AppConfig, *, turn_bound: bool) -> None:
        self._runtime = runtime
        self._generation = generation
        self._config = config
        self._turn_bound = turn_bound

    def authorize(self, permit: Permit, require_current) -> _Look:
        client = self._runtime._new_bridge_client(self._config)
        try:
            require_current()
            grant = permit.grant
            ttl = int(max(1.0, min(3600.0, permit.authority.expires - time.monotonic())))
            # Revocation/Stop notifications follow the grant that was actually published.
            self._runtime._show_tell_scope = (grant, self._config)
            bridge_permit, announcement = client.show_tell_permit(grant.token, grant.serial, ttl)
            require_current()
            return _Look(client, bridge_permit, announcement)
        except BaseException:
            client.close()
            raise

    def speak_fixed(self, look: _Look, require_current) -> None:
        self._speak(look, look.announcement, require_current)

    def capture_once(self, require_current) -> bytes:
        runtime = self._runtime
        with runtime._kids_lock:
            if not runtime._kids_callback_is_current(self._generation):
                raise Unavailable()
            runtime._kids_camera_active = True
        try:
            require_current()
            runtime._set_status("looking", "Show and Tell: looking once")
            return runtime._capture_camera_jpeg(kids_generation=self._generation, show_tell=require_current)
        finally:
            with runtime._kids_lock:
                if runtime._kids_generation == self._generation:
                    runtime._kids_camera_active = False

    def describe(self, frame: bytes, look: _Look, require_current) -> LookResult:
        require_current()
        runtime = self._runtime
        runtime._set_status("thinking", "Show and Tell: checking the picture")
        text, recognized = look.client.show_tell_look(look.permit, frame)
        require_current()
        return LookResult(text, recognized)

    def speak_approved(self, text: str, look: _Look, require_current) -> None:
        self._speak(look, text, require_current)

    def release(self, look: _Look, *, completed: bool) -> None:
        try:
            if not completed:
                look.client.show_tell_cancel()  # the bridge then discards a held provider result
        except Exception:
            _LOGGER.warning("Could not cancel the Show and Tell look on the bridge")
        finally:
            look.client.close()

    def _speak(self, look: _Look, text: str, require_current) -> None:
        """Exact approved Kids speech only; no fallback voice, barge-in or listening."""
        runtime = self._runtime
        require_current()
        if runtime._ack_output_quarantined():
            raise Unavailable()  # a blocked filler push owns the output: never speak or capture over it

        def still_current() -> bool:
            try:
                require_current()
            except Unavailable:
                return False
            return True

        runtime._set_status("speaking", "Show and Tell", tts_provider="elevenlabs-flash-stream")
        interrupted = runtime._play_kids_stream(
            look.client, text, barge_in=False, animate_motion=False,
            still_current=still_current, turn_bound=self._turn_bound,
        )
        # Every early return of the stream is also a gate change, so passing here means it played.
        require_current()
        if interrupted:
            raise Unavailable()


class ShowAndTellMixin:
    """Owner-permitted, explicit-request-only, one-frame Show and Tell inside Kids Chat (Buddy)."""

    def _init_show_tell_state(self) -> None:
        self._show_tell_permission_lock = threading.Lock()
        # (camera approved, Show and Tell setting on, permission generation); RAM mirror of saved config.
        self._show_tell_permission: tuple[bool, bool, int] = (False, False, 0)
        self._show_tell_scope: tuple[Grant, AppConfig] | None = None  # last grant published to the bridge
        self._show_tell_gate = SessionGate(self._show_tell_authority, on_revoke=self._show_tell_revoked)
        self.show_tell_sync_permission()  # reads saved settings only: no capture, wake or microphone

    def show_tell_sync_permission(self, *, revoke: bool = False) -> dict[str, bool]:
        """Re-derive the standing permission from the saved Owner settings.

        Reading and applying are serialized, so whichever caller applies last applied the newest
        saved settings; a slower, stale caller can never restore an older permission. ``revoke``
        marks a saved revocation, which always cancels outstanding work even if a later save
        re-enabled it. Enabling never captures, wakes or opens the microphone.
        """
        with self._show_tell_permission_lock:
            try:
                config = self.config_loader()
                camera = config.camera_enabled is True
                setting = config.kids_show_and_tell_enabled is True
            except Exception:
                camera = setting = False  # unreadable settings fail closed
            old_camera, old_setting, generation = self._show_tell_permission
            if revoke or (camera, setting) != (old_camera, old_setting):
                generation += 1
            self._show_tell_permission = (camera, setting, generation)
        if revoke or not (camera and setting):
            self._show_tell_gate.revoke()
        return self._show_tell_public()

    def _show_tell_authority(self) -> Authority:
        camera, setting, permission_generation = self._show_tell_permission
        with self._kids_lock:
            profile = self._kids_profile
            if not self._kids_active or profile is None or profile.host:
                return Authority("", -1, -1, -1, 0.0, "", "", permission_generation=permission_generation)
            session_id, generation, expires = self._kids_session_id, self._kids_generation, self._kids_ends_at
            activity, language = profile.activity, profile.language
        with self._conversation_stop_generation_lock:
            stop_generation = self._conversation_stop_generation
            turn_generation = self._turn_stop_generation
        return Authority(
            session_id, generation, stop_generation, turn_generation, expires, activity, language,
            permitted=camera and setting, permission_generation=permission_generation,
            private=self._privacy_requested.is_set() or self._effective_power_mode() in {"meeting", "sleep"},
            stopped=self.stop_event.is_set(),
        )

    def _show_tell_public(self) -> dict[str, bool]:
        """Categorical owner-safe state only; never a session id, frame, grant or description."""
        camera, setting, _generation = self._show_tell_permission
        state = self._show_tell_gate.public_state()
        return {
            "camera_approved": camera,
            "enabled": setting,
            "session": state["session"],
            "available": state["available"],
            "looking": state["looking"],
        }

    def _show_tell_notify(self, config: AppConfig, action: str, serial: int) -> None:
        client = self._new_bridge_client(config)
        try:
            if action == "revoke":
                client.show_tell_revoke(serial)
            else:
                client.show_tell_cancel()
        except Exception:
            # End/expiry also clear the bridge record; this is the earlier, explicit path.
            _LOGGER.warning("Could not publish a Show and Tell %s to the bridge", action)
        finally:
            client.close()

    def _show_tell_schedule(self, config: AppConfig, action: str, serial: int) -> None:
        threading.Thread(
            target=self._show_tell_notify, args=(config, action, serial),
            name=f"kids-show-and-tell-{action}", daemon=True,
        ).start()

    def _show_tell_revoked(self, grant: Grant) -> None:
        """Called under the gate lock: only schedule bridge clean-up, never block or take locks."""
        scope = self._show_tell_scope
        if scope is None or scope[0] is not grant:
            return  # never published: the bridge holds nothing for it
        self._show_tell_scope = None
        self._show_tell_schedule(scope[1], "revoke", grant.serial)

    def _show_tell_stop_requested(self) -> None:
        """Every conversation Stop: drop the bridge's in-flight look and description, keep the setting.

        Lock-free (callers may hold _kids_lock): the robot look itself is already fenced by the new
        Stop generation. A later explicit request may look again under the same permission.
        """
        scope = getattr(self, "_show_tell_scope", None)  # Stop may run before this mixin's state exists
        if scope is not None:
            self._show_tell_schedule(scope[1], "cancel", scope[0].serial)

    def _show_tell_routes(self, *, turn_bound: bool) -> _RuntimeRoutes:
        self.show_tell_sync_permission()  # each look re-reads the current saved permission
        base = self.config_loader()
        with self._kids_lock:
            generation = self._kids_generation
        config = replace(self._kids_voice_config(base), continuous_conversation=False, barge_in_enabled=False)
        return _RuntimeRoutes(self, generation, config, turn_bound=turn_bound)

    def show_tell_look_now(self) -> dict[str, object]:
        """Owner Look now: one explicit look as the voice owner; never wakes or opens the microphone."""
        state = self._show_tell_public()
        if not state["available"]:
            raise ShowAndTellUnavailable(
                "Show and Tell works in an active Kids Chat while Camera access for AI and Show and Tell are on"
            )
        if not self._voice_activity_lock.acquire(blocking=False):
            raise ShowAndTellUnavailable("Wait until Reachy finishes the current turn")
        try:
            if self._announcement_active.is_set():
                raise ShowAndTellUnavailable("Wait until Reachy finishes speaking")
            routes = self._show_tell_routes(turn_bound=False)
            worker = threading.Thread(
                target=self._show_tell_owned_look, args=(routes,), name="kids-show-and-tell", daemon=True,
            )
            worker.start()  # the worker now owns, and releases, the voice slot
        except BaseException:
            self._voice_activity_lock.release()
            raise
        return {"accepted": True, **self._show_tell_public()}

    def _show_tell_owned_look(self, routes: _RuntimeRoutes) -> None:
        try:
            run_look(self._show_tell_gate, routes, None)
        except Unavailable:
            _LOGGER.info("Show and Tell look ended without a result")
        finally:
            self._voice_activity_lock.release()
            self._set_status("waiting_for_wake_word", "Show and Tell finished")

    def _show_tell_requested(self, config: AppConfig, transcript: str) -> bool:
        """An explicit EN/NL request in an unhosted Kids Chat while the Owner permission allows it."""
        return (
            config.kids_mode_enabled and config.kids_activity == "buddy" and not config.kids_host_enabled
            and explicit_look_request(transcript, config.language)
            and self._show_tell_public()["available"]
        )

    def _show_tell_voice_turn(self) -> bool:
        """Run one look inside the current voice turn, which already owns the voice slot."""
        routes = self._show_tell_routes(turn_bound=True)
        try:
            run_look(self._show_tell_gate, routes, None)
            return True
        except Unavailable:
            _LOGGER.info("Show and Tell look ended without a result")
            return False
