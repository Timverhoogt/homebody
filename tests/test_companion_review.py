"""Independent regressions for the approved companion's consent and Stop contract."""

from __future__ import annotations

import threading

import pytest
from test_companion import jpeg, make_runtime

from homebody.companion import looks_like_look_request
from homebody.hermes_client import HermesBridgeError


@pytest.mark.parametrize(
    "text",
    [
        "What is this project about?",
        "Read the roadmap for Homebody",
        "Do not look at this letter",
        "Please do not take a look",
        'My colleague said "look at this" yesterday',
        "Explain the phrase look at this",
        "Lees dit projectplan uit de repository",
        "Kun je uitleggen wat kijk naar dit betekent?",
    ],
)
def test_ordinary_text_negation_and_quoted_speech_do_not_grant_camera_consent(text):
    assert not looks_like_look_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "Look at this letter",
        "What does this say?",
        "Kijk nog eens",
        "Lees dit briefje",
        "Hey Hermes, look at this letter",
        "What's on this label?",
    ],
)
def test_deliberate_camera_request_still_works(text):
    assert looks_like_look_request(text)


def test_stop_during_look_preflight_cannot_be_rebased_into_new_permission(monkeypatch):
    runtime, client, captures = make_runtime()
    entered = threading.Event()
    release = threading.Event()
    original = runtime._look_block_reason

    def held(config):
        result = original(config)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(runtime, "_look_block_reason", held)
    result = {}

    def run():
        try:
            result["answer"] = runtime.look_at_this("read this letter")
        except RuntimeError as exc:
            result["error"] = str(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(5)
    runtime.stop_speaking()
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert captures == [] and client.looks == []
    assert "answer" not in result


def test_camera_permission_revoked_during_capture_prevents_upload(monkeypatch):
    runtime, client, captures = make_runtime()
    config = runtime.config_loader()

    def capture(**_kwargs):
        captures.append(1)
        config.camera_enabled = False
        return jpeg()

    monkeypatch.setattr(runtime, "_capture_camera_jpeg", capture)
    with pytest.raises(RuntimeError):
        runtime.look_at_this("read this letter")
    assert captures == [1]
    assert client.looks == []
    assert runtime._look_context is None


def test_stop_clears_owner_only_private_details():
    runtime, _client, _captures = make_runtime()
    runtime._remember_private_detail("calendar", "Private appointment at eleven")
    assert runtime.companion_owner_status()["private_details"]
    runtime.companion_stop()
    assert runtime.companion_owner_status()["private_details"] == []


def test_raw_provider_errors_never_enter_public_companion_status():
    runtime, client, _captures = make_runtime()
    private = "PRIVATE_PROVIDER_BODY_WITH_PERSONAL_TEXT"
    client.fail_with = HermesBridgeError(private)
    with pytest.raises(RuntimeError):
        runtime.look_at_this("read this letter")
    assert private not in repr(runtime.companion_public_status(runtime.config_loader()))
