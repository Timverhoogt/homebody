"""HTTP client for the Hermes Reachy companion bridge."""

from __future__ import annotations

import base64
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field

import httpx

from .config import AppConfig
from .ispy import ISpyTarget, validate_ispy_target

_LOGGER = logging.getLogger(__name__)


class HermesBridgeError(RuntimeError):
    pass


@dataclass(slots=True)
class SpeechAudio:
    data: bytes
    content_type: str
    extension: str
    provider: str = "configured"


@dataclass(frozen=True, slots=True)
class AgentBrokerContext:
    """Authoritative Reachy state bound to one broker request."""

    capability_profile: str
    adult_ui_unlocked: bool
    kids_mode_active: bool
    power_mode: str
    privacy_enabled: bool
    emergency_stop_active: bool
    robot_available: bool
    session_generation: int
    requested_session_generation: int
    explicit_private_intent: bool
    reachy_status: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentBrokerResult:
    request_id: str
    capability_id: str
    data: object
    evidence: tuple[dict[str, object], ...]
    freshness: dict[str, object]
    read_only: bool
    side_effect: bool


class HermesBridgeClient:
    """Talk to Hermes without moving provider credentials onto the robot."""

    def __init__(self, config: AppConfig, *, client: httpx.Client | None = None) -> None:
        self.config = config
        self._client = client or httpx.Client(timeout=httpx.Timeout(90.0, connect=10.0), follow_redirects=False)
        self._owns_client = client is None
        self._session_id = self._new_session_id()
        self._last_turn_at = 0.0
        self.last_stt_provider = ""
        self.last_tts_provider = ""
        self._kids_speech_approval = ""
        self._kids_fallback_speech_approval = ""
        self.last_kids_next_action = ""
        self.last_kids_ispy_role = ""
        self.last_kids_ispy_phase = ""

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _new_session_id(self) -> str:
        return f"reachy-{self.config.instance_id[:16]}-{uuid.uuid4().hex[:12]}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "X-Reachy-Device-Id": self.config.instance_id,
            "X-Hermes-Session-Id": self._session_id,
            "X-Hermes-Session-Key": f"agent:main:reachy-mini:{self.config.instance_id}",
        }

    def _rotate_session_if_stale(self) -> None:
        now = time.monotonic()
        if self._last_turn_at and now - self._last_turn_at > self.config.conversation_timeout_seconds:
            self._session_id = self._new_session_id()
        self._last_turn_at = now

    def health(self) -> dict[str, object]:
        try:
            response = self._client.get(f"{self.config.bridge_url}/health", timeout=5.0)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise HermesBridgeError("Hermes bridge returned an invalid health payload")
            if payload.get("status") != "ok" or payload.get("agent_api", payload.get("hermes_api")) is False:
                failing = [
                    f"{item.get('label') or item.get('name')}: {item.get('error') or 'unavailable'}"
                    for item in payload.get("agent_backends", [])
                    if isinstance(item, dict) and item.get("ok") is not True
                ]
                detail = "; ".join(failing) or "its agent API is unavailable"
                raise HermesBridgeError(f"The bridge is running but {detail}")
            return payload
        except Exception as exc:
            raise HermesBridgeError(f"Hermes bridge is unavailable: {exc}") from exc

    def select_ispy_target(
        self,
        frames: list[bytes],
        *,
        session_id: str,
        age_band: str,
        language: str,
    ) -> ISpyTarget:
        """Send exactly five transient frames to the fixed child-safe vision route."""
        if len(frames) != 5 or any(not frame or len(frame) > 1_500_000 for frame in frames):
            raise HermesBridgeError("I Spy camera frame bounds were not met")
        response = self._client.post(
            f"{self.config.bridge_url}/v1/kids/ispy/select",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            json={
                "session_id": session_id,
                "age_band": age_band,
                "language": language,
                "frames_jpeg": [base64.b64encode(frame).decode("ascii") for frame in frames],
            },
            timeout=30.0,
        )
        self._raise_for_error(response, "I Spy target selection")
        payload = response.json()
        try:
            return validate_ispy_target(payload["target"], frame_count=len(frames))
        except (KeyError, TypeError, ValueError) as exc:
            raise HermesBridgeError("Hermes returned an unsafe or invalid I Spy target") from exc

    def ispy_clue(self, session_id: str) -> str:
        """Fetch the server-owned colour clue with exact child-speech approvals."""
        self._kids_speech_approval = ""
        self._kids_fallback_speech_approval = ""
        response = self._client.post(
            f"{self.config.bridge_url}/v1/kids/ispy/clue",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            json={"session_id": session_id},
            timeout=10.0,
        )
        self._raise_for_error(response, "I Spy colour clue")
        payload = response.json()
        if not isinstance(payload, dict):
            raise HermesBridgeError("Hermes returned an invalid I Spy clue")
        text = str(payload.get("text") or "").strip()
        approval = str(payload.get("speech_approval") or "").strip()
        fallback = str(payload.get("fallback_speech_approval") or "").strip()
        if not text or not approval or not fallback or payload.get("ispy_role") != "reachy_picker":
            raise HermesBridgeError("Hermes returned an incomplete I Spy clue approval")
        self._kids_speech_approval = approval
        self._kids_fallback_speech_approval = fallback
        self.last_kids_next_action = ""
        self.last_kids_ispy_role = "reachy_picker"
        self.last_kids_ispy_phase = "reachy_guessing"
        return text

    def cancel_ispy_session(self, session_id: str) -> None:
        """Best-effort deletion of bridge-side I Spy target and guess state."""
        if not session_id.startswith("kids-") or len(session_id) != 37:
            return
        response = self._client.post(
            f"{self.config.bridge_url}/v1/kids/ispy/cancel",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            json={"session_id": session_id},
            timeout=3.0,
        )
        self._raise_for_error(response, "I Spy session cancellation")

    def set_kids_session_state(self, session_id: str, *, active: bool) -> None:
        """Tell the bridge a Kids session started or ended so it can latch adult routes."""
        if not session_id.startswith("kids-") or len(session_id) != 37:
            return
        response = self._client.post(
            f"{self.config.bridge_url}/v1/kids/session",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            json={"session_id": session_id, "state": "active" if active else "ended"},
            timeout=3.0,
        )
        self._raise_for_error(response, "Kids session state")

    def voice_options(self) -> dict[str, object]:
        response = self._client.get(
            f"{self.config.bridge_url}/v1/voice-options",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
        )
        self._raise_for_error(response, "voice option discovery")
        payload = response.json()
        if not isinstance(payload, dict):
            raise HermesBridgeError("Hermes returned invalid voice options")
        return payload

    def transcribe(self, wav_data: bytes) -> str:
        response = self._client.post(
            f"{self.config.bridge_url}/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            data={
                "language": self.config.language,
                "provider": self.config.stt_provider,
                "model": self.config.stt_model,
            },
            files={"file": ("utterance.wav", wav_data, "audio/wav")},
        )
        self._raise_for_error(response, "transcription")
        payload = response.json()
        transcript = str(payload.get("text") or payload.get("transcript") or "").strip()
        self.last_stt_provider = str(payload.get("provider") or self.config.stt_provider)
        # Silence or unintelligible audio is a normal no-speech turn, not a bridge failure.
        return transcript

    def models(self) -> list[dict[str, object]]:
        response = self._client.get(
            f"{self.config.bridge_url}/v1/models",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
        )
        self._raise_for_error(response, "model discovery")
        payload = response.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(data, list):
            raise HermesBridgeError("Hermes returned an invalid model list")
        return [item for item in data if isinstance(item, dict) and item.get("id")]

    def agent_capabilities(self) -> list[dict[str, object]]:
        response = self._client.get(
            f"{self.config.bridge_url}/v1/agent/capabilities",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
        )
        self._raise_for_error(response, "Agent Mode capability discovery")
        payload = response.json()
        capabilities = payload.get("capabilities") if isinstance(payload, dict) else None
        if not isinstance(capabilities, list) or any(not isinstance(item, dict) for item in capabilities):
            raise HermesBridgeError("Hermes returned an invalid Agent Mode capability manifest")
        return capabilities

    def establish_agent_session(self, context: AgentBrokerContext) -> None:
        """Publish this device's authoritative live generation to the broker."""
        response = self._client.post(
            f"{self.config.bridge_url}/v1/agent/session",
            headers=self._headers(),
            json={"context": asdict(context)},
        )
        self._raise_for_error(response, "Agent Mode session update")
        payload = response.json()
        if (
            not isinstance(payload, dict)
            or payload.get("ok") is not True
            or payload.get("session_generation") != context.session_generation
        ):
            raise HermesBridgeError("Hermes returned an invalid Agent Mode session acknowledgement")

    def execute_agent_capability(
        self,
        capability_id: str,
        arguments: dict[str, object],
        context: AgentBrokerContext,
        *,
        request_id: str | None = None,
        approval_token: str = "",
    ) -> AgentBrokerResult:
        identifier = request_id or f"agent-{uuid.uuid4().hex}"
        response = self._client.post(
            f"{self.config.bridge_url}/v1/agent/execute",
            headers=self._headers(),
            json={
                "request_id": identifier,
                "capability_id": capability_id,
                "arguments": arguments,
                "context": asdict(context),
                **({"approval_token": approval_token} if approval_token else {}),
            },
        )
        self._raise_for_error(response, f"Agent Mode capability {capability_id}")
        payload = response.json()
        try:
            read_only = payload.get("read_only")
            side_effect = payload.get("side_effect")
            if (
                payload.get("ok") is not True
                or type(read_only) is not bool
                or type(side_effect) is not bool
                or read_only is side_effect
            ):
                raise ValueError("unverified result")
            if payload.get("request_id") != identifier or payload.get("capability_id") != capability_id:
                raise ValueError("mismatched result identity")
            evidence = payload["evidence"]
            freshness = payload["freshness"]
            if (
                not isinstance(evidence, list)
                or any(not isinstance(item, dict) for item in evidence)
                or not isinstance(freshness, dict)
                or set(freshness) != {"observed_at", "completed_at", "age_seconds"}
                or any(type(freshness[name]) not in {int, float} for name in freshness)
            ):
                raise ValueError("invalid metadata")
            return AgentBrokerResult(
                request_id=str(payload["request_id"]),
                capability_id=str(payload["capability_id"]),
                data=payload["data"],
                evidence=tuple(dict(item) for item in evidence),
                freshness=dict(freshness),
                read_only=read_only,
                side_effect=side_effect,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HermesBridgeError("Hermes returned an invalid Agent Mode result") from exc

    def approve_agent_action(
        self,
        capability_id: str,
        arguments: dict[str, object],
        context: AgentBrokerContext,
        *,
        request_id: str | None = None,
    ) -> AgentBrokerResult:
        """Approve and execute exactly one phone-reviewed broker action."""
        identifier = request_id or f"agent-{uuid.uuid4().hex}"
        response = self._client.post(
            f"{self.config.bridge_url}/v1/agent/approve",
            headers=self._headers(),
            json={
                "request_id": identifier,
                "capability_id": capability_id,
                "arguments": arguments,
                "context": asdict(context),
            },
        )
        self._raise_for_error(response, f"approved Agent Mode capability {capability_id}")
        payload = response.json()
        try:
            if (
                payload.get("ok") is not True
                or payload.get("request_id") != identifier
                or payload.get("capability_id") != capability_id
                or payload.get("read_only") is not False
                or payload.get("side_effect") is not True
            ):
                raise ValueError("unverified approved result")
            evidence = payload["evidence"]
            freshness = payload["freshness"]
            if not isinstance(evidence, list) or not isinstance(freshness, dict):
                raise ValueError("invalid approved metadata")
            return AgentBrokerResult(
                request_id=identifier,
                capability_id=capability_id,
                data=payload["data"],
                evidence=tuple(dict(item) for item in evidence if isinstance(item, dict)),
                freshness=dict(freshness),
                read_only=False,
                side_effect=True,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HermesBridgeError("Hermes returned an invalid approved Agent Mode result") from exc

    def pending_agent_approval(
        self, context: AgentBrokerContext
    ) -> dict[str, object] | None:
        response = self._client.post(
            f"{self.config.bridge_url}/v1/agent/pending-approval",
            headers=self._headers(),
            json={"context": asdict(context)},
        )
        self._raise_for_error(response, "Agent Mode pending approval")
        payload = response.json()
        pending = payload.get("pending_approval") if isinstance(payload, dict) else None
        if pending is not None and not isinstance(pending, dict):
            raise HermesBridgeError("Hermes returned an invalid pending Agent approval")
        return dict(pending) if isinstance(pending, dict) else None

    def approve_pending_agent_action(
        self,
        draft_id: str,
        context: AgentBrokerContext,
    ) -> dict[str, object]:
        response = self._client.post(
            f"{self.config.bridge_url}/v1/agent/approve-pending",
            headers=self._headers(),
            json={"context": asdict(context), "draft_id": draft_id},
        )
        self._raise_for_error(response, "pending Agent Mode approval")
        payload = response.json()
        if (
            not isinstance(payload, dict)
            or payload.get("ok") is not True
            or payload.get("draft_id") != draft_id
            or payload.get("side_effect") is not True
        ):
            raise HermesBridgeError("Hermes returned an invalid pending approval result")
        return dict(payload)

    def ask_agent(
        self,
        request: str,
        context: AgentBrokerContext,
        *,
        request_id: str | None = None,
    ) -> str:
        identifier = request_id or f"agent-{uuid.uuid4().hex}"
        response = self._client.post(
            f"{self.config.bridge_url}/v1/agent/ask",
            headers=self._headers(),
            json={"request_id": identifier, "request": request, "context": asdict(context)},
        )
        self._raise_for_error(response, "Agent Mode response")
        payload = response.json()
        text = str(payload.get("text") or "").strip() if isinstance(payload, dict) else ""
        if not text:
            raise HermesBridgeError("Hermes returned an empty Agent Mode response")
        return text

    def preview_agent_run(
        self,
        goal: str,
        context: AgentBrokerContext,
        *,
        request_id: str | None = None,
    ) -> dict[str, object]:
        identifier = request_id or f"agent-run-preview-{uuid.uuid4().hex[:16]}"
        response = self._client.post(
            f"{self.config.bridge_url}/v1/agent/run/preview",
            headers=self._headers(),
            json={"request_id": identifier, "goal": goal, "context": asdict(context)},
        )
        self._raise_for_error(response, "Agent run preview")
        return self._parse_agent_run(response.json())

    def current_agent_run(self, context: AgentBrokerContext) -> dict[str, object] | None:
        response = self._client.post(
            f"{self.config.bridge_url}/v1/agent/run/current",
            headers=self._headers(),
            json={"context": asdict(context)},
        )
        self._raise_for_error(response, "Current Agent run")
        payload = response.json()
        if isinstance(payload, dict) and payload.get("run") is None:
            return None
        return self._parse_agent_run(payload)

    def agent_run_action(
        self,
        action: str,
        run_id: str,
        context: AgentBrokerContext,
        *,
        step_id: str = "",
    ) -> dict[str, object]:
        if action not in {"status", "start", "approve", "pause", "resume", "cancel"}:
            raise ValueError("unsupported Agent run action")
        response = self._client.post(
            f"{self.config.bridge_url}/v1/agent/run/{action}",
            headers=self._headers(),
            json={
                "context": asdict(context),
                "run_id": run_id,
                **({"step_id": step_id} if action == "approve" else {}),
            },
        )
        self._raise_for_error(response, f"Agent run {action}")
        return self._parse_agent_run(response.json())

    @staticmethod
    def _parse_agent_run(payload: object) -> dict[str, object]:
        run = payload.get("run") if isinstance(payload, dict) else None
        if not isinstance(run, dict):
            raise HermesBridgeError("Hermes returned an invalid Agent run")
        required = {"run_id", "goal", "status", "generation", "budgets", "steps", "resumable"}
        if not required <= set(run) or not isinstance(run.get("steps"), list):
            raise HermesBridgeError("Hermes returned an incomplete Agent run")
        if not re.fullmatch(r"run-[0-9a-f]{24}", str(run.get("run_id") or "")):
            raise HermesBridgeError("Hermes returned an invalid Agent run identity")
        if any(not isinstance(step, dict) for step in run["steps"]):
            raise HermesBridgeError("Hermes returned invalid Agent run steps")
        return dict(run)

    def cancel_agent_request(self, request_id: str) -> bool:
        response = self._client.post(
            f"{self.config.bridge_url}/v1/agent/cancel/{request_id}",
            headers=self._headers(),
        )
        self._raise_for_error(response, "Agent Mode cancellation")
        payload = response.json()
        return bool(payload.get("cancelled")) if isinstance(payload, dict) else False

    def agent_activity(
        self, context: AgentBrokerContext, *, request_id: str | None = None
    ) -> list[dict[str, object]]:
        identifier = request_id or f"agent-{uuid.uuid4().hex}"
        response = self._client.post(
            f"{self.config.bridge_url}/v1/agent/activity",
            headers=self._headers(),
            json={"context": asdict(context), "request_id": identifier},
        )
        self._raise_for_error(response, "Agent Mode activity")
        payload = response.json()
        activity = payload.get("activity") if isinstance(payload, dict) else None
        if not isinstance(activity, list):
            raise HermesBridgeError("Hermes returned invalid Agent Mode activity")
        return [dict(item) for item in activity if isinstance(item, dict)]

    def chat(self, transcript: str) -> str:
        if self.config.kids_mode_enabled:
            return self._kids_chat(transcript)
        self._rotate_session_if_stale()
        response = self._client.post(
            f"{self.config.bridge_url}/v1/chat/completions",
            headers=self._headers(),
            json={
                "model": self.config.model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": self.config.system_prompt},
                    {"role": "user", "content": transcript},
                ],
            },
        )
        self._raise_for_error(response, "agent response")
        payload = response.json()
        try:
            text = str(payload["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise HermesBridgeError("Hermes returned an invalid chat-completion payload") from exc
        if not text:
            raise HermesBridgeError("Hermes returned an empty response")
        return text

    def _kids_chat(self, transcript: str) -> str:
        """Use the dedicated no-agent child route with ephemeral in-process context only."""
        self._kids_speech_approval = ""
        self._kids_fallback_speech_approval = ""
        self.last_kids_next_action = ""
        self.last_kids_ispy_role = ""
        self.last_kids_ispy_phase = ""
        clean = transcript.strip()
        if not clean:
            raise HermesBridgeError("Kids Mode received an empty transcript")
        response = self._client.post(
            f"{self.config.bridge_url}/v1/kids/chat",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            json={
                "input": clean,
                "session_id": self.config.kids_session_id,
                "profile": {
                    "age_band": self.config.kids_age_band,
                    "activity": self.config.kids_activity,
                    "language": self.config.language,
                },
            },
        )
        self._raise_for_error(response, "Kids Mode response")
        payload = response.json()
        text = str(payload.get("text") or "").strip() if isinstance(payload, dict) else ""
        if not text:
            raise HermesBridgeError("Kids Mode returned an empty response")
        approval = str(payload.get("speech_approval") or "").strip() if isinstance(payload, dict) else ""
        fallback_approval = (
            str(payload.get("fallback_speech_approval") or "").strip()
            if isinstance(payload, dict)
            else ""
        )
        if not approval or not fallback_approval:
            raise HermesBridgeError("Kids Mode returned incomplete moderated speech approvals")
        if self.config.kids_activity == "ispy":
            action = str(payload.get("ispy_next_action") or "")
            role = str(payload.get("ispy_role") or "")
            phase = str(payload.get("ispy_phase") or "")
            if action not in {"", "prepare_robot_round"}:
                raise HermesBridgeError("Kids Mode returned an invalid I Spy action")
            if role not in {"reachy_picker", "player_picker", "reachy_pending"}:
                raise HermesBridgeError("Kids Mode returned an invalid I Spy role")
            if phase not in {
                "reachy_guessing",
                "awaiting_clue",
                "awaiting_confirmation",
                "awaiting_reveal",
                "complete",
            }:
                raise HermesBridgeError("Kids Mode returned an invalid I Spy phase")
            self.last_kids_next_action = action
            self.last_kids_ispy_role = role
            self.last_kids_ispy_phase = phase
        self._kids_speech_approval = approval
        self._kids_fallback_speech_approval = fallback_approval
        return text

    def iter_kids_speech(
        self,
        text: str,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[bytes]:
        """Yield low-latency 24 kHz PCM from the bridge's fixed child TTS route."""
        clean = text.strip()
        if not self.config.kids_mode_enabled:
            raise HermesBridgeError("Kids streaming speech is only available in Kids Mode")
        if not clean:
            raise HermesBridgeError("Kids Mode received an empty speech response")
        approval = self._kids_speech_approval
        if not approval:
            raise HermesBridgeError("Kids Mode speech has no moderated-response approval")
        if should_stop is not None and should_stop():
            return
        with self._client.stream(
            "POST",
            f"{self.config.bridge_url}/v1/kids/speech/stream",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            json={
                "input": clean,
                "session_id": self.config.kids_session_id,
                "speech_approval": approval,
            },
            timeout=httpx.Timeout(30.0, connect=10.0, read=2.0),
        ) as response:
            self._kids_speech_approval = ""
            if not response.is_success:
                response.read()
                self._raise_for_error(response, "Kids Mode streaming speech synthesis")
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            rate = response.headers.get("x-reachy-audio-rate", "")
            if content_type != "audio/pcm" or rate != "24000":
                response.read()
                raise HermesBridgeError("Kids Mode TTS returned an unsupported audio stream")
            self.last_tts_provider = response.headers.get(
                "x-reachy-tts-provider",
                "elevenlabs-flash-stream",
            )
            received = False
            for chunk in response.iter_bytes(chunk_size=8 * 1024):
                if should_stop is not None and should_stop():
                    return
                if chunk:
                    received = True
                    yield chunk
            if not received:
                raise HermesBridgeError("Kids Mode TTS returned no streaming audio")

    def synthesize(self, text: str) -> SpeechAudio:
        if self.config.kids_mode_enabled:
            approval = self._kids_fallback_speech_approval
            if not approval:
                raise HermesBridgeError("Kids Mode fallback speech has no moderated-response approval")
            payload = {
                "input": text,
                "session_id": self.config.kids_session_id,
                "speech_approval": approval,
            }
            endpoint = "/v1/kids/speech/fallback"
        else:
            payload = {
                "provider": self.config.tts_provider,
                "model": self.config.tts_model,
                "input": text,
                "voice": self.config.tts_voice,
                "response_format": "mp3",
            }
            endpoint = "/v1/audio/speech"
        response = self._client.post(
            f"{self.config.bridge_url}{endpoint}",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            json=payload,
        )
        if self.config.kids_mode_enabled:
            self._kids_fallback_speech_approval = ""
        self._raise_for_error(response, "speech synthesis")
        content_type = response.headers.get("content-type", "audio/mpeg").split(";", 1)[0]
        extension = {
            "audio/mpeg": ".mp3",
            "audio/mp3": ".mp3",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "audio/ogg": ".ogg",
            "audio/flac": ".flac",
        }.get(content_type, ".audio")
        if not response.content:
            raise HermesBridgeError("Hermes TTS returned no audio")
        provider = response.headers.get("x-reachy-tts-provider", self.config.tts_provider)
        self.last_tts_provider = provider
        return SpeechAudio(response.content, content_type, extension, provider)

    @staticmethod
    def _raise_for_error(response: httpx.Response, operation: str) -> None:
        if response.is_success:
            return
        detail = response.text[:500]
        try:
            payload = response.json()
            detail = str(payload.get("detail") or payload.get("error") or detail)
        except Exception:
            pass
        _LOGGER.warning("Hermes %s failed with HTTP %s", operation, response.status_code)
        raise HermesBridgeError(f"Hermes {operation} failed (HTTP {response.status_code}): {detail}")
