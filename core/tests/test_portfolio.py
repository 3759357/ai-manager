import unittest
from decimal import Decimal

from core.portfolio import (
    BrokerPortfolio,
    KIS_TERMINAL_STATUSES,
    TOSS_TERMINAL_STATUSES,
    build_snapshot,
    parse_kis_cash,
    parse_kis_positions,
    parse_toss_cash,
    parse_toss_holdings,
    pending_buy_amount,
)


class ParseKisTest(unittest.TestCase):
    def test_parses_balance_rows(self) -> None:
        raw = {
            "positions": [
                {
                    "ovrs_pdno": "googl",
                    "ovrs_cblc_qty": "1",
                    "now_pric2": "368.7600",
                    "tr_crcy_cd": "USD",
                },
                {
                    "ovrs_pdno": "MUU",
                    "ovrs_cblc_qty": "2",
                    "now_pric2": "",
                    "ovrs_stck_evlu_amt": "2244.00",
                    "tr_crcy_cd": "USD",
                },
                {"ovrs_pdno": "ZERO", "ovrs_cblc_qty": "0"},
            ]
        }

        rows = parse_kis_positions(raw)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["symbol"], "GOOGL")
        self.assertEqual(rows[0]["price"], Decimal("368.7600"))
        self.assertEqual(rows[1]["price"], Decimal("1122.00"))

    def test_parses_cash_with_fallback_keys(self) -> None:
        self.assertEqual(
            parse_kis_cash({"ord_psbl_frcr_amt": "1244.97"}),
            Decimal("1244.97"),
        )
        self.assertEqual(
            parse_kis_cash({"frcr_ord_psbl_amt1": "10.5"}),
            Decimal("10.5"),
        )
        with self.assertRaises(ValueError):
            parse_kis_cash({})


class ParseTossTest(unittest.TestCase):
    def test_parses_wrapped_holdings(self) -> None:
        raw = {
            "holdings": [
                {
                    "symbol": "AAPL",
                    "quantity": "1",
                    "currentPrice": "278.91",
                    "currency": "USD",
                },
                {
                    "symbol": "KRWSTOCK",
                    "quantity": "3",
                    "currentPrice": "10000",
                    "currency": "KRW",
                },
            ]
        }

        rows = parse_toss_holdings(raw)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["symbol"], "AAPL")
        self.assertEqual(rows[1]["currency"], "KRW")

    def test_parses_bare_list_and_alt_keys(self) -> None:
        rows = parse_toss_holdings(
            [
                {
                    "symbol": "GGLL",
                    "holdingQuantity": "6",
                    "evaluationAmount": "630.60",
                }
            ]
        )

        self.assertEqual(rows[0]["quantity"], Decimal("6"))
        self.assertEqual(rows[0]["price"], Decimal("105.10"))

    def test_parses_cash(self) -> None:
        self.assertEqual(
            parse_toss_cash({"cashBuyingPower": "1500.25"}),
            Decimal("1500.25"),
        )
        with self.assertRaises(ValueError):
            parse_toss_cash({})


class PendingBuyTest(unittest.TestCase):
    def test_sums_open_buy_orders_only(self) -> None:
        orders = [
            {"side": "buy", "status": "RISK_APPROVED", "estimated_amount": "100"},
            {"side": "BUY", "status": "SUBMITTED", "estimated_amount": "200"},
            {"side": "BUY", "status": "FILLED", "estimated_amount": "400"},
            {"side": "SELL", "status": "SUBMITTED", "estimated_amount": "800"},
        ]

        self.assertEqual(
            pending_buy_amount(orders, KIS_TERMINAL_STATUSES),
            Decimal("300"),
        )
        self.assertEqual(
            pending_buy_amount(
                [{"side": "BUY", "status": "BLOCKED", "estimated_amount": "50"}],
                TOSS_TERMINAL_STATUSES,
            ),
            Decimal("0"),
        )


class BuildSnapshotTest(unittest.TestCase):
    def test_merges_same_symbol_across_brokers(self) -> None:
        brokers = [
            BrokerPortfolio(
                broker="kis",
                cash=Decimal("1000"),
                positions=[
                    {
                        "symbol": "GOOGL",
                        "quantity": Decimal("1"),
                        "price": Decimal("300"),
                        "currency": "USD",
                    }
                ],
                pending_buy_amount=Decimal("100"),
            ),
            BrokerPortfolio(
                broker="toss",
                cash=Decimal("500"),
                positions=[
                    {
                        "symbol": "GOOGL",
                        "quantity": Decimal("1"),
                        "price": Decimal("310"),
                        "currency": "USD",
                    },
                    {
                        "symbol": "SOXL",
                        "quantity": Decimal("2"),
                        "price": Decimal("50"),
                        "currency": "USD",
                    },
                ],
                pending_buy_amount=Decimal("50"),
            ),
        ]

        snapshot, detail = build_snapshot(
            brokers,
            leveraged_symbols=frozenset({"SOXL"}),
        )

        self.assertEqual(snapshot.cash, Decimal("1500"))
        self.assertEqual(snapshot.pending_buy_amount, Decimal("150"))
        googl = next(p for p in snapshot.positions if p.symbol == "GOOGL")
        self.assertEqual(googl.quantity, Decimal("2"))
        self.assertEqual(googl.price, Decimal("305"))
        soxl = next(p for p in snapshot.positions if p.symbol == "SOXL")
        self.assertTrue(soxl.leveraged)
        self.assertFalse(googl.leveraged)
        self.assertEqual(len(detail["brokers"]), 2)

    def test_skips_non_usd_and_records_broker_error(self) -> None:
        brokers = [
            BrokerPortfolio(
                broker="kis",
                cash=Decimal("1000"),
                positions=[
                    {
                        "symbol": "KRWSTOCK",
                        "quantity": Decimal("1"),
                        "price": Decimal("10000"),
                        "currency": "KRW",
                    }
                ],
            ),
            BrokerPortfolio(broker="toss", error="인증 실패"),
        ]

        snapshot, detail = build_snapshot(
            brokers,
            leveraged_symbols=frozenset(),
        )

        self.assertEqual(snapshot.positions, ())
        self.assertEqual(len(detail["skipped"]), 1)
        self.assertEqual(detail["brokers"][1]["error"], "인증 실패")

    def test_extra_pending_buy_is_added(self) -> None:
        brokers = [
            BrokerPortfolio(broker="kis", cash=Decimal("1000")),
        ]

        snapshot, _ = build_snapshot(
            brokers,
            leveraged_symbols=frozenset(),
            extra_pending_buy=Decimal("250"),
        )

        self.assertEqual(snapshot.pending_buy_amount, Decimal("250"))

    def test_all_brokers_failed_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_snapshot(
                [BrokerPortfolio(broker="kis", error="오류")],
                leveraged_symbols=frozenset(),
            )


if __name__ == "__main__":
    unittest.main()
