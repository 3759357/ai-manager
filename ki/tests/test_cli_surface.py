import contextlib
import io
import unittest

from ki.cli import _parser


class OrderPathSurfaceTest(unittest.TestCase):
    """실주문은 원장을 거치는 submit-order 하나로만 나간다."""

    def assert_rejected(self, argv: list[str]) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                _parser().parse_args(argv)

    def test_direct_order_commands_are_removed(self) -> None:
        self.assert_rejected(["order", "buy", "005930", "1", "--execute"])
        self.assert_rejected(["auto", "005930", "--buy-below", "70000", "--execute"])

    def test_market_watch_cannot_submit(self) -> None:
        self.assert_rejected(["market-watch", "--auto-submit", "--execute"])
        self.assertEqual(_parser().parse_args(["market-watch"]).command, "market-watch")

    def test_ledger_submit_path_remains(self) -> None:
        args = _parser().parse_args(["submit-order", "1", "--execute"])
        self.assertEqual(args.command, "submit-order")


if __name__ == "__main__":
    unittest.main()
