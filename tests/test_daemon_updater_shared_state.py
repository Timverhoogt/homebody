"""Managed job state must be writable by BOTH existing app and independent worker."""
from pathlib import Path


def test_daemon_example_shares_journal_inside_existing_app_state_sandbox():
    root=Path(__file__).resolve().parents[1]
    unit=(root/'docs/systemd/reachy-daemon-updater.service').read_text()
    writable=next(line.split('=',1)[1].split() for line in unit.splitlines() if line.startswith('ReadWritePaths='))
    assert '/opt/reachy-mini-lite/homebody-managed' in writable
    assert '/opt/reachy-mini-lite/state/home/.local/share/homebody/updates' in writable
    assert 'ProtectSystem=strict' in unit and 'ProtectHome=true' in unit
    assert 'ExecStart=/opt/reachy-mini-lite/homebody-managed/updater/bin/python -m homebody.update_worker' in unit
    guidance=(root/'docs/managed-updates.md').read_text()
    assert 'existing app sandbox' in guidance
    assert 'without changing the daemon' in guidance
