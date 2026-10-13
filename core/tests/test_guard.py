import contextlib
import dataclasses
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from core import guard
from core.guard import AccountState, Holding, OrderIntent, PolicyError, Quote, StrategyState

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
POLICY = guard.load_policy()


def account(**over):
    base = dict(as_of=NOW, nav=D("100000"), cash=D("40000"),
                holdings=(Holding("AAPL", D("50"), D("200")), Holding("MSFT", D("20"), D("400"))),
                drawdown_pct=D("-2"), equity_cap_pct=D("70"), trading_book_nav=D("10000"))
    base.update(over)
    return AccountState(**base)


def quote(symbol="AAPL", bid="199.90", ask="200.10", age=0):
    return Quote(symbol, D(bid), D(ask), NOW - timedelta(seconds=age))


def buy(**over):
    base = dict(intent_id="i-1", book="invest", symbol="AAPL", side="buy", quantity=D("5"), limit_price=D("200.05"))
    base.update(over)
    return OrderIntent(**base)


def codes(decision):
    return {c.code for c in decision.blocks}


class PolicyTests(unittest.TestCase):
    def test_packaged_policy_matches_plan(self):
        self.assertEqual(POLICY.version, "v1")
        self.assertEqual(POLICY.account["min_cash_pct"], D("20"))
        self.assertEqual(POLICY.account["emergency_drawdown_pct"], D("-15"))
        self.assertEqual(POLICY.position["max_single_pct"], D("15"))
        self.assertEqual(POLICY.alerts["market_drop_pct"], (D("-3"), D("-5"), D("-7")))
        json.dumps(POLICY.to_dict())

    def _raw(self):
        import tomllib
        from importlib import resources
        return tomllib.loads(resources.files("core").joinpath("policy/v1.toml").read_text(encoding="utf-8"))

    def test_missing_value_is_rejected_not_unlimited(self):
        raw = self._raw()
        del raw["order"]["max_order_usd"]
        with self.assertRaisesRegex(PolicyError, "max_order_usd"):
            guard.parse_policy(raw)

    def test_unknown_and_wrong_sign_values_are_rejected(self):
        raw = self._raw()
        raw["order"]["max_leverage"] = 3
        with self.assertRaises(PolicyError):
            guard.parse_policy(raw)
        raw = self._raw()
        raw["account"]["emergency_drawdown_pct"] = 15
        with self.assertRaisesRegex(PolicyError, "음수"):
            guard.parse_policy(raw)
        raw = self._raw()
        raw["alerts"]["market_drop_pct"] = [-5, -3]
        with self.assertRaises(PolicyError):
            guard.parse_policy(raw)

    def test_policy_path_from_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.toml"
            text = guard.resources.files("core").joinpath("policy/v1.toml").read_text(encoding="utf-8")
            path.write_text(text.replace('version = "v1"', 'version = "v1-test"'), encoding="utf-8")
            with patch.dict("os.environ", {"AI_MANAGER_POLICY_PATH": str(path)}):
                self.assertEqual(guard.load_policy().version, "v1-test")


class BuyTests(unittest.TestCase):
    def check(self, intent=None, acct=None, q=None):
        return guard.evaluate(intent or buy(), acct or account(), q or quote(), POLICY, NOW)

    def test_ordinary_invest_buy_is_allowed(self):
        decision = self.check()
        self.assertTrue(decision.allowed, decision.blocks)
        self.assertEqual(decision.to_dict()["decision"], "ALLOW")

    def test_unknown_state_blocks_buys(self):
        self.assertIn("drawdown_unknown", codes(self.check(acct=account(drawdown_pct=None))))
        self.assertIn("equity_cap_unknown", codes(self.check(acct=account(equity_cap_pct=None))))
        self.assertIn("account_unknown", codes(self.check(acct=account(complete=False))))
        self.assertIn("account_unknown", codes(self.check(acct=account(as_of=NOW - timedelta(minutes=10)))))

    def test_emergency_drawdown_stops_new_buys(self):
        self.assertIn("emergency_stop", codes(self.check(acct=account(drawdown_pct=D("-15")))))
        self.assertTrue(self.check(acct=account(drawdown_pct=D("-14.99"))).allowed)

    def test_quote_and_price_rules(self):
        self.assertIn("stale_quote", codes(self.check(q=quote(age=31))))
        self.assertIn("price_through_book", codes(self.check(intent=buy(limit_price=D("200.20")))))
        self.assertIn("not_limit", codes(self.check(intent=buy(limit_price=None))))
        self.assertIn("bad_quote", codes(self.check(q=quote(bid="201", ask="200"))))
        self.assertIn("price_deviation", codes(self.check(intent=buy(limit_price=D("197")))))

    def test_order_and_daily_size(self):
        self.assertIn("order_size", codes(self.check(intent=buy(quantity=D("8")))))  # $1,600
        self.assertIn("daily_buy", codes(self.check(acct=account(bought_today_usd=D("1000")))))
        self.assertIn("daily_buy", codes(self.check(acct=account(bought_today_usd=D("400"), pending_buy_usd=D("200")))))

    def test_cash_floor_counts_pending_buys(self):
        self.assertIn("cash_floor", codes(self.check(acct=account(cash=D("20500"), pending_buy_usd=D("0")),
                                                     intent=buy(quantity=D("5")))))
        self.assertTrue(self.check(acct=account(cash=D("21100"))).allowed)

    def test_position_limits_tighten_for_high_vol_and_earnings(self):
        acct = account(holdings=(Holding("AAPL", D("49"), D("200")),))  # 9.8%
        self.assertTrue(self.check(acct=acct).allowed)
        self.assertIn("position_limit", codes(self.check(acct=dataclasses.replace(acct, high_vol_symbols=frozenset({"AAPL"})))))
        self.assertIn("position_limit", codes(self.check(acct=dataclasses.replace(acct, earnings_tomorrow=frozenset({"AAPL"})))))

    def test_position_limit_sums_both_books(self):
        acct = account(holdings=(Holding("AAPL", D("40"), D("200")), Holding("AAPL", D("35"), D("200"), book="trading")))
        self.assertIn("position_limit", codes(self.check(acct=acct, intent=buy(quantity=D("1")))))

    def test_equity_cap_applies_to_invest_book(self):
        self.assertIn("equity_cap", codes(self.check(acct=account(equity_cap_pct=D("18")))))

    def test_kill_switch_and_duplicates(self):
        self.assertIn("kill_switch", codes(self.check(acct=account(kill_switch_active=True))))
        self.assertIn("duplicate", codes(self.check(acct=account(seen_intent_ids=frozenset({"i-1"})))))

    def test_invalid_intent_short_circuits(self):
        self.assertEqual(codes(self.check(intent=buy(quantity=D("0")))), {"invalid_intent"})
        self.assertEqual(codes(self.check(q=quote(symbol="MSFT"))), {"invalid_intent"})


class TradingBookTests(unittest.TestCase):
    def tbuy(self, **over):
        base = dict(intent_id="t-1", book="trading", strategy="pullback", symbol="QQQ", side="buy",
                    quantity=D("3"), limit_price=D("500"), stop_price=D("480"))
        base.update(over)
        return OrderIntent(**base)

    def check(self, intent=None, acct=None):
        return guard.evaluate(intent or self.tbuy(), acct or account(), quote("QQQ", "499.9", "500.1"), POLICY, NOW)

    def test_sized_trade_is_allowed(self):
        self.assertTrue(self.check().allowed, self.check().blocks)

    def test_stop_and_trade_risk(self):
        self.assertIn("stop_required", codes(self.check(self.tbuy(stop_price=None))))
        self.assertIn("stop_required", codes(self.check(self.tbuy(stop_price=D("501")))))
        self.assertIn("trade_risk", codes(self.check(self.tbuy(quantity=D("6")))))  # 6×$20 = $120 > $100

    def test_strategy_and_book_loss_stops(self):
        self.assertIn("strategy_daily_loss", codes(self.check(acct=account(strategies={"pullback": StrategyState(daily_pnl_pct=D("-2"))}))))
        self.assertIn("strategy_monthly_loss", codes(self.check(acct=account(strategies={"pullback": StrategyState(monthly_pnl_pct=D("-6"))}))))
        self.assertIn("strategy_halted", codes(self.check(acct=account(strategies={"pullback": StrategyState(halted=True)}))))
        self.assertIn("book_daily_loss", codes(self.check(acct=account(trading_book_daily_pnl_pct=D("-3")))))
        self.assertIn("book_drawdown", codes(self.check(acct=account(trading_book_drawdown_pct=D("-15")))))
        self.assertIn("trading_book_unknown", codes(self.check(acct=account(trading_book_nav=None))))

    def test_book_rules_and_capital(self):
        self.assertIn("book_mismatch", codes(self.check(self.tbuy(strategy=None))))
        acct = account(holdings=(Holding("SPY", D("17"), D("560"), book="trading"),))
        self.assertIn("book_capital", codes(self.check(acct=acct)))

    def test_leveraged_products_only_in_trading_book_and_capped(self):
        lev_invest = guard.evaluate(buy(leverage=D("2")), account(), quote(), POLICY, NOW)
        self.assertIn("leverage_book", codes(lev_invest))
        acct = account(holdings=(Holding("TQQQ", D("50"), D("60"), book="trading", leverage=D("3")),))  # 9%
        self.assertIn("leveraged_exposure", codes(self.check(self.tbuy(leverage=D("3")), acct)))

    def test_size_trade_uses_one_percent_of_book(self):
        self.assertEqual(guard.size_trade(D("500"), D("480"), D("10000"), POLICY), D("5"))
        self.assertEqual(guard.size_trade(D("500"), D("480"), D("10000"), POLICY, step=D("0.1")), D("5.0"))
        with self.assertRaises(ValueError):
            guard.size_trade(D("500"), D("510"), D("10000"), POLICY)


class SellTests(unittest.TestCase):
    def sell(self, **over):
        base = dict(intent_id="s-1", book="invest", symbol="AAPL", side="sell", quantity=D("10"), limit_price=D("199.95"))
        base.update(over)
        return OrderIntent(**base)

    def test_risk_reducing_sell_ignores_buy_limits(self):
        acct = account(drawdown_pct=D("-20"), equity_cap_pct=None, cash=D("0"), complete=False)
        self.assertTrue(guard.evaluate(self.sell(), acct, quote(), POLICY, NOW).allowed)

    def test_no_short_and_no_cross_book_sell(self):
        self.assertIn("oversell", codes(guard.evaluate(self.sell(quantity=D("51")), account(), quote(), POLICY, NOW)))
        self.assertIn("oversell", codes(guard.evaluate(self.sell(book="trading", strategy="pullback"), account(), quote(), POLICY, NOW)))

    def test_sell_still_needs_fresh_data_and_respects_kill_switch(self):
        self.assertIn("stale_quote", codes(guard.evaluate(self.sell(), account(), quote(age=60), POLICY, NOW)))
        self.assertIn("stale_account", codes(guard.evaluate(self.sell(), account(as_of=NOW - timedelta(hours=1)), quote(), POLICY, NOW)))
        self.assertIn("kill_switch", codes(guard.evaluate(self.sell(), account(kill_switch_active=True), quote(), POLICY, NOW)))
        self.assertIn("price_through_book", codes(guard.evaluate(self.sell(limit_price=D("199.80")), account(), quote(), POLICY, NOW)))


class AlertTests(unittest.TestCase):
    def test_market_drop_sends_deepest_level_once_per_day(self):
        first = guard.check_alerts(POLICY, NOW, market_change_pct=D("-3.2"))
        self.assertEqual([(a.kind, a.severity) for a in first], [("MARKET_DROP", "warning")])
        deeper = guard.check_alerts(POLICY, NOW, market_change_pct=D("-5.5"))
        self.assertEqual(len(deeper), 1)
        self.assertEqual(deeper[0].severity, "critical")
        self.assertNotEqual(first[0].dedup_key, deeper[0].dedup_key)
        later = guard.check_alerts(POLICY, NOW + timedelta(hours=1), market_change_pct=D("-3.1"))
        self.assertEqual(later[0].dedup_key, first[0].dedup_key)
        self.assertEqual(guard.check_alerts(POLICY, NOW, market_change_pct=D("-2.9")), [])

    def test_other_alerts(self):
        alerts = guard.check_alerts(POLICY, NOW, account_daily_pct=D("-5"), holding_moves={"AAPL": D("7.5"), "MSFT": D("-3")},
                                    earnings_tomorrow=frozenset({"QCOM"}), heartbeat_at=NOW - timedelta(minutes=11))
        self.assertEqual([a.kind for a in alerts], ["ACCOUNT_DROP", "HOLDING_MOVE", "EARNINGS_TOMORROW", "HEARTBEAT_STALE"])
        self.assertEqual(alerts[0].severity, "critical")

    def test_alerts_and_blocks_become_outbox_events_without_account_details(self):
        from core.outbox import OutboxStore
        with tempfile.TemporaryDirectory() as tmp:
            store = OutboxStore(Path(tmp) / "core.db")
            alert = guard.check_alerts(POLICY, NOW, market_change_pct=D("-3.5"))[0]
            self.assertTrue(store.append(alert.to_event(NOW))["created"])
            self.assertFalse(store.append(alert.to_event(NOW + timedelta(minutes=5)))["created"])
            decision = guard.evaluate(buy(), account(kill_switch_active=True), quote(), POLICY, NOW)
            event = guard.decision_event(decision, buy(), NOW)
            self.assertEqual(event.category, "RISK")
            self.assertTrue(store.append(event)["created"])
            self.assertIsNone(guard.decision_event(guard.evaluate(buy(), account(), quote(), POLICY, NOW), buy(), NOW))


class CliTests(unittest.TestCase):
    def run_cli(self, argv, stdin=""):
        from core import cli
        with tempfile.TemporaryDirectory() as tmp, \
                patch("core.config._load_dotenv"), \
                patch.dict("os.environ", {"CORE_KILL_SWITCH_PATH": str(Path(tmp) / "ks.json")}), \
                patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(argv)
        return json.loads(out.getvalue())

    def test_policy_size_and_check(self):
        self.assertEqual(self.run_cli(["guard", "policy"])["version"], "v1")
        self.assertEqual(self.run_cli(["guard", "size", "--entry", "500", "--stop", "480", "--book-nav", "10000"])["quantity"], "5")
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "intent": {"intent_id": "x", "book": "invest", "symbol": "aapl", "side": "buy", "quantity": "1", "limit_price": "200"},
            "account": {"as_of": now, "nav": "100000", "cash": "50000", "drawdown_pct": "-1", "equity_cap_pct": "70"},
            "quote": {"symbol": "AAPL", "bid": "199.9", "ask": "200.1", "as_of": now},
        }
        self.assertEqual(self.run_cli(["guard", "check", "-"], json.dumps(payload))["decision"], "ALLOW")


if __name__ == "__main__":
    unittest.main()
