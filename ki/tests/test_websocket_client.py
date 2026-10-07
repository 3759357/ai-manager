import unittest

from ki.websocket_client import (
    KISWebSocketClient,
    parse_overseas_orderbook,
    parse_overseas_trades,
)


class WebSocketClientTest(unittest.TestCase):
    def test_parse_legacy_seventeen_field_orderbook(self) -> None:
        payload = "^".join(
            [
                "DNASAAPL",
                "AAPL",
                "4",
                "20260610",
                "113000",
                "20260611",
                "003000",
                "1000",
                "1200",
                "10",
                "20",
                "289.9000",
                "290.0000",
                "50",
                "60",
                "5",
                "6",
            ]
        )
        book = parse_overseas_orderbook(payload)
        self.assertEqual(book.symbol, "AAPL")
        self.assertEqual(book.bid_price, "289.9000")
        self.assertEqual(book.ask_price, "290.0000")
        self.assertEqual(book.spread, "0.1000")
        self.assertEqual(book.korea_time, "003000")

    def test_subscription_message(self) -> None:
        message = KISWebSocketClient._subscription_message("approval", "DNASAAPL")
        self.assertEqual(message["header"]["tr_type"], "1")
        self.assertEqual(message["body"]["input"]["tr_id"], "HDFSASP0")
        self.assertEqual(message["body"]["input"]["tr_key"], "DNASAAPL")

    def test_parse_overseas_trade(self) -> None:
        values = [
            "DNASAAPL",
            "AAPL",
            "4",
            "20260623",
            "20260623",
            "103015",
            "20260623",
            "233015",
            "299.00",
            "302.00",
            "298.50",
            "301.25",
            "2",
            "1.25",
            "0.42",
            "301.20",
            "301.25",
            "30",
            "20",
            "5",
            "123456",
            "37123456",
            "70000",
            "53456",
            "130.94",
            "2",
        ]

        trades = parse_overseas_trades("^".join(values), data_count=1)

        self.assertEqual(trades[0].symbol, "AAPL")
        self.assertEqual(trades[0].last_price, "301.25")
        self.assertEqual(trades[0].trade_quantity, "5")
        self.assertEqual(trades[0].execution_strength, "130.94")

    def test_trade_subscription_message(self) -> None:
        message = KISWebSocketClient._subscription_message(
            "approval",
            "DNASAAPL",
            tr_id="HDFSCNT0",
        )
        self.assertEqual(message["body"]["input"]["tr_id"], "HDFSCNT0")
