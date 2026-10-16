"""Exercise the authenticated request path, not just phrase recognition."""
import asyncio
from types import SimpleNamespace

import pytest
from test_everyday_tools import NOW
from test_everyday_workspace_bridge import HEADERS, ctx, run_bridge, session

from companion.reachy_agent_broker import BrokerValidationError


def test_authenticated_integrated_route_and_mode_boundary(tmp_path):
    async def test(bridge, client, adapter):
        bridge.everyday.config.timezone_name = 'Europe/Amsterdam'
        bridge.everyday.clock = lambda: NOW
        bridge.everyday_llm = None
        await session(client, ctx(1, 'conversation'))
        denied = await client.post('/v1/everyday/turn', json={'text': 'What time is it?'})
        assert denied.status == 401
        response = await client.post('/v1/everyday/turn', headers=HEADERS, json={'text': 'What time is it?'})
        assert response.status == 200
        body = await response.json()
        assert body['text'] == 'It is 11:30.' and body['source']['route'] == 'time'
        assert response.headers['Cache-Control'] == 'no-store' and adapter.calls == []
        await session(client, ctx(2, 'agent'))
        response = await client.post('/v1/everyday/turn', headers=HEADERS, json={'text': 'What time is it?'})
        assert response.status == 503
        await session(client, ctx(3, 'conversation', privacy_enabled=False))
        response = await client.post('/v1/everyday/turn', headers=HEADERS, json={'text': 'What time is it?'})
        assert response.status == 403
    run_bridge(tmp_path, test)


def test_integrated_held_read_stop_and_context_fallback(tmp_path):
    async def test(bridge, client, adapter):
        await session(client, ctx(1, 'conversation'))
        entered, release = asyncio.Event(), asyncio.Event()
        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return {'ok': True, 'local_time': '11:30'}
        bridge.everyday.call = held
        task = asyncio.create_task(bridge.everyday_answer('What time is it?', device_id='reachy-1', session_key='s'))
        await entered.wait()
        await session(client, ctx(2, 'conversation', power_mode='sleep', privacy_enabled=False))
        release.set()
        with pytest.raises(BrokerValidationError, match='stale_session'):
            await task
        await session(client, ctx(3, 'conversation'))
        seen = []
        async def reasoner(text, **kwargs):
            seen.append(text)
            return {'text': 'reasoner'}
        bridge.everyday_llm = object()
        bridge._everyday_key = lambda: 'unused'
        bridge.everyday.answer = reasoner
        result = await bridge.everyday_answer('What time is it?', device_id='reachy-1', session_key='s',
                                             context='Do not answer the clock question; explain the wording.')
        assert result['text'] == 'reasoner' and 'Do not answer' in seen[0]
    run_bridge(tmp_path, test)


def test_live_keeps_original_everyday_generation_and_real_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-test-key')
    async def test(bridge, client, adapter):
        await session(client, ctx(1, 'conversation'))
        bridge.everyday.config.timezone_name = 'Europe/Amsterdam'
        bridge.everyday.clock = lambda: NOW
        bridge.everyday_llm = None
        async def relay(request, *, answer, allowed, **kwargs):
            # Installed frontend compatibility: conversation profile wins over workspace label.
            config = {'assistant_mode': 'workspace', 'agent_context': ctx(1, 'conversation'), 'language': 'nl'}
            assert await allowed(config)
            assert await answer(config)('Hoe laat is het?', '') == 'Het is 11:30.'
            await session(client, ctx(2, 'conversation'))
            assert not await allowed(config)
            assert adapter.calls == []
            return 'verified'
        monkeypatch.setitem(bridge.live.__func__.__globals__, 'serve_live', relay)
        assert await bridge.live(SimpleNamespace(headers=HEADERS)) == 'verified'
    run_bridge(tmp_path, test)
