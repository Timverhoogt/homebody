"""Authenticated original-generation fences; no hardware or provider access."""
import asyncio
from types import SimpleNamespace

import pytest
from test_everyday_tools import NOW
from test_everyday_workspace_bridge import HEADERS, ctx, run_bridge, session

from companion.reachy_agent_broker import BrokerContext, BrokerValidationError


def install_reasoner(bridge):
    seen = []
    async def reasoner(text, **kwargs):
        seen.append(text)
        return {'text': 'reasoner'}
    bridge.everyday_llm = object()
    bridge._everyday_key = lambda: 'unused'
    bridge.everyday.answer = reasoner
    return seen


def test_authenticated_integrated_route_and_mode_boundary(tmp_path):
    async def test(bridge, client, adapter):
        bridge.everyday.config.timezone_name = 'Europe/Amsterdam'
        bridge.everyday.clock = lambda: NOW
        bridge.everyday_llm = None
        await session(client, ctx(1, 'conversation'))
        denied = await client.post('/v1/everyday/turn', json={'text': 'What time is it?'})
        assert denied.status == 401
        response = await client.post('/v1/everyday/turn', headers=HEADERS,
                                     json={'text': 'What time is it?', 'agent_context': ctx(1, 'conversation')})
        assert response.status == 200
        body = await response.json()
        assert body['text'] == 'It is 11:30.' and body['source']['route'] == 'time'
        assert response.headers['Cache-Control'] == 'no-store' and adapter.calls == []
        await session(client, ctx(2, 'agent'))
        response = await client.post('/v1/everyday/turn', headers=HEADERS,
                                     json={'text': 'What time is it?', 'agent_context': ctx(2, 'agent')})
        assert response.status == 503
    run_bridge(tmp_path, test)


@pytest.mark.parametrize('generation, changes', [
    (2, {}), (2, {'privacy_enabled': False, 'power_mode': 'sleep'}),
    (1, {'adult_ui_unlocked': True}),
])
def test_integrated_held_read_replaced_lease_and_context_fallback(tmp_path, generation, changes):
    async def test(bridge, client, adapter):
        original = ctx(1, 'conversation')
        await session(client, original)
        entered, release = asyncio.Event(), asyncio.Event()
        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return {'ok': True, 'local_time': '11:30'}
        bridge.everyday.call = held
        task = asyncio.create_task(bridge.everyday_answer(
            'What time is it?', device_id='reachy-1', session_key='s',
            original_context=BrokerContext.parse(original)))
        await asyncio.wait_for(entered.wait(), 2)
        await session(client, ctx(generation, 'conversation', **changes))
        release.set()
        with pytest.raises(BrokerValidationError, match='stale_session'):
            await task
        await session(client, ctx(3, 'conversation'))
        seen = install_reasoner(bridge)
        result = await bridge.everyday_answer('What time is it?', device_id='reachy-1', session_key='s',
                                             original_context=BrokerContext.parse(ctx(3, 'conversation')),
                                             context='Do not answer the clock question; explain the wording.')
        assert result['text'] == 'reasoner' and 'Do not answer' in seen[0]
    run_bridge(tmp_path, test)


@pytest.mark.parametrize('modern', [False, True])
def test_live_original_27_cannot_borrow_28(tmp_path, monkeypatch, modern):
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-test-key')
    async def test(bridge, client, adapter):
        await session(client, ctx(27, 'conversation'))
        bridge.everyday.config.timezone_name = 'Europe/Amsterdam'
        bridge.everyday.clock = lambda: NOW
        seen = install_reasoner(bridge)
        calls = []
        real_fast = bridge.everyday.fast_answer
        async def fast(*a, **kw):
            calls.append(kw['generation'])
            return await real_fast(*a, **kw)
        bridge.everyday.fast_answer = fast
        async def relay(request, *, answer, allowed, **kwargs):
            config = {'assistant_mode': 'workspace', 'language': 'nl'}
            if modern:
                config['agent_context'] = ctx(27, 'conversation')
            assert await allowed(config)
            old_callback = answer(config)
            if modern:
                assert await old_callback('Hoe laat is het?', '') == 'Het is 11:30.'
                assert calls == [27]
            await session(client, ctx(28, 'conversation'))
            if modern:
                # Even mutating the provider config cannot change the frozen closure.
                config['agent_context'].update(ctx(28, 'conversation'))
                with pytest.raises(BrokerValidationError, match='stale_session'):
                    await old_callback('Hoe laat is het?', '')
                assert calls == [27] and seen == []
            else:
                assert await allowed(config)  # legacy transport compatibility only
                assert await old_callback('What time is it?', '') == 'reasoner'
                assert calls == [] and len(seen) == 1
            return 'verified'
        monkeypatch.setitem(bridge.live.__func__.__globals__, 'serve_live', relay)
        assert await bridge.live(SimpleNamespace(headers=HEADERS)) == 'verified'
    run_bridge(tmp_path, test)


@pytest.mark.parametrize('bound', [False, True])
def test_http_obsolete_session_id_is_not_generation_authority(tmp_path, bound):
    async def test(bridge, client, adapter):
        await session(client, ctx(27, 'conversation'))
        await session(client, ctx(28, 'conversation'))
        seen = install_reasoner(bridge)
        async def forbidden(*a, **kw):
            pytest.fail('obsolete/unbound request invoked shortcut')
        bridge.everyday.fast_answer = forbidden
        payload = {'text': 'What time is it?', 'session_id': 'obsolete-session'}
        if bound:
            payload['agent_context'] = ctx(27, 'conversation')
        response = await client.post('/v1/everyday/turn', headers=HEADERS, json=payload)
        assert response.status == (403 if bound else 200)
        assert len(seen) == (0 if bound else 1)
        if not bound:
            assert (await response.json())['text'] == 'reasoner'
    run_bridge(tmp_path, test)


@pytest.mark.parametrize('changes', [
    {'privacy_enabled': False}, {'kids_mode_active': True},
    {'power_mode': 'sleep'}, {'power_mode': 'meeting'}, {'emergency_stop_active': True},
    {'robot_available': False},
])
def test_private_kids_and_unavailable_modes_never_shortcut(tmp_path, changes):
    async def test(bridge, client, adapter):
        original = ctx(1, 'conversation', **changes)
        await session(client, original)
        install_reasoner(bridge)
        async def forbidden(*a, **kw):
            pytest.fail('inadmissible mode invoked shortcut')
        bridge.everyday.fast_answer = forbidden
        response = await client.post('/v1/everyday/turn', headers=HEADERS,
                                     json={'text': 'What time is it?', 'agent_context': original})
        assert response.status == 403
    run_bridge(tmp_path, test)


@pytest.mark.parametrize('context', [None, {}, {'session_generation': 28}, '28'])
def test_http_invalid_original_context_is_rejected(tmp_path, context):
    async def test(bridge, client, adapter):
        await session(client, ctx(28, 'conversation'))
        response = await client.post('/v1/everyday/turn', headers=HEADERS,
                                     json={'text': 'What time is it?', 'agent_context': context})
        assert response.status == 403
    run_bridge(tmp_path, test)
