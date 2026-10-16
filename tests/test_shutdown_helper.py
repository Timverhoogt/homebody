"""Rootless socket/process/loopback tests; never invoke the kernel's real poweroff."""
import json
import math
import os
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from homebody import host_shutdown as client
from homebody import shutdown_helper as helper

POSE = {"x": -.021, "y": .001, "z": -.044, "roll": 0,
        "pitch": math.atan2(.413, .911), "yaw": 0}
STATE = {"control_mode": "disabled", "head_pose": POSE}


@pytest.fixture
def kernel(monkeypatch, tmp_path):
    """An actual child executable stands in for systemctl; it only writes a fixture log."""
    log = tmp_path / "calls.jsonl"
    fake = tmp_path / "fake-systemctl"
    fake.write_text(f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
                    f"p=Path({str(log)!r})\n"
                    "with p.open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
                    "print('loaded\\nloaded' if sys.argv[1]=='show' else "
                    "'active' if sys.argv[1]=='is-active' else '')\n")
    fake.chmod(0o755)
    monkeypatch.setattr(helper, "SYSTEMCTL", str(fake))
    monkeypatch.setattr(helper, "dedicated_host", lambda: True)
    monkeypatch.setattr(helper, "physical_safe", lambda: True)
    return log


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_real_child_command_receipt_and_independent_execution(kernel, monkeypatch):
    assert helper.dispatch("CHECK") == {"protocol": 1, "state": "ready"}
    assert not any(c[0] == "start" for c in calls(kernel))
    receipt = helper.dispatch("SCHEDULE")
    assert receipt == {"protocol": 1, "state": "scheduled", "delay_seconds": 5}
    assert ["start", "homebody-poweroff.timer"] in calls(kernel)
    assert not any("poweroff" in c for c in calls(kernel))
    # Exercise the timer's independent entry point with the fake child, not the OS.
    monkeypatch.setattr(sys, "argv", ["helper", "--execute"])
    helper.main()
    assert calls(kernel)[-1] == ["--no-block", "poweroff", "--no-wall"]


def test_changed_pose_before_timer_prevents_kernel_command(kernel, monkeypatch):
    assert helper.dispatch("SCHEDULE")["state"] == "scheduled"
    monkeypatch.setattr(helper, "physical_safe", lambda: False)
    monkeypatch.setattr(sys, "argv", ["helper", "--execute"])
    with pytest.raises(SystemExit, match="refused"):
        helper.main()
    assert not any("poweroff" in c for c in calls(kernel))


@pytest.mark.parametrize("failure", ["host", "units", "pose", "daemon"])
def test_preflight_and_physical_refusals_never_schedule(kernel, monkeypatch, failure):
    if failure == "host":
        monkeypatch.setattr(helper, "dedicated_host", lambda: False)
    if failure == "units":
        monkeypatch.setattr(helper, "command", lambda *a: "not-found")
    if failure == "pose":
        monkeypatch.setattr(helper, "physical_safe", lambda: False)
    if failure == "daemon":
        def unavailable():
            raise OSError("daemon unavailable")
        monkeypatch.setattr(helper, "physical_safe", unavailable)
    assert helper.dispatch("SCHEDULE")["state"] == "refused"
    assert not kernel.exists() or not any(c[0] == "start" for c in calls(kernel))


def test_start_ack_failure_is_unknown_not_refused(kernel, monkeypatch):
    original = helper.command
    def run(*args):
        if args[0] == "is-active":
            raise subprocess.TimeoutExpired("fake-systemctl", 2)
        return original(*args)
    monkeypatch.setattr(helper, "command", run)
    assert helper.dispatch("SCHEDULE")["state"] == "unknown"
    assert ["start", helper.TIMER] in calls(kernel)


@pytest.mark.parametrize("change", [
    {"control_mode": "enabled"}, {"head_pose": {}}, {"head_pose": {**POSE, "x": True}},
    {"head_pose": {**POSE, "pitch": float('nan')}}, {"head_pose": {**POSE, "z": 0}},
    {"head_pose": {**POSE, "yaw": 1}}, {"head_pose": {**POSE, "x": -.01}},
])
def test_bad_pose_is_fail_closed(change):
    assert not helper.safe_state({**STATE, **change})


def test_safe_pose_matches_exact_sdk_trace_distance_neighbourhood():
    import numpy as np
    from scipy.spatial.transform import Rotation
    target = np.array([[.911,.004,.413],[-.004,1,-.001],[-.413,-.001,.911]])
    rng = np.random.default_rng(417)
    accepted = 0
    for _ in range(4000):
        p = {k: v + float(rng.uniform(-.004,.004) if k in ('x','y','z') else rng.uniform(-.07,.07))
             for k,v in POSE.items()}
        r = Rotation.from_euler('xyz', [p[k] for k in ('roll','pitch','yaw')]).as_matrix()
        # SDK 1.9 uses the CLIPPED TRACE of the rounded sleep rotation, not
        # Rotation.from_matrix(...).magnitude() (that projects/changes the matrix).
        distance = np.linalg.norm(np.array([p[k]-POSE[k] for k in ('x','y','z')]))*1000
        distance += np.degrees(np.arccos(np.clip((np.trace(r @ target.T)-1)/2,-1,1)))
        expected = p['z'] <= -.03 and p['pitch'] >= .25 and distance <= 10
        actual = helper.safe_state({"control_mode": "disabled", "head_pose": p})
        assert actual == expected
        accepted += actual
    assert accepted > 100
    assert helper.safe_state(STATE)


def test_read_only_bounded_daemon_http_over_real_loopback(monkeypatch):
    import http.client
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(STATE).encode())
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = http.client.HTTPConnection
    def fixture_connection(host, port, timeout):
        assert (host, port, timeout) == ('127.0.0.1', 8000, 2)
        return original(host, server.server_port, timeout=timeout)
    monkeypatch.setattr(helper.http.client, 'HTTPConnection', fixture_connection)
    try:
        assert helper.physical_safe()
        assert seen == ['/api/state/full']
    finally:
        server.shutdown()
        thread.join(2)
        server.server_close()


@pytest.mark.parametrize("message,authorized", [(b'CHECK\n',True),(b'SCHEDULE\n',True),
    (b'SCHEDULE /bin/sh\n',True),(b'SCHEDULE\nCHECK\n',True),(b'CHECK\n',False)])
def test_real_unix_stream_peer_uid_and_fixed_protocol(kernel, message, authorized):
    a, b = socket.socketpair()
    uid = os.getuid() if authorized else os.getuid() + 1
    thread = threading.Thread(target=helper.serve, args=(b, uid), daemon=True)
    thread.start()
    a.settimeout(3)
    try:
        a.sendall(message)
        a.shutdown(socket.SHUT_WR)
        if authorized and message in (b'CHECK\n', b'SCHEDULE\n'):
            body = json.loads(a.recv(1024))
            assert body['state'] == ('ready' if message == b'CHECK\n' else 'scheduled')
        thread.join(3)
        assert not thread.is_alive()
        if not authorized or message not in (b'CHECK\n', b'SCHEDULE\n'):
            assert not kernel.exists()
    finally:
        a.close()
        b.close()


def test_client_rejects_missing_and_untrusted_socket(monkeypatch, tmp_path):
    monkeypatch.setattr(client, 'SOCKET', str(tmp_path/'missing'))
    with pytest.raises(client.ShutdownRefused):
        client.request('SCHEDULE')
    path = tmp_path/'sock'
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(path))
        monkeypatch.setattr(client, 'SOCKET', str(path))
        with pytest.raises(client.ShutdownRefused):
            client.request('CHECK')


@pytest.mark.parametrize('reply,expected', [
    (b'{"protocol":1,"state":"scheduled","delay_seconds":5}\n', 'scheduled'),
    (b'{"protocol":1,"state":"refused"}\n', client.ShutdownRefused),
    (b'{"protocol":1,"state":"unknown"}\n', client.ShutdownUnknown),
    (b'{"ok":true}\n', client.ShutdownUnknown),
    (b'{"protocol":true,"state":"scheduled","delay_seconds":5}\n', client.ShutdownUnknown),
    (b'', client.ShutdownUnknown), (b'x'*1025, client.ShutdownUnknown),
])
def test_client_real_socket_receipts_and_lost_reply(monkeypatch, tmp_path, reply, expected):
    path = tmp_path/'sock'
    original_socket = socket.socket
    listener = original_socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(1)
    seen = []
    def server():
        with listener.accept()[0] as connection:
            seen.append(connection.recv(64))
            connection.sendall(reply)
    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    # Root identity cannot be acquired in this rootless test. Mock only the two
    # trust attestations; all stream framing/admission code runs over real IPC.
    class RootPeerSocket(original_socket):
        def getsockopt(self, level, option, size=0):
            if option == socket.SO_PEERCRED:
                return struct.pack('3i', 1, 0, 0)
            return super().getsockopt(level, option, size)
    monkeypatch.setattr(client, 'SOCKET', str(path))
    monkeypatch.setattr(client.os, 'lstat', lambda p: SimpleNamespace(st_mode=0o140660,st_uid=0))
    monkeypatch.setattr(client.socket, 'socket', RootPeerSocket)
    try:
        if isinstance(expected, str):
            assert client.request('SCHEDULE')['state'] == expected
        else:
            with pytest.raises(expected):
                client.request('SCHEDULE')
        assert seen == [b'SCHEDULE\n']
    finally:
        thread.join(3)
        listener.close()


def test_isolated_child_with_effective_no_new_privileges_serves_ipc(kernel):
    # No privileged installation: exercise the real standalone entry point in a
    # child with the kernel's NNP bit set and a fake systemctl executable.
    a, b = socket.socketpair()
    code = f"""
import ctypes, importlib.util
libc=ctypes.CDLL(None, use_errno=True)
assert libc.prctl(38, 1, 0, 0, 0) == 0
assert libc.prctl(39, 0, 0, 0, 0) == 1
spec=importlib.util.spec_from_file_location('standalone', {str(Path(helper.__file__).resolve())!r})
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.SYSTEMCTL={helper.SYSTEMCTL!r}
m.dedicated_host=lambda:True
m.main()
"""
    process = subprocess.Popen([sys.executable, '-I', '-c', code, '--uid', str(os.getuid())],
                               stdin=b.fileno(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    b.close()
    try:
        a.settimeout(5)
        a.sendall(b'CHECK\n')
        assert json.loads(a.recv(1024)) == {'protocol':1,'state':'ready'}
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, (stdout, stderr)
        assert all(c[0] == 'show' for c in calls(kernel))
    finally:
        a.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_offline_installer_and_hardening_contract(tmp_path):
    import pwd
    stage = tmp_path/'stage'
    stage.mkdir(mode=0o755)
    result = subprocess.run([sys.executable, '-I', 'tools/install_shutdown_helper.py',
                             '--app-user', pwd.getpwuid(os.getuid()).pw_name,
                             '--stage', str(stage)], capture_output=True, text=True, check=True)
    assert 'Rendered only' in result.stdout
    root = Path('homebody/provision')
    for name in ('homebody-shutdown@.service','homebody-poweroff.service'):
        text = (stage/'etc/systemd/system'/name).read_text()
        assert 'NoNewPrivileges=yes' in text and 'CapabilityBoundingSet=\n' in text
        assert '/usr/bin/python3 -I /opt/homebody-shutdown/shutdown_helper.py' in text
        assert 'PrivateNetwork=yes' not in text  # loopback daemon remains reachable
        assert 'IPAddressDeny=any' in text and 'IPAddressAllow=localhost' in text
    assert (stage/'opt/homebody-shutdown/shutdown_helper.py').read_bytes() == Path(helper.__file__).read_bytes()
    assert 'OnActiveSec=5s' in (root/'homebody-poweroff.timer').read_text()
    assert 'WantedBy=' not in (root/'homebody-poweroff.timer').read_text()
    assert all(p.stat().st_mode & 0o777 == 0o644 for p in stage.rglob('*') if p.is_file())
