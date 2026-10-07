import unittest

from ki.config import Settings
from ki.trader import AutoTrader, ThresholdStrategy


class FakeClient:
    def __init__(self, price: int, holding: int = 0) -> None:
        self.price = price
        self.holding = holding
        self.orders = []

    def current_price(self, symbol: str) -> int:
        return self.price

    def holding_quantity(self, symbol: str) -> int:
        return self.holding

    def order(self, **kwargs):
        self.orders.append(kwargs)
        return {"ODNO": "1"}


class ThresholdStrategyTest(unittest.TestCase):
    def test_buy_signal(self) -> None:
        strategy = ThresholdStrategy("005930", 1, buy_below=70_000)
        self.assertEqual(strategy.decide(69_900), "buy")
        self.assertIsNone(strategy.decide(70_100))

    def test_dry_run_never_calls_order_api(self) -> None:
        settings = Settings(
            environment="demo",
            app_key="key",
            app_secret="secret",
            account_no="12345678",
        )
        client = FakeClient(price=69_000)
        trader = AutoTrader(client, settings, sleep=lambda _: None)
        result = trader.run(
            ThresholdStrategy("005930", 1, buy_below=70_000),
            interval_seconds=1,
            max_orders=1,
            execute=False,
        )
        self.assertEqual(result[0]["mode"], "dry-run")
        self.assertEqual(client.orders, [])

    def test_order_amount_limit_blocks_signal(self) -> None:
        settings = Settings(
            environment="demo",
            app_key="key",
            app_secret="secret",
            account_no="12345678",
            max_order_amount=50_000,
        )
        client = FakeClient(price=69_000)
        trader = AutoTrader(client, settings, sleep=lambda _: None)
        with self.assertRaises(ValueError):
            trader.run(
                ThresholdStrategy("005930", 1, buy_below=70_000),
                interval_seconds=1,
                max_orders=1,
                execute=False,
            )
