import unittest
from decimal import Decimal

from core.risk import (
    PortfolioPosition,
    PortfolioRiskLimits,
    PortfolioSnapshot,
    evaluate_portfolio_risk,
)


class PortfolioRiskTest(unittest.TestCase):
    def test_allows_balanced_cash_and_leverage(self) -> None:
        report = evaluate_portfolio_risk(
            PortfolioSnapshot(
                cash=Decimal("3000"),
                positions=(
                    PortfolioPosition(
                        "MUU",
                        Decimal("1"),
                        Decimal("1000"),
                        leveraged=True,
                    ),
                    PortfolioPosition("GOOGL", Decimal("2"), Decimal("300")),
                ),
            ),
            PortfolioRiskLimits(
                min_cash_pct=Decimal("40"),
                max_leveraged_exposure_pct=Decimal("25"),
                max_single_position_pct=Decimal("30"),
            ),
        )

        self.assertEqual(report["decision"], "ALLOW")
        self.assertEqual(report["cash_pct"], "65.22")
        self.assertEqual(report["leveraged_exposure_pct"], "21.74")

    def test_blocks_when_projected_cash_breaches_minimum(self) -> None:
        report = evaluate_portfolio_risk(
            PortfolioSnapshot(
                cash=Decimal("2000"),
                positions=(
                    PortfolioPosition("MUU", Decimal("1"), Decimal("1200")),
                ),
                pending_buy_amount=Decimal("1000"),
            ),
            PortfolioRiskLimits(min_cash_pct=Decimal("40")),
        )

        self.assertEqual(report["decision"], "BLOCK")
        self.assertIn("현금 비중", report["reasons"][0])
        self.assertEqual(report["cash_pct"], "31.25")

    def test_blocks_excessive_leveraged_exposure(self) -> None:
        report = evaluate_portfolio_risk(
            PortfolioSnapshot(
                cash=Decimal("1000"),
                positions=(
                    PortfolioPosition(
                        "MUU",
                        Decimal("1"),
                        Decimal("1200"),
                        leveraged=True,
                    ),
                    PortfolioPosition(
                        "GGLL",
                        Decimal("5"),
                        Decimal("100"),
                        leveraged=True,
                    ),
                ),
            ),
            PortfolioRiskLimits(max_leveraged_exposure_pct=Decimal("50")),
        )

        self.assertEqual(report["decision"], "BLOCK")
        self.assertIn("레버리지 실효 노출", report["reasons"][0])
        self.assertEqual(report["leveraged_exposure_pct"], "62.96")


if __name__ == "__main__":
    unittest.main()
