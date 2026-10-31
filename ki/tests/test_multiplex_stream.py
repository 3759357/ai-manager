import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from ki.config import Settings
from ki.websocket_client import KISWebSocketClient


class Socket:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.send = AsyncMock()
        self.pong = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = next(self.frames, None)
        if value is None:
            raise AssertionError("Unexpected reconnect")
        if isinstance(value, Exception):
            raise value
        return value


class MultiplexStreamTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = KISWebSocketClient(Settings("demo", "key", "secret", "12345678"))
        self.client.approval_key = Mock(return_value="approval")

    async def test_two_feeds_share_one_connection_and_handle_ping(self):
        ping = json.dumps({"header": {"tr_id": "PINGPONG"}})
        socket = Socket([ping, "0|OTHER|001|ignored", "0|HDFSASP0|001|book", "0|HDFSCNT0|002|trades"])
        with patch("ki.websocket_client.websockets.connect", return_value=socket) as connect:
            stream = self.client.stream_many([("HDFSASP0", "DNASAAPL"), ("HDFSCNT0", "DNASGOOGL")])
            try:
                self.assertEqual(await anext(stream), ("HDFSASP0", "book", 1))
                self.assertEqual(await anext(stream), ("HDFSCNT0", "trades", 2))
            finally:
                await stream.aclose()
            connect.assert_called_once()
            self.assertEqual(socket.send.await_count, 2)
            keys = [json.loads(c.args[0])["body"]["input"]["tr_key"] for c in socket.send.await_args_list]
            self.assertEqual(keys, ["DNASAAPL", "DNASGOOGL"])
            socket.pong.assert_awaited_once_with(ping)

    async def test_reconnect_resubscribes_both_feeds(self):
        first = Socket([OSError("disconnected")])
        second = Socket(["0|HDFSCNT0|001|trade"])
        with patch("ki.websocket_client.websockets.connect", side_effect=[first, second]), patch("ki.websocket_client.asyncio.sleep", new_callable=AsyncMock):
            stream = self.client.stream_many([("HDFSASP0", "DNASAAPL"), ("HDFSCNT0", "DNASGOOGL")])
            try:
                self.assertEqual(await anext(stream), ("HDFSCNT0", "trade", 1))
            finally:
                await stream.aclose()
            self.assertEqual(first.send.await_count, 2)
            self.assertEqual(second.send.await_count, 2)

    async def test_server_closing_normally_backs_off_before_reconnecting(self):
        class Closed(Socket):
            async def __anext__(self):
                raise StopAsyncIteration
        first = Closed([])
        second = Socket(["0|HDFSCNT0|001|trade"])
        sleep = AsyncMock()
        with patch("ki.websocket_client.websockets.connect", side_effect=[first, second]), \
                patch("ki.websocket_client.asyncio.sleep", sleep):
            stream = self.client.stream_many([("HDFSCNT0", "DNASGOOGL")])
            try:
                self.assertEqual(await anext(stream), ("HDFSCNT0", "trade", 1))
            finally:
                await stream.aclose()
        sleep.assert_awaited_once()

    async def test_existing_single_stream_interface_is_preserved(self):
        socket = Socket(["0|HDFSCNT0|001|ignored", "0|HDFSASP0|001|book"])
        with patch("ki.websocket_client.websockets.connect", return_value=socket):
            stream = self.client._stream("DNASAAPL", tr_id="HDFSASP0", max_retries=0)
            try:
                self.assertEqual(await anext(stream), ("book", 1))
            finally:
                await stream.aclose()
            self.assertEqual(socket.send.await_count, 1)
