from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "reachy_mini_hermes" / "static"


def test_robot_tab_exposes_motor_state_and_safe_power_controls() -> None:
    html = (STATIC / "index.html").read_text()

    assert 'id="motor-state"' in html
    assert 'id="motor-state-dot"' in html
    assert 'id="motor-wake-button"' in html
    assert 'id="motor-standby-button"' in html
    assert 'id="robot-stop-button"' in html
    assert 'aria-describedby="robot-stop-help"' in html
    assert 'id="motor-state-live"' in html
    assert 'aria-live="polite"' in html
    assert html.index('id="robot-stop-button"') < html.index('class="robot-control-grid"')
    assert "Wake &amp; enable" in html
    assert "Fold &amp; disable" in html
    assert "Stop action" in html


def test_robot_tab_offers_bounded_diagonal_look_and_wide_motion_confirmation() -> None:
    html = (STATIC / "index.html").read_text()

    for direction in (
        "up_left",
        "up",
        "up_right",
        "left",
        "center",
        "right",
        "down_left",
        "down",
        "down_right",
    ):
        assert f'data-robot-value="{direction}"' in html
    assert html.count('data-robot-action="look"') == 9
    assert 'data-confirm="Energetic uses wide body, head, and antenna movement.' in html
    assert "Compact" in html
    assert "Medium" in html
    assert "Wide" in html


def test_robot_tab_offers_precision_pose_and_base_controls() -> None:
    html = (STATIC / "index.html").read_text()
    main = (STATIC / "main.js").read_text()

    assert 'id="precision-step"' in html
    for axis in ("x", "y", "z", "roll", "pitch", "yaw", "body_yaw"):
        assert f'data-nudge-axis="{axis}"' in html
        assert f'id="pose-{axis.replace("_", "-")}"' in html
    for center in ("center_head", "center_base", "center_all"):
        assert f'data-nudge-axis="{center}"' in html
    assert "Fine · 1 mm / 1°" in html
    assert "Small · 2.5 mm / 2.5°" in html
    assert 'fetch("/api/robot/nudge"' in main
    assert 'fetch("/api/robot/pose"' in main
    assert "if (!robotBusy) refreshRobotPose()" in main
    assert 'aria-label="Move head up on Z axis"' in html
    assert 'aria-label="Rotate base left"' in html
    assert 'role="group" aria-label="Current measured head and base pose"' in html
    assert "nudge_reachy: \"Precision pose\"" in main


def test_motor_ui_serializes_power_transitions_and_reports_confirmed_state() -> None:
    main = (STATIC / "main.js").read_text()
    style = (STATIC / "style.css").read_text()
    worker = (STATIC / "service-worker.js").read_text()
    html = (STATIC / "index.html").read_text()

    assert "let powerTransitionPending = false" in main
    assert "let statusRefreshPending = false" in main
    assert "if (statusRefreshPending) return" in main
    assert "statusRefreshPending = false" in main
    assert 'document.querySelectorAll(".manual-control, [data-power]")' in main
    assert "Awake was not confirmed by the robot runtime" in main
    assert "Safe folded Standby was not confirmed by the robot runtime" in main
    assert 'move_reachy_head: "Look direction"' in main
    assert 'runtime.motors_enabled' in main
    assert 'runtime.head_safely_folded' in main
    assert "Remote controls disabled until live status returns" in main
    assert 'panel.closest("#panel-robot")' in main
    assert 'button.dataset.confirm && !window.confirm(button.dataset.confirm)' in main
    assert 'dPad.addEventListener("keydown"' in main
    assert 'button.dataset.power === powerMode' in main
    assert ".motor-command-bar" in style
    assert ".motor-safety-actions" in style
    assert 'data-action-busy="true"' in style
    assert "position: fixed" in style
    assert 'data-action-busy="false"' in html
    assert 'reachy-hermes-shell-v22' in worker
    assert '/static/style.css?v=22' in html
    assert '/static/camera.js?v=22' in html
    assert '/static/main.js?v=22' in html
