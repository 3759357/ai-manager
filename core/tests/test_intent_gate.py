import unittest
from decimal import Decimal
from unittest.mock import patch

from core import cli
from core.config import Settings
from core.models import UnifiedOrderRequest
from core.portfolio import BrokerPortfolio, build_snapshot


def _request(quantity: int = 1, price: str = "100") -> UnifiedOrderRequest:
    return UnifiedOrderRequest(
        broker="toss",
        side="buy",
        symbol="AAPL",
        quantity=quantity,
        price=Decimal(price),
        currency="USD",
        source="test",
    )


def _snapshot(cash: str, brokers_detail=None, extra=Decimal("0")):
    portfolios = [
        BrokerPortfolio(broker="kis", cash=Decimal(cash)),
        BrokerPortfolio(broker="toss", cash=Decimal("0")),
    ]
    return build_snapshot(
        portfolios,
        leveraged_symbols=frozenset(),
        extra_pending_buy=extra,
    )


class IntentRiskGateTest(unittest.TestCase):
    def test_allow_when_cash_is_sufficient(self) -> None:
        with patch.object(
            cli,
            "_live_snapshot",
            side_effect=lambda settings, extra_pending_buy: _snapshot(
                "10000",
                extra=extra_pending_buy,
            ),
        ):
            result = cli._intent_risk_gate(Settings(), _request())

        self.assertEqual(result["decision"], "ALLOW")
        self.assertEqual(result["intent_amount"], "100")

    def test_block_when_cash_pct_falls_below_minimum(self) -> None:
        # 현금 1000에서 900짜리 매수 → 잔여 현금 10% < 35%
        with patch.object(
            cli,
            "_live_snapshot",
            side_effect=lambda settings, extra_pending_buy: _snapshot(
                "1000",
                extra=extra_pending_buy,
            ),
        ):
            result = cli._intent_risk_gate(
                Settings(),
                _request(quantity=9, price="100"),
            )

        self.assertEqual(result["decision"], "BLOCK")
        self.assertTrue(
            any("현금 비중" in reason for reason in result["reasons"])
        )

    def test_broker_collection_error_blocks_conservatively(self) -> None:
        def snapshot_with_error(settings, extra_pending_buy):
            portfolios = [
                BrokerPortfolio(broker="kis", cash=Decimal("10000")),
                BrokerPortfolio(broker="toss", error="인증 실패"),
            ]
            return build_snapshot(
                portfolios,
                leveraged_symbols=frozenset(),
                extra_pending_buy=extra_pending_buy,
            )

        with patch.object(
            cli,
            "_live_snapshot",
            side_effect=snapshot_with_error,
        ):
            result = cli._intent_risk_gate(Settings(), _request())

        self.assertEqual(result["decision"], "BLOCK")
        self.assertTrue(
            any("수집 실패" in reason for reason in result["reasons"])
        )


if __name__ == "__main__":
    unittest.main()
