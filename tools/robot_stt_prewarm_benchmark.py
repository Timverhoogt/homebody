"""Synthetic-only candidate ASR benchmark; never constructs an SDK/media client.

Run with the target app interpreter, an isolated socket root and already-provisioned assets.
--overlay can select candidate Python adapters without installing/replacing the running app.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import wave
from pathlib import Path

import numpy as np


def snapshot():
    thermals = {}
    for p in Path('/sys/class/thermal').glob('thermal_zone*/temp'):
        try:
            thermals[(p.parent / 'type').read_text().strip()] = int(p.read_text())
        except OSError:
            pass
    return {'meminfo': Path('/proc/meminfo').read_text(), 'thermal_millidegrees': thermals}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--overlay', type=Path)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--socket-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    import homebody
    if args.overlay:
        homebody.__path__.insert(0, str(args.overlay.resolve()))
    from homebody import asr_worker, robot_stt
    from homebody.robot_stt import RobotSttClient
    meta = json.loads(args.corpus.read_text())
    assert meta['synthetic'] is True
    manifest_hash = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    args.socket_root.mkdir(mode=0o700, exist_ok=True)
    report = {'synthetic': True, 'baseline': snapshot(), 'manifest_sha256': manifest_hash,
              'adapter_sha256': {m.__name__: hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()
                                 for m in (robot_stt, asr_worker)}, 'cycles': []}
    for cycle in range(3):
        sd = args.socket_root / str(cycle)
        sd.mkdir(mode=0o700)
        client = RobotSttClient(socket_path=sd / 'worker.sock', manifest_path=args.manifest)
        proc = None
        row = {'cycle': cycle, 'decodes': []}
        report['cycles'].append(row)
        try:
            start = time.perf_counter()
            row['ready'] = client.status()
            row['model_ready_wall_ms'] = (time.perf_counter() - start) * 1000
            proc = client._process
            row['pid'] = client.worker_pid
            start = time.perf_counter()
            row['warmup'] = client.prewarm(cancelled=lambda: False)
            row['warmup_wall_ms'] = (time.perf_counter() - start) * 1000
            row['cold_to_inference_warm_ms'] = row['model_ready_wall_ms'] + row['warmup_wall_ms']
            # Three rounds, alternating language/activity. These are synthetic speech decodes,
            # not "real microphone" turns; prewarm itself always uses RAM-only silence.
            clips = [next(c for c in meta['clips'] if c['language'] == lang) for lang in ('en', 'nl')]
            for turn, clip in enumerate(clips * 3):
                with wave.open(str(args.corpus.parent / clip['wav'])) as w:
                    assert w.getnchannels() == 1 and w.getsampwidth() == 2
                    rate = w.getframerate()
                    pcm = np.frombuffer(w.readframes(w.getnframes()), '<i2').astype('float32') / 32768
                if rate != 16000:
                    pcm = np.interp(np.arange(int(len(pcm) * 16000 / rate)) * rate / 16000,
                                    np.arange(len(pcm)), pcm).astype('float32')
                start = time.perf_counter()
                r = client.transcribe(pcm, session=f'synthetic-activity-{turn}', generation=turn,
                                      language=clip['language'], cancelled=lambda: False)
                row['decodes'].append({'language': clip['language'], 'decode_ms': r.decode_ms,
                                       'wall_ms': (time.perf_counter() - start) * 1000,
                                       'pid': client.worker_pid, 'text': r.text,
                                       'expected_synthetic': clip['expected']})
            row['final_status'] = client.status()
            row['resident'] = snapshot()
            row['one_worker_reused'] = all(d['pid'] == row['pid'] for d in row['decodes'])
        finally:
            client.close()
            if proc is not None:
                proc.wait(timeout=10)
            row['cleanup_returncode'] = proc.poll() if proc else None
            row['pid_absent_after_reap'] = not Path(f'/proc/{proc.pid}').exists() if proc else True
            for p in sd.iterdir():
                p.unlink()
            sd.rmdir()
            args.output.write_text(json.dumps(report, indent=2))
    report['after'] = snapshot()
    report['manifest_unchanged'] = manifest_hash == hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    args.output.write_text(json.dumps(report, indent=2))
    assert report['manifest_unchanged'] and all(
        c['one_worker_reused'] and c['pid_absent_after_reap'] for c in report['cycles'])
    print(json.dumps({'cycles': len(report['cycles']), 'manifest_unchanged': report['manifest_unchanged'],
                      'all_workers_reaped': all(c['pid_absent_after_reap'] for c in report['cycles'])}))


if __name__ == '__main__':
    main()
