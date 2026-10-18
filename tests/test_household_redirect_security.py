"""No credential-bearing redirect handshake; network-free real connect loop."""
import asyncio
from types import SimpleNamespace

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from homebody.household_client import connect


@pytest.mark.parametrize('location', [
    'ws://outside.example/v1/household/robot',
    'wss://outside.example/v1/household/robot',
    '/another-path',
])
@pytest.mark.parametrize('status', [301, 302, 307, 308])
def test_redirect_never_performs_second_handshake(location, status):
    async def exercise():
        client = connect('ws://127.0.0.1:8644/v1/household/robot',
                         additional_headers={'Authorization': 'Bearer synthetic-robot'})
        attempts = []
        aborted = []

        async def handshake(headers, user_agent):
            attempts.append((client.uri, dict(headers)))
            raise InvalidStatus(Response(status, 'Redirect', Headers({'Location': location})))

        async def create_connection():
            return SimpleNamespace(handshake=handshake,
                                   transport=SimpleNamespace(abort=lambda: aborted.append(True)))

        client.create_connection = create_connection
        with pytest.raises(InvalidStatus):
            await client
        assert attempts == [('ws://127.0.0.1:8644/v1/household/robot',
                             {'Authorization': 'Bearer synthetic-robot'})]
        assert aborted == [True]
    asyncio.run(exercise())
