import argparse
import unittest
from decimal import Decimal

from core.cli import _parse_position, _selected_brokers
from core.config import Settings


class CliTest(unittest.TestCase):
    def test_selected_brokers_only_initializes_requested_broker(self) -> None:
        settings = Settings(default_broker="toss")

        self.assertEqual(
            _selected_brokers(
                argparse.Namespace(command="quote", broker="kis"),
                settings,
            ),
            {"kis"},
        )
        self.assertEqual(
            _selected_brokers(
                argparse.Namespace(command="intent", broker=None),
                settings,
            ),
            {"toss"},
        )
        self.assertEqual(
            _selected_brokers(
                argparse.Namespace(command="orders", broker="both"),
                settings,
            ),
            {"kis", "toss"},
        )
        self.assertEqual(
            _selected_brokers(
                argparse.Namespace(command="portfolio-risk"),
                settings,
            ),
            set(),
        )

    def test_parse_position_marks_leveraged_position(self) -> None:
        position = _parse_position("muu:1:1122:leveraged")

        self.assertEqual(position.symbol, "MUU")
        self.assertEqual(position.quantity, Decimal("1"))
        self.assertEqual(position.price, Decimal("1122"))
        self.assertTrue(position.leveraged)


if __name__ == "__main__":
    unittest.main()
