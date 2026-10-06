import asyncio
import json

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from companion.native_adapters import json_request, public_messages, public_output


def test_structured_native_output_never_stringifies_hidden_fields():
    assert public_output({"answer": "visible?", "reasoning": "hidden", "token": "private"}) == ""
    assert (
        public_output([{"type": "thinking", "text": "hidden"}, {"type": "output_text", "text": "visible"}])
        == "visible"
    )
    assert public_messages({"data": [{"role": "assistant", "content": "actual Hermes data-envelope text"}]}) == [
        {"role": "assistant", "text": "actual Hermes data-envelope text"}
    ]


def test_native_json_reads_complete_chunked_body_not_just_first_network_fragment():
    async def scenario():
        expected = {"data": "x" * 150000}
        encoded = json.dumps(expected).encode()

        async def route(request):
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(encoded[:100])
            await asyncio.sleep(0.02)  # explicit fragmented transport fixture, not robot/model work
            await response.write(encoded[100:])
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_get("/data", route)
        async with TestServer(app) as server, ClientSession() as http:
            assert await json_request(http, "GET", str(server.make_url("/data")), headers={}) == expected

    asyncio.run(scenario())
