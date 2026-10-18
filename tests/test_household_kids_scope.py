# ruff: noqa: F811 -- imported fixture is intentionally a test parameter
"""Hosted Kids I Spy under the household preapproval: the real runtime host lobby and caregiver route.

The adult natural-look approval never covers Kids; only the separate I Spy choice removes the repeated
grown-up camera prompt, and revocation withdraws a grant that came from it.
"""

from __future__ import annotations

from test_kids_game_master import host, host_state, start, wait_for  # noqa: F401

from homebody.config import AppConfig

ADULT = {"camera_enabled": True, "camera_natural_requests_enabled": True, "household_setup_version": 1}


def choose_ispy(host, **settings):
    runtime = host.runtime
    config = AppConfig(**settings)
    runtime.config_loader = lambda: config
    start(host)
    session = runtime._kids_session_id
    runtime._motor_transition_lock.acquire()  # hold the round transition; inspect admission only
    try:
        runtime._kids_host_choose("ispy", runtime._kids_generation)  # the child's lobby choice
        consent = runtime._kids_profile.camera_consent
        notice = host_state(runtime).get("notice", "")
        if consent:
            runtime.kids_revoke_preapproved_camera()
            revoked = runtime._kids_profile.camera_consent
        else:
            revoked = None
        runtime._kids_host_handle_transcript(session, "stop")
    finally:
        runtime._motor_transition_lock.release()
    wait_for(lambda: runtime.status()["kids_mode"]["active"] is False)
    return consent, notice, revoked


def test_child_choice_of_ispy_uses_the_household_preapproval_without_a_repeat_prompt(host):
    consent, notice, revoked = choose_ispy(host, **ADULT, kids_ispy_camera_preapproved=True)
    assert consent is True and notice != "camera_consent_needed"
    assert revoked is False  # household revocation withdraws the preapproved grant


def test_adult_natural_approval_alone_never_opens_the_camera_for_kids(host):
    consent, notice, _ = choose_ispy(host, **ADULT)
    assert consent is False and notice == "camera_consent_needed"


def test_camera_off_beats_the_kids_preapproval(host):
    consent, notice, _ = choose_ispy(host, **{**ADULT, "camera_enabled": False}, kids_ispy_camera_preapproved=True)
    assert consent is False and notice == "camera_consent_needed"
