"""Deterministic adult Everyday routing; no hardware or provider requests."""
import asyncio

import pytest
from test_everyday_tools import NOW, make_tools, run_with_ha

from companion.reachy_agent_broker import BrokerValidationError


@pytest.mark.parametrize(('text', 'route', 'language'), [
    ('What time is it?', 'time', 'en'),
    ('What is the current time? Answer briefly.', 'time', 'en'),
    ('Hoe laat is het nu? Antwoord kort.', 'time', 'nl'),
    ('Hoe laat is het?', 'time', 'nl'),
    ("What's the date today?", 'date', 'en'),
    ('Welke datum is het vandaag?', 'date', 'nl'),
    ('What is the weather now?', 'weather', 'en'),
    ('Wat is het weer nu?', 'weather', 'nl'),
    ('Is desk light on?', 'status', 'en'),
])
def test_exact_intents(text, route, language):
    tools = make_tools('http://127.0.0.1:1')
    intent = tools.fast_intent(text, language=language)
    assert intent is not None and intent[0] == route


@pytest.mark.parametrize('text', [
    'Do not tell me the time', 'Not what time is it?', '"What time is it?"',
    'Explain the phrase what time is it', 'What time is it and turn on the lamp?',
    'What time is it? No, tomorrow', 'What time is it in my project?',
    'What time is my meeting?', 'What time is it in Tokyo?',
    'Hoe laat is het? Nee, de datum', 'Zeg niet hoe laat het is',
    'Turn on desk light', 'Is lamp on?', 'Is kettle on?',
    'Is desk light on and hall light off?', 'Is desk light on? Actually turn it off',
    'What is the weather tomorrow?', 'What is the weather in Tokyo?',
])
def test_ambiguous_complex_and_actions_do_not_match(text):
    assert make_tools('http://127.0.0.1:1').fast_intent(text) is None


def test_clock_fresh_localized_and_no_cache():
    async def main():
        tools = make_tools('http://127.0.0.1:1')
        values = iter([NOW, NOW + 60])
        tools.clock = lambda: next(values)
        a = await tools.fast_answer('What time is it?', http=None, device_id='d', generation=1)
        b = await tools.fast_answer('Hoe laat is het?', http=None, device_id='d', generation=1)
        assert a['text'] == 'It is 11:30.' and b['text'] == 'Het is 11:31.'
        assert a['source']['route'] == 'time' and a['source']['kind'] == 'everyday'
        assert a['timing']['backend_ms'] >= 0
        date = await make_tools('').fast_answer('Welke datum is het vandaag?', http=None, device_id='d', generation=1)
        assert date['text'] == 'Het is 12 oktober 2026.'
    asyncio.run(main())


def test_reads_existing_tools_and_never_mutates():
    async def test(tools, ha, http):
        a = await tools.fast_answer('Is desk light on?', http=http, device_id='d', generation=1)
        assert a['text'] == 'desk light: off.'
        ha.states['light.desk']['state'] = 'on'
        b = await tools.fast_answer('Is desk light on?', http=http, device_id='d', generation=1)
        assert b['text'] == 'desk light: on.'
        weather = await tools.fast_answer('Wat is het weer nu?', http=http, device_id='d', generation=1)
        assert '12.5' in weather['text']
        assert ha.calls == [] and ha.forecast_requests == []
    run_with_ha(test)


def test_currentness_and_kids_gate_before_read_and_after_await():
    async def main():
        tools = make_tools('')
        async def forbidden(*args, **kwargs):
            raise AssertionError('read after revocation')
        tools.call = forbidden
        for flags in ({'is_current': lambda: False}, {'kids_live': lambda: True}):
            with pytest.raises(BrokerValidationError):
                await tools.fast_answer('What time is it?', http=None, device_id='d', generation=1, **flags)
        entered, release = asyncio.Event(), asyncio.Event()
        current = [True]
        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return {'ok': True, 'local_time': '11:30'}
        tools.call = held
        task = asyncio.create_task(tools.fast_answer('What time is it?', http=None, device_id='d', generation=1,
                                                   is_current=lambda: current[0]))
        await entered.wait()
        current[0] = False
        release.set()
        with pytest.raises(BrokerValidationError):
            await task
    asyncio.run(main())
