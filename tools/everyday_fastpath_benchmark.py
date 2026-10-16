#!/usr/bin/env python3
"""Silent loopback integrated-route benchmark; --live opts into two short cloud Live sessions.

Uses archived synthetic PCM and the existing benchmark protocol, never capture/playback.
No production endpoints/config writes; only the selected profile's saved OpenAI key is resolved.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from companion.hermes_reachy_bridge import Bridge, _resolve_secret


def context(generation=1):
    return dict(capability_profile='conversation', adult_ui_unlocked=True, kids_mode_active=False,
                power_mode='awake', privacy_enabled=True, emergency_stop_active=False, robot_available=True,
                session_generation=generation, requested_session_generation=generation, explicit_private_intent=False)


async def run(args):
    os.umask(0o077)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    if args.live:
        key = _resolve_secret('OPENAI_API_KEY', 'reachy')
        assert key, 'Saved OpenAI key unavailable'
        os.environ['OPENAI_API_KEY'] = key
    os.environ['HERMES_HOME'] = str(output/'isolated-hermes-home')
    bridge = Bridge(api_key='isolated-fastpath-benchmark', hermes_url='http://127.0.0.1:1', profile='reachy')
    bridge.everyday.config.timezone_name = 'Europe/Amsterdam'
    # No reasoner/network fallback is permitted in this experiment.
    bridge.everyday_llm = None
    app = web.Application()
    app.router.add_post('/v1/agent/session', bridge.broker_session)
    app.router.add_post('/v1/everyday/turn', bridge.everyday_turn)
    app.router.add_get('/v1/live', bridge.live)
    headers = {'Authorization': 'Bearer isolated-fastpath-benchmark', 'X-Reachy-Device-Id': 'isolated-clock-fixture'}
    records = []
    original = bridge.everyday_answer
    async def measured(*a, **kw):
        start = time.monotonic()
        try:
            result = await original(*a, **kw)
        except Exception:
            # Synthetic-only diagnostics, categories rather than transcripts.
            text = a[0].casefold()
            records.append({'route': 'not_matched', 'context_present': bool(kw.get('context')),
                            'time_prefix': text.startswith('what is the current time'),
                            'brief_suffix': text.rstrip('.!? ').endswith('answer briefly'),
                            'wall_ms': (time.monotonic()-start)*1000})
            raise
        records.append({'route': result['source'].get('route'), 'wall_ms': (time.monotonic()-start)*1000})
        return result
    bridge.everyday_answer = measured
    report = {'hardware': 'none; no SDK, capture or playback', 'timezone': 'Europe/Amsterdam', 'clock_http': []}
    async with aiohttp.ClientSession() as http:
        bridge.http = http
        async with TestClient(TestServer(app)) as client:
            native_listener = next(iter(client.server.runner.sites))._server
            response = await client.post('/v1/agent/session', headers=headers, json={'context': context()})
            assert response.status == 200
            fixed = datetime(2026, 10, 12, 9, 30, tzinfo=UTC).timestamp()
            for clock_kind in ('fixture', 'host'):
                bridge.everyday.clock = (lambda: fixed) if clock_kind == 'fixture' else time.time
                for language, text in (('en', 'What time is it?'), ('nl', 'Hoe laat is het nu?')):
                    start = time.monotonic()
                    response = await client.post('/v1/everyday/turn', headers=headers,
                                                 json={'text': text, 'language': language, 'session_id': clock_kind})
                    result = await response.json()
                    assert response.status == 200 and result['source']['route'] == 'time'
                    if clock_kind == 'fixture':
                        assert '11:30' in result['text']
                    report['clock_http'].append({'clock': clock_kind, 'language': language,
                                                 'wall_ms': (time.monotonic()-start)*1000,
                                                 'backend_ms': result['timing']['backend_ms']})
            if args.live:
                archive = args.archive.resolve()
                spec = importlib.util.spec_from_file_location('archived_live_benchmark', archive/'benchmark.py')
                bench = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(bench)
                bench.ROOT = output
                bench.BASE = str(client.make_url('')).rstrip('/')
                key = _resolve_secret('OPENAI_API_KEY', 'reachy')
                assert key, 'Saved OpenAI key unavailable'
                meta = json.loads((archive/'fixtures/input_manifest.json').read_text())
                (output/'fixtures').mkdir()
                rows = []
                deadline = time.monotonic() + 220
                for item in (m for m in meta if m['id'] in {'en_time', 'nl_time'}):
                    shutil.copyfile(archive/'fixtures'/f"{item['id']}.pcm", output/'fixtures'/f"{item['id']}.pcm")
                    rows.extend(await bench.one(
                        http, key, 'isolated-fastpath-benchmark', 'relay', 0, [item], deadline))
                report['live'] = rows
                baseline = json.loads((archive/'audited-summary.json').read_text())
                report['archived_baseline'] = [g for g in baseline['groups']
                                               if g['route'] == 'relay' and g['fixture'] in {'en_time', 'nl_time'}]
                sessions = [json.loads(line) for line in (output/'sessions.jsonl').read_text().splitlines()]
                report['sessions'] = sessions
                assert len(sessions) == 2 and all(s['closed_confirmed'] and s['lease_invalidated'] for s in sessions)
                report['all_useful'] = all(not row['deadline_hit'] for row in rows)
            report['backend_receipts'] = records
            report['listener_closed'] = False
        report['listener_closed'] = native_listener is not None and not native_listener.is_serving()
        assert report['listener_closed']
    (output/'performance.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'performance_json': str(output/'performance.json'), 'route_calls': len(records),
                      'live_sessions': len(report.get('sessions', [])), 'listener_closed': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if args.live and not args.archive:
        parser.error('--live requires --archive with existing synthetic fixtures')
    asyncio.run(run(args))
