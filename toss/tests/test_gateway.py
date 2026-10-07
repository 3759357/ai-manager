import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from toss.client import TossError, TossOutcomeUnknown
from toss.config import REAL_TRADING_ACK, Settings
from toss.gateway import TossGateway
from toss.storage import OrderStore


def settings(**kwargs) -> Settings:
    defaults = {
        "client_id": "client",
        "client_secret": "secret",
        "account_seq": 1,
        "max_order_amount_usd": Decimal("500"),
        "max_order_amount_krw": Decimal("500000"),
        "max_slippage_pct": Decimal("0.10"),
    }
    defaults.update(kwargs)
    return Settings(**defaults)


class TossGatewayTest(unittest.TestCase):
    def test_buy_candidate_checks_buying_power_and_creates_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = OrderStore(Path(temp_dir) / "toss.db")
            client = FakeClient()
            gateway = TossGateway(settings(), client, store)

            result = gateway.create_order_candidate(
                side="buy",
                symbol="AAPL",
                quantity=1,
                price="200.10",
                currency="USD",
            )
            order = store.get_order(result["order_id"])

        self.assertEqual(result["status"], "RISK_APPROVED")
        self.assertEqual(order["status"], "RISK_APPROVED")
        self.assertEqual(order["payload"]["side"], "BUY")
        self.assertTrue(order["payload"]["clientOrderId"].startswith("tp-"))
        self.assertEqual(client.buying_power_calls, ["USD"])

    def test_candidate_is_blocked_when_amount_exceeds_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = OrderStore(Path(temp_dir) / "toss.db")
            gateway = TossGateway(settings(), FakeClient(), store)

            result = gateway.create_order_candidate(
                side="buy",
                symbol="AAPL",
                quantity=3,
                price="200",
                currency="USD",
            )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("주문 한도", result["preflight"]["reasons"][0])

    def test_sell_candidate_checks_sellable_quantity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = OrderStore(Path(temp_dir) / "toss.db")
            client = FakeClient(sellable="0")
            gateway = TossGateway(settings(), client, store)

            result = gateway.create_order_candidate(
                side="sell",
                symbol="AAPL",
                quantity=1,
                price="200",
                currency="USD",
            )

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(client.sellable_calls, ["AAPL"])

    def test_submit_requires_explicit_permission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = OrderStore(Path(temp_dir) / "toss.db")
            client = FakeClient()
            gateway = TossGateway(settings(), client, store)
            candidate = gateway.create_order_candidate(
                side="buy",
                symbol="AAPL",
                quantity=1,
                price="200",
                currency="USD",
            )

            with self.assertRaises(PermissionError):
                gateway.submit_order(candidate["order_id"], execute=True)

        self.assertEqual(client.created_orders, [])

    def test_submit_rechecks_and_sends_idempotent_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = OrderStore(Path(temp_dir) / "toss.db")
            client = FakeClient()
            live_settings = settings(
                allow_orders=True,
                real_trading_ack=REAL_TRADING_ACK,
            )
            gateway = TossGateway(live_settings, client, store)
            candidate = gateway.create_order_candidate(
                side="buy",
                symbol="AAPL",
                quantity=1,
                price="200",
                currency="USD",
            )

            result = gateway.submit_order(candidate["order_id"], execute=True)
            order = store.get_order(candidate["order_id"])

        self.assertEqual(result["status"], "SUBMITTED")
        self.assertEqual(order["toss_order_id"], "TOSS-1")
        self.assertEqual(client.created_orders[0]["orderType"], "LIMIT")
        self.assertTrue(client.created_orders[0]["clientOrderId"].startswith("tp-"))
        self.assertEqual(client.buying_power_calls, ["USD", "USD"])
        self.assertEqual(client.orderbook_calls, ["AAPL"])

    def test_submit_blocks_when_toss_quote_deviates_from_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = OrderStore(Path(temp_dir) / "toss.db")
            client = FakeClient(ask_price="205")
            live_settings = settings(
                allow_orders=True,
                real_trading_ack=REAL_TRADING_ACK,
            )
            gateway = TossGateway(live_settings, client, store)
            candidate = gateway.create_order_candidate(
                side="buy",
                symbol="AAPL",
                quantity=1,
                price="200",
                currency="USD",
            )

            result = gateway.submit_order(candidate["order_id"], execute=True)

        self.assertEqual(result["status"], "PREFLIGHT_BLOCKED")
        self.assertEqual(client.created_orders, [])

    def test_sync_order_updates_local_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = OrderStore(Path(temp_dir) / "toss.db")
            client = FakeClient()
            live_settings = settings(
                allow_orders=True,
                real_trading_ack=REAL_TRADING_ACK,
            )
            gateway = TossGateway(live_settings, client, store)
            candidate = gateway.create_order_candidate(
                side="buy",
                symbol="AAPL",
                quantity=1,
                price="200",
                currency="USD",
            )
            gateway.submit_order(candidate["order_id"], execute=True)

            result = gateway.sync_order(candidate["order_id"])
            order = store.get_order(candidate["order_id"])

        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(order["status"], "FILLED")


class SubmitRecoveryTest(unittest.TestCase):
    """결과 불명 주문은 재제출되지 않고 조회로만 복구된다."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = OrderStore(Path(self.temp.name) / "toss.db")
        self.client = FakeClient()
        self.gateway = TossGateway(settings(allow_orders=True, real_trading_ack=REAL_TRADING_ACK), self.client, self.store)
        self.order_id = self.gateway.create_order_candidate(side="buy", symbol="AAPL", quantity=1, price="200", currency="USD")["order_id"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _age_submitting(self, minutes: int) -> None:
        old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        with sqlite3.connect(self.store.database_path) as db:
            db.execute("UPDATE order_events SET created_at=? WHERE event_type='SUBMITTING'", (old,))

    def _submit_unknown(self) -> dict:
        self.client.create_error = TossOutcomeUnknown("timeout")
        return self.gateway.submit_order(self.order_id, execute=True)

    def test_unknown_outcome_blocks_resubmission(self) -> None:
        result = self._submit_unknown()
        self.assertEqual(result["status"], "SUBMIT_UNKNOWN")
        self.client.create_error = None
        with self.assertRaises(ValueError):
            self.gateway.submit_order(self.order_id, execute=True)
        self.assertEqual(len(self.client.created_orders), 1)

    def test_crash_during_send_leaves_order_unsubmittable(self) -> None:
        self.client.create_error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.gateway.submit_order(self.order_id, execute=True)
        self.assertEqual(self.store.get_order(self.order_id)["status"], "SUBMITTING")
        self.client.create_error = None
        with self.assertRaises(ValueError):
            self.gateway.submit_order(self.order_id, execute=True)
        self.assertEqual(len(self.client.created_orders), 1)

    def test_sync_recovers_by_client_order_id(self) -> None:
        self._submit_unknown()
        client_id = self.store.get_order(self.order_id)["client_order_id"]
        self.client.remote_orders = [
            {"orderId": "OTHER", "clientOrderId": "tp-other", "symbol": "AAPL", "side": "BUY", "quantity": "1", "price": "200"},
            {"orderId": "TOSS-9", "clientOrderId": client_id, "symbol": "AAPL", "side": "BUY", "quantity": "1"},
        ]
        result = self.gateway.sync_order(self.order_id)
        order = self.store.get_order(self.order_id)
        self.assertEqual(order["toss_order_id"], "TOSS-9")
        self.assertEqual(result["status"], "FILLED")

    def test_sync_recovers_single_field_match_after_submit_time(self) -> None:
        self._submit_unknown()
        now = datetime.now(timezone.utc).isoformat()
        self.client.remote_orders = [{"orderId": "TOSS-7", "symbol": "AAPL", "side": "BUY", "quantity": "1", "price": "200.00", "orderedAt": now}]
        self.gateway.sync_order(self.order_id)
        self.assertEqual(self.store.get_order(self.order_id)["toss_order_id"], "TOSS-7")

    def test_ambiguous_matches_are_not_linked(self) -> None:
        self._submit_unknown()
        now = datetime.now(timezone.utc).isoformat()
        row = {"symbol": "AAPL", "side": "BUY", "quantity": "1", "price": "200", "orderedAt": now}
        self.client.remote_orders = [dict(row, orderId="A"), dict(row, orderId="B")]
        result = self.gateway.sync_order(self.order_id)
        self.assertFalse(result["recovered"])
        self.assertEqual(self.store.get_order(self.order_id)["status"], "SUBMIT_UNKNOWN")
        self._age_submitting(5)
        with self.assertRaises(ValueError):
            self.gateway.resolve_not_placed(self.order_id)

    def test_old_identical_order_is_not_adopted(self) -> None:
        self._submit_unknown()
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        self.client.remote_orders = [{"orderId": "OLD", "symbol": "AAPL", "side": "BUY", "quantity": "1", "price": "200", "orderedAt": old}]
        result = self.gateway.sync_order(self.order_id)
        self.assertFalse(result["recovered"])

    def test_not_found_can_be_resolved_only_after_wait(self) -> None:
        self._submit_unknown()
        self.client.remote_orders = []
        with self.assertRaises(ValueError):
            self.gateway.resolve_not_placed(self.order_id)
        self._age_submitting(3)
        result = self.gateway.resolve_not_placed(self.order_id)
        self.assertEqual(result["status"], "SUBMIT_FAILED")
        with self.assertRaises(ValueError):
            self.gateway.submit_order(self.order_id, execute=True)

    def test_definite_rejection_is_recorded(self) -> None:
        self.client.create_error = TossError("잔고 부족", status_code=422, code="insufficient-buying-power")
        with self.assertRaises(TossError):
            self.gateway.submit_order(self.order_id, execute=True)
        self.assertEqual(self.store.get_order(self.order_id)["status"], "SUBMIT_REJECTED")

    def test_missing_order_id_is_unknown(self) -> None:
        self.client.create_response = {}
        result = self.gateway.submit_order(self.order_id, execute=True)
        self.assertEqual(result["status"], "SUBMIT_UNKNOWN")

    def test_stale_candidate_expires(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with sqlite3.connect(self.store.database_path) as db:
            db.execute("UPDATE orders SET created_at=? WHERE id=?", (old, self.order_id))
        with self.assertRaises(ValueError):
            self.gateway.submit_order(self.order_id, execute=True)
        self.assertEqual(self.store.get_order(self.order_id)["status"], "EXPIRED")
        self.assertEqual(self.client.created_orders, [])

    def test_cancel_unknown_keeps_status_and_reports(self) -> None:
        self.gateway.submit_order(self.order_id, execute=True)
        self.client.cancel_error = TossOutcomeUnknown("timeout")
        result = self.gateway.cancel_order(self.order_id, execute=True)
        self.assertEqual(result["mode"], "unknown")
        self.assertEqual(self.store.get_order(self.order_id)["status"], "SUBMITTED")


class FakeClient:
    def __init__(
        self,
        *,
        buying_power: str = "1000",
        sellable: str = "10",
        ask_price: str = "200.10",
        bid_price: str = "199.90",
    ) -> None:
        self.buying_power_value = buying_power
        self.sellable_value = sellable
        self.ask_price = ask_price
        self.bid_price = bid_price
        self.buying_power_calls: list[str] = []
        self.sellable_calls: list[str] = []
        self.orderbook_calls: list[str] = []
        self.created_orders: list[dict] = []
        self.create_error: BaseException | None = None
        self.create_response: dict | None = None
        self.cancel_error: BaseException | None = None
        self.remote_orders: list[dict] = []

    def buying_power(self, currency: str):
        self.buying_power_calls.append(currency)
        return {"currency": currency, "cashBuyingPower": self.buying_power_value}

    def sellable_quantity(self, symbol: str):
        self.sellable_calls.append(symbol)
        return {"sellableQuantity": self.sellable_value}

    def create_order(self, payload: dict):
        self.created_orders.append(payload)
        if self.create_error is not None:
            raise self.create_error
        if self.create_response is not None:
            return self.create_response
        return {"orderId": "TOSS-1", "clientOrderId": payload["clientOrderId"]}

    def orders(self, *, status: str = "OPEN", symbol: str | None = None, limit: int = 20):
        return {"orders": list(self.remote_orders) if status == "OPEN" else []}

    def orderbook(self, symbol: str):
        self.orderbook_calls.append(symbol)
        return {
            "currency": "USD",
            "asks": [{"price": self.ask_price, "volume": "10"}],
            "bids": [{"price": self.bid_price, "volume": "10"}],
        }

    def cancel_order(self, order_id: str):
        if self.cancel_error is not None:
            raise self.cancel_error
        return {"orderId": order_id}

    def modify_order(self, order_id: str, payload: dict):
        return {"orderId": order_id}

    def order(self, order_id: str):
        return {"orderId": order_id, "status": "FILLED"}


if __name__ == "__main__":
    unittest.main()
