"""주문 경로 리스크 한도가 정책 파일 하나에서 오고, 새 주문을 반영해 판단하는지 확인한다."""
import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from core import cli
from core.config import Settings
from core.guard import load_policy
from core.portfolio import (
    KIS_NOT_PLACED_STATUSES,
    BrokerPortfolio,
    build_snapshot,
    daily_buy_amount,
    trading_day_start,
)
from core.risk import (
    OrderImpact,
    PortfolioPosition,
    PortfolioRiskLimits,
    PortfolioSnapshot,
    evaluate_portfolio_risk,
)

D = Decimal
DAY_START = datetime(2026, 10, 30, 4, 0, tzinfo=timezone.utc)  # 뉴욕 10/30 00:00


def snapshot(cash="10000", positions=(), pending="0", bought_today="0"):
    return PortfolioSnapshot(
        cash=D(cash),
        positions=tuple(positions),
        pending_buy_amount=D(pending),
        bought_today=None if bought_today is None else D(bought_today),
    )


class LimitsFromPolicyTest(unittest.TestCase):
    def test_defaults_are_policy_v1(self) -> None:
        limits = PortfolioRiskLimits.from_policy(load_policy(), Settings())

        self.assertEqual(limits.min_cash_pct, D("20"))
        self.assertEqual(limits.max_single_position_pct, D("15"))
        self.assertEqual(limits.max_leveraged_exposure_pct, D("10"))
        self.assertEqual(limits.max_order_usd, D("1500"))
        self.assertEqual(limits.max_daily_new_buy_usd, D("1500"))
        self.assertIn("policy v1", limits.source)

    def test_environment_can_only_tighten(self) -> None:
        settings = Settings(
            min_cash_pct=D("35"),               # 정책 20보다 엄격 → 반영
            max_single_position_pct=D("25"),    # 정책 15보다 느슨 → 무시
            max_leveraged_exposure_pct=D("50"),  # 느슨 → 무시
            max_daily_new_buy_usd=D("1000"),    # 엄격 → 반영, 주문당 한도도 함께 낮춤
        )
        limits = PortfolioRiskLimits.from_policy(load_policy(), settings)

        self.assertEqual(limits.min_cash_pct, D("35"))
        self.assertEqual(limits.max_single_position_pct, D("15"))
        self.assertEqual(limits.max_leveraged_exposure_pct, D("10"))
        self.assertEqual(limits.max_daily_new_buy_usd, D("1000"))
        self.assertEqual(limits.max_order_usd, D("1000"))
        self.assertIn("min_cash_pct", limits.source)

    def test_env_file_without_limits_uses_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = Path(temp_dir) / ".env"
            env.write_text("CORE_LEVERAGED_SYMBOLS=SOXL:2,TQQQ\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                settings = Settings.from_env(env)

        self.assertIsNone(settings.min_cash_pct)
        self.assertEqual(settings.leverage_multipliers, {"SOXL": D("2"), "TQQQ": D("3")})
        self.assertEqual(settings.leveraged_symbols, ("SOXL", "TQQQ"))

    def test_leverage_multiplier_must_exceed_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = Path(temp_dir) / ".env"
            env.write_text("CORE_LEVERAGED_SYMBOLS=SOXL:1\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ValueError):
                    Settings.from_env(env)


class OrderAwareRiskTest(unittest.TestCase):
    limits = PortfolioRiskLimits()

    def test_new_order_counts_toward_single_position(self) -> None:
        held = [PortfolioPosition("AAPL", D("5"), D("200"))]  # 1000 / 10000 = 10%
        without = evaluate_portfolio_risk(snapshot(cash="9000", positions=held), self.limits)
        with_order = evaluate_portfolio_risk(
            snapshot(cash="9000", positions=held, pending="800"),
            self.limits,
            order=OrderImpact("AAPL", D("800")),
        )

        self.assertEqual(without["decision"], "ALLOW")
        self.assertEqual(with_order["decision"], "BLOCK")
        self.assertEqual(with_order["order"]["symbol_weight_after_pct"], "18.00")
        self.assertTrue(any("매수 후 종목 비중" in r for r in with_order["reasons"]))

    def test_order_size_limit(self) -> None:
        result = evaluate_portfolio_risk(
            snapshot(cash="100000", pending="1600"), self.limits, order=OrderImpact("AAPL", D("1600"))
        )
        self.assertTrue(any("주문당 한도" in r for r in result["reasons"]))

    def test_daily_buy_limit_blocks(self) -> None:
        result = evaluate_portfolio_risk(
            snapshot(cash="100000", pending="200", bought_today="1400"),
            self.limits,
            order=OrderImpact("AAPL", D("200")),
        )
        self.assertEqual(result["decision"], "BLOCK")
        self.assertTrue(any("일일 한도" in r for r in result["reasons"]))

    def test_unknown_daily_total_blocks(self) -> None:
        result = evaluate_portfolio_risk(
            snapshot(cash="100000", pending="100", bought_today=None),
            self.limits,
            order=OrderImpact("AAPL", D("100")),
        )
        self.assertTrue(any("오늘 매수 합계를 확인할 수 없어" in r for r in result["reasons"]))

    def test_leveraged_exposure_is_effective(self) -> None:
        held = [PortfolioPosition("TQQQ", D("1"), D("100"), leveraged=True, leverage_multiplier=D("3"))]
        allowed = evaluate_portfolio_risk(
            snapshot(cash="9900", positions=held, pending="200"), self.limits,
            order=OrderImpact("SOXL", D("200"), leverage_multiplier=D("3")),
        )
        blocked = evaluate_portfolio_risk(
            snapshot(cash="9900", positions=held, pending="300"), self.limits,
            order=OrderImpact("SOXL", D("300"), leverage_multiplier=D("3")),
        )

        self.assertEqual(allowed["leveraged_exposure_pct"], "9.00")
        self.assertEqual(allowed["decision"], "ALLOW")
        self.assertEqual(blocked["leveraged_exposure_pct"], "12.00")
        self.assertEqual(blocked["decision"], "BLOCK")


class DailyBuyAmountTest(unittest.TestCase):
    def order(self, id, status, created="2026-10-30T15:00:00+00:00", side="buy", amount="100"):
        return {"id": id, "side": side, "status": status, "created_at": created,
                "estimated_amount": amount, "currency": "USD"}

    def test_counts_only_todays_placed_buys(self) -> None:
        orders = [
            self.order(1, "FILLED"),
            self.order(2, "CANCELED"),                     # 일부 체결 가능성 → 포함
            self.order(3, "REJECTED"),                     # 나가지 않음 → 제외
            self.order(4, "FILLED", created="2026-10-29T20:00:00+00:00"),  # 어제 → 제외
            self.order(5, "FILLED", side="sell"),
            self.order(6, "RISK_APPROVED"),
        ]
        total = daily_buy_amount(orders, KIS_NOT_PLACED_STATUSES, since=DAY_START)
        excluded = daily_buy_amount(orders, KIS_NOT_PLACED_STATUSES, since=DAY_START,
                                    exclude_ids=frozenset({6}))

        self.assertEqual(total, D("300"))
        self.assertEqual(excluded, D("200"))

    def test_trading_day_starts_at_new_york_midnight(self) -> None:
        start = trading_day_start(datetime(2026, 10, 31, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(start.astimezone(timezone.utc), DAY_START)

    def test_snapshot_excludes_the_order_being_submitted(self) -> None:
        portfolios = [
            BrokerPortfolio(broker="kis", cash=D("5000"), orders=[self.order(7, "RISK_APPROVED")]),
            BrokerPortfolio(broker="toss", cash=D("5000"), orders=[self.order(7, "SUBMITTED")]),
        ]
        snap, detail = build_snapshot(portfolios, day_start=DAY_START, exclude_order=("kis", 7))

        self.assertEqual(snap.bought_today, D("100"))
        self.assertEqual(detail["bought_today"], "100")

    def test_missing_ledger_means_unknown(self) -> None:
        portfolios = [BrokerPortfolio(broker="kis", cash=D("5000"), orders=None)]
        snap, _ = build_snapshot(portfolios, day_start=DAY_START)
        self.assertIsNone(snap.bought_today)


class FakeAdapter:
    def __init__(self, summary):
        self.summary = summary
        self.submitted = []

    def order_summary(self, order_id):
        return self.summary


class FakeService:
    def __init__(self, summary):
        self.adapters = {"toss": FakeAdapter(summary)}
        self.submitted = []

    def submit_order(self, broker, order_id, *, execute):
        self.submitted.append(order_id)
        return {"status": "SUBMITTED"}


class SubmitGateTest(unittest.TestCase):
    summary = {"side": "BUY", "symbol": "AAPL", "currency": "USD", "amount": D("200"), "status": "RISK_APPROVED"}

    def test_skip_risk_option_is_gone(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli._parser().parse_args(["intent", "buy", "AAPL", "1", "--price", "1", "--skip-risk"])

    def test_submit_gate_does_not_double_count_the_candidate(self) -> None:
        calls = []

        def live(settings, *, extra_pending_buy, exclude_order=None):
            calls.append((extra_pending_buy, exclude_order))
            return build_snapshot(
                [BrokerPortfolio(broker="toss", cash=D("10000"), pending_buy_amount=D("200"),
                                 orders=[{"id": 9, "side": "BUY", "status": "RISK_APPROVED",
                                          "created_at": None, "estimated_amount": "200"}])],
                day_start=DAY_START, exclude_order=exclude_order,
            )

        with mock.patch.object(cli, "_live_snapshot", side_effect=live):
            result = cli._submit_risk_gate(Settings(), FakeService(self.summary), "toss", 9)

        self.assertEqual(calls, [(D("0"), ("toss", 9))])
        self.assertEqual(result["decision"], "ALLOW")
        self.assertEqual(result["projected_cash"], "9800.00")
        self.assertEqual(result["order"]["bought_today"], "0.00")

    def test_sell_orders_skip_the_buy_gate(self) -> None:
        sell = dict(self.summary, side="SELL")
        self.assertIsNone(cli._submit_risk_gate(Settings(), FakeService(sell), "toss", 9))

    def test_blocked_submit_never_reaches_the_broker(self) -> None:
        service = FakeService(self.summary)

        def over_limit(settings, *, extra_pending_buy, exclude_order=None):
            return build_snapshot(
                [BrokerPortfolio(broker="toss", cash=D("10000"), orders=[
                    {"id": 1, "side": "BUY", "status": "FILLED", "created_at": None, "estimated_amount": "1400"}
                ])],
                day_start=DAY_START, exclude_order=exclude_order,
            )

        with tempfile.TemporaryDirectory() as temp_dir, \
                mock.patch.object(cli.Settings, "from_env",
                                  return_value=Settings(kill_switch_path=Path(temp_dir) / "ks.json")), \
                mock.patch.object(cli, "_service", return_value=service), \
                mock.patch.object(cli, "_live_snapshot", side_effect=over_limit), \
                mock.patch.object(cli, "_record_order_event"), \
                mock.patch.object(cli, "_record_command_error"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["submit", "toss", "9", "--execute"])

        self.assertEqual(service.submitted, [])


if __name__ == "__main__":
    unittest.main()
