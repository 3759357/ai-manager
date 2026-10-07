import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from ki.client import KISError, KISOutcomeUnknown
from ki.config import Settings
from ki.gateway import KISGateway
from ki.models import Fill, make_order_intent
from ki.order_state import OrderStateError
from ki.risk import RiskEngine
from ki.storage import SnapshotStore


def settings(**kwargs) -> Settings:
    defaults = {
        "environment": "real",
        "app_key": "key",
        "app_secret": "secret",
        "account_no": "12345678",
        "max_overseas_order_amount_usd": Decimal("500"),
    }
    defaults.update(kwargs)
    return Settings(**defaults)


class GatewayTest(unittest.TestCase):
    def test_gateway_records_allowed_dry_run_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "gateway.db"
            gateway = KISGateway(settings(), SnapshotStore(database_path))
            result = gateway.create_order_intent(
                side="buy",
                symbol="AAPL",
                quantity=1,
                price="250",
            )

            with sqlite3.connect(database_path) as connection:
                intent_row = connection.execute(
                    "SELECT symbol, side, status FROM order_intents"
                ).fetchone()
                decision_row = connection.execute(
                    "SELECT decision, reasons_json FROM risk_decisions"
                ).fetchone()
                order_row = connection.execute(
                    "SELECT symbol, side, status FROM broker_orders"
                ).fetchone()
                event_row = connection.execute(
                    "SELECT event_type, status FROM order_events"
                ).fetchone()

        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["risk_decision"]["decision"], "ALLOW")
        self.assertEqual(result["order"]["status"], "RISK_APPROVED")
        self.assertEqual(intent_row, ("AAPL", "buy", "DRY_RUN_READY"))
        self.assertEqual(decision_row[0], "ALLOW")
        self.assertEqual(json.loads(decision_row[1]), [])
        self.assertEqual(order_row, ("AAPL", "buy", "RISK_APPROVED"))
        self.assertEqual(event_row, ("DRY_RUN_CREATED", "RISK_APPROVED"))

    def test_blocked_intent_does_not_create_broker_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "gateway.db"
            gateway = KISGateway(settings(), SnapshotStore(database_path))
            result = gateway.create_order_intent(
                side="buy",
                symbol="AAPL",
                quantity=3,
                price="250",
            )

            with sqlite3.connect(database_path) as connection:
                order_count = connection.execute(
                    "SELECT COUNT(*) FROM broker_orders"
                ).fetchone()[0]

        self.assertEqual(result["risk_decision"]["decision"], "BLOCK")
        self.assertIsNone(result["order"])
        self.assertEqual(order_count, 0)

    def test_store_lists_orders_and_fills(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "gateway.db"
            store = SnapshotStore(database_path)
            gateway = KISGateway(settings(), store)
            result = gateway.create_order_intent(
                side="buy",
                symbol="AAPL",
                quantity=1,
                price="250",
            )
            fill_id = store.save_fill(
                Fill(
                    order_id=result["order"]["id"],
                    symbol="AAPL",
                    side="buy",
                    quantity=1,
                    price=Decimal("250"),
                    fee=Decimal("0.5"),
                    raw={"source": "test"},
                )
            )
            orders = store.list_broker_orders()
            fills = store.list_fills()

        self.assertEqual(orders[0]["symbol"], "AAPL")
        self.assertEqual(orders[0]["status"], "RISK_APPROVED")
        self.assertEqual(fills[0]["id"], fill_id)
        self.assertEqual(fills[0]["raw"], {"source": "test"})

    def test_gateway_checks_buying_power_before_creating_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "gateway.db"
            client = FakeBuyingPowerClient(max_quantity="0")
            gateway = KISGateway(settings(), SnapshotStore(database_path))
            result = gateway.create_order_intent(
                side="buy",
                symbol="AAPL",
                quantity=1,
                price="250",
                client=client,
                check_buying_power=True,
            )

            with sqlite3.connect(database_path) as connection:
                order_count = connection.execute(
                    "SELECT COUNT(*) FROM broker_orders"
                ).fetchone()[0]

        self.assertEqual(client.calls[0], ("AAPL", "250", "NASD"))
        self.assertEqual(result["risk_decision"]["decision"], "BLOCK")
        self.assertEqual(
            result["risk_decision"]["checks"]["max_orderable_quantity"],
            0,
        )
        self.assertIsNone(result["order"])
        self.assertEqual(order_count, 0)

    def test_gateway_checks_position_before_creating_sell_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "gateway.db"
            client = FakePositionClient(available_quantity="0")
            gateway = KISGateway(settings(), SnapshotStore(database_path))
            result = gateway.create_order_intent(
                side="sell",
                symbol="AAPL",
                quantity=1,
                price="250",
                client=client,
                check_position=True,
            )

            with sqlite3.connect(database_path) as connection:
                order_count = connection.execute(
                    "SELECT COUNT(*) FROM broker_orders"
                ).fetchone()[0]

        self.assertEqual(client.calls[0], ("NASD", "USD"))
        self.assertEqual(result["risk_decision"]["decision"], "BLOCK")
        self.assertEqual(
            result["risk_decision"]["checks"]["sellable_quantity"],
            0,
        )
        self.assertIsNone(result["order"])
        self.assertEqual(order_count, 0)

    def test_gateway_allows_sell_order_when_position_is_enough(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "gateway.db"
            client = FakePositionClient(available_quantity="3")
            gateway = KISGateway(settings(), SnapshotStore(database_path))
            result = gateway.create_order_intent(
                side="sell",
                symbol="AAPL",
                quantity=1,
                price="250",
                client=client,
                check_position=True,
            )

        self.assertEqual(result["risk_decision"]["decision"], "ALLOW")
        self.assertEqual(result["order"]["status"], "RISK_APPROVED")
        self.assertEqual(result["position"]["ovrs_pdno"], "AAPL")

    def test_submit_order_requires_execute_permission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "gateway.db"
            gateway = KISGateway(settings(), SnapshotStore(database_path))
            result = gateway.create_order_intent(
                side="buy",
                symbol="AAPL",
                quantity=1,
                price="250",
            )

            with self.assertRaises(PermissionError):
                gateway.submit_order(
                    order_id=result["order"]["id"],
                    client=FakeOrderClient(),
                    execute=False,
                )

    def test_submit_order_updates_ledger_with_kis_order_id(self) -> None:
        live_settings = settings(
            allow_orders=True,
            real_trading_ack="I_UNDERSTAND_REAL_TRADING",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "gateway.db"
            store = SnapshotStore(database_path)
            gateway = KISGateway(live_settings, store)
            result = gateway.create_order_intent(
                side="buy",
                symbol="AAPL",
                quantity=1,
                price="250",
            )
            client = FakeOrderClient()
            submit_result = gateway.submit_order(
                order_id=result["order"]["id"],
                client=client,
                execute=True,
            )
            order = store.get_broker_order(result["order"]["id"])

        self.assertEqual(client.calls[0], ("buying_power", "AAPL", "250", "NASD"))
        self.assertEqual(client.calls[1]["symbol"], "AAPL")
        self.assertEqual(submit_result["status"], "ACCEPTED")
        self.assertEqual(submit_result["kis_order_id"], "KIS123")
        self.assertIn("preflight_event_id", submit_result)
        self.assertEqual(order["status"], "ACCEPTED")
        self.assertEqual(order["kis_order_id"], "KIS123")

    def test_submit_order_blocks_buy_when_preflight_buying_power_is_short(self) -> None:
        live_settings = settings(
            allow_orders=True,
            real_trading_ack="I_UNDERSTAND_REAL_TRADING",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "gateway.db"
            store = SnapshotStore(database_path)
            gateway = KISGateway(live_settings, store)
            result = gateway.create_order_intent(
                side="buy",
                symbol="AAPL",
                quantity=1,
                price="250",
            )
            client = FakeOrderClient(max_buy_quantity="0")
            submit_result = gateway.submit_order(
                order_id=result["order"]["id"],
                client=client,
                execute=True,
            )
            order = store.get_broker_order(result["order"]["id"])

            with sqlite3.connect(database_path) as connection:
                event_row = connection.execute(
                    """
                    SELECT event_type, status
                    FROM order_events
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()

        self.assertEqual(client.calls[0], ("buying_power", "AAPL", "250", "NASD"))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(submit_result["mode"], "blocked")
        self.assertEqual(submit_result["status"], "PREFLIGHT_BLOCKED")
        self.assertEqual(order["status"], "PREFLIGHT_BLOCKED")
        self.assertEqual(event_row, ("SUBMIT_PREFLIGHT_BLOCKED", "PREFLIGHT_BLOCKED"))

    def test_submit_order_blocks_invalid_state(self) -> None:
        live_settings = settings(
            allow_orders=True,
            real_trading_ack="I_UNDERSTAND_REAL_TRADING",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "gateway.db"
            store = SnapshotStore(database_path)
            gateway = KISGateway(live_settings, store)
            result = gateway.create_order_intent(
                side="buy",
                symbol="AAPL",
                quantity=1,
                price="250",
            )
            store.update_broker_order_status(
                result["order"]["id"],
                "PREFLIGHT_BLOCKED",
            )

            with self.assertRaises(OrderStateError):
                gateway.submit_order(
                    order_id=result["order"]["id"],
                    client=FakeOrderClient(),
                    execute=True,
                )

    def test_submit_order_blocks_sell_when_preflight_position_is_short(self) -> None:
        live_settings = settings(
            allow_orders=True,
            real_trading_ack="I_UNDERSTAND_REAL_TRADING",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "gateway.db"
            store = SnapshotStore(database_path)
            gateway = KISGateway(live_settings, store)
            result = gateway.create_order_intent(
                side="sell",
                symbol="AAPL",
                quantity=1,
                price="250",
            )
            client = FakeOrderClient(available_quantity="0")
            submit_result = gateway.submit_order(
                order_id=result["order"]["id"],
                client=client,
                execute=True,
            )
            order = store.get_broker_order(result["order"]["id"])

        self.assertEqual(client.calls[0], ("balance", "NASD", "USD"))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(submit_result["mode"], "blocked")
        self.assertEqual(
            submit_result["risk_decision"]["checks"]["sellable_quantity"],
            0,
        )
        self.assertEqual(order["status"], "PREFLIGHT_BLOCKED")

    def test_cancel_order_requires_execute_permission(self) -> None:
        live_settings = settings(
            allow_orders=True,
            real_trading_ack="I_UNDERSTAND_REAL_TRADING",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "gateway.db"
            store = SnapshotStore(database_path)
            gateway = KISGateway(live_settings, store)
            result = gateway.create_order_intent(
                side="buy",
                symbol="AAPL",
                quantity=1,
                price="250",
            )
            store.mark_broker_order_submitted(
                result["order"]["id"],
                status="ACCEPTED",
                kis_order_id="KIS123",
            )

            with self.assertRaises(PermissionError):
                gateway.cancel_order(
                    order_id=result["order"]["id"],
                    client=FakeOrderClient(),
                    execute=False,
                )

    def test_cancel_order_blocks_invalid_state(self) -> None:
        live_settings = settings(
            allow_orders=True,
            real_trading_ack="I_UNDERSTAND_REAL_TRADING",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "gateway.db"
            store = SnapshotStore(database_path)
            gateway = KISGateway(live_settings, store)
            result = gateway.create_order_intent(
                side="buy",
                symbol="AAPL",
                quantity=1,
                price="250",
            )

            with self.assertRaises(OrderStateError):
                gateway.cancel_order(
                    order_id=result["order"]["id"],
                    client=FakeOrderClient(),
                    execute=True,
                )

    def test_cancel_order_updates_ledger_and_records_event(self) -> None:
        live_settings = settings(
            allow_orders=True,
            real_trading_ack="I_UNDERSTAND_REAL_TRADING",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "gateway.db"
            store = SnapshotStore(database_path)
            gateway = KISGateway(live_settings, store)
            result = gateway.create_order_intent(
                side="buy",
                symbol="AAPL",
                quantity=2,
                price="250",
            )
            order_id = result["order"]["id"]
            store.mark_broker_order_submitted(
                order_id,
                status="ACCEPTED",
                kis_order_id="KIS123",
            )
            client = FakeOrderClient()
            cancel_result = gateway.cancel_order(
                order_id=order_id,
                client=client,
                execute=True,
                quantity=1,
            )
            order = store.get_broker_order(order_id)

            with sqlite3.connect(database_path) as connection:
                event_row = connection.execute(
                    """
                    SELECT event_type, status, payload_json
                    FROM order_events
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()

        self.assertEqual(
            client.calls[-1],
            ("cancel", "AAPL", "KIS123", 1, "NASD"),
        )
        self.assertEqual(cancel_result["status"], "CANCEL_REQUESTED")
        self.assertEqual(cancel_result["cancel_quantity"], 1)
        self.assertEqual(order["status"], "CANCEL_REQUESTED")
        self.assertEqual(event_row[0], "CANCEL_REQUESTED")
        self.assertEqual(event_row[1], "CANCEL_REQUESTED")
        self.assertEqual(json.loads(event_row[2])["original_kis_order_id"], "KIS123")

    def test_void_order_updates_local_ledger_without_kis_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "gateway.db"
            store = SnapshotStore(database_path)
            gateway = KISGateway(settings(), store)
            result = gateway.create_order_intent(
                side="buy",
                symbol="AAPL",
                quantity=1,
                price="250",
            )
            client = FakeOrderClient()
            void_result = gateway.void_order(
                order_id=result["order"]["id"],
                reason="stale quote",
            )
            order = store.get_broker_order(result["order"]["id"])

            with sqlite3.connect(database_path) as connection:
                event_row = connection.execute(
                    """
                    SELECT event_type, status, payload_json
                    FROM order_events
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()

        self.assertEqual(client.calls, [])
        self.assertEqual(void_result["status"], "VOIDED")
        self.assertEqual(order["status"], "VOIDED")
        self.assertEqual(event_row[0], "VOIDED")
        self.assertEqual(event_row[1], "VOIDED")
        self.assertEqual(json.loads(event_row[2])["reason"], "stale quote")

    def test_void_order_blocks_submitted_order(self) -> None:
        live_settings = settings(
            allow_orders=True,
            real_trading_ack="I_UNDERSTAND_REAL_TRADING",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "gateway.db"
            store = SnapshotStore(database_path)
            gateway = KISGateway(live_settings, store)
            result = gateway.create_order_intent(
                side="buy",
                symbol="AAPL",
                quantity=1,
                price="250",
            )
            store.mark_broker_order_submitted(
                result["order"]["id"],
                status="ACCEPTED",
                kis_order_id="KIS123",
            )

            with self.assertRaises(OrderStateError):
                gateway.void_order(
                    order_id=result["order"]["id"],
                    reason="already submitted",
                )

    def test_risk_blocks_market_order_and_high_amount(self) -> None:
        intent = make_order_intent(
            side="buy",
            symbol="AAPL",
            quantity=3,
            price="250",
            order_type="market",
        )
        decision = RiskEngine(settings()).evaluate(intent)

        self.assertEqual(decision.decision, "BLOCK")
        self.assertTrue(
            any("시장가 주문" in reason for reason in decision.reasons)
        )
        self.assertTrue(
            any("주문 한도" in reason for reason in decision.reasons)
        )

    def test_risk_blocks_when_kill_switch_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            kill_switch = Path(temp_dir) / "KILL_SWITCH"
            kill_switch.write_text("stop", encoding="utf-8")
            intent = make_order_intent(
                side="buy",
                symbol="AAPL",
                quantity=1,
                price="250",
            )
            decision = RiskEngine(
                settings(),
                kill_switch_path=kill_switch,
            ).evaluate(intent)

        self.assertEqual(decision.decision, "BLOCK")
        self.assertTrue(any("Kill Switch" in reason for reason in decision.reasons))

    def test_risk_blocks_buy_quantity_over_buying_power(self) -> None:
        intent = make_order_intent(
            side="buy",
            symbol="AAPL",
            quantity=2,
            price="100",
        )
        decision = RiskEngine(settings()).evaluate(
            intent,
            buying_power={"max_ord_psbl_qty": "1"},
        )

        self.assertEqual(decision.decision, "BLOCK")
        self.assertTrue(
            any("매수가능수량" in reason for reason in decision.reasons)
        )

    def test_risk_blocks_sell_quantity_over_position(self) -> None:
        intent = make_order_intent(
            side="sell",
            symbol="AAPL",
            quantity=2,
            price="100",
        )
        decision = RiskEngine(settings()).evaluate(
            intent,
            position={"ovrs_pdno": "AAPL", "ord_psbl_qty": "1"},
        )

        self.assertEqual(decision.decision, "BLOCK")
        self.assertTrue(
            any("매도가능수량" in reason for reason in decision.reasons)
        )


class SubmitUnknownTest(unittest.TestCase):
    def _setup(self, temp_dir, failure):
        store = SnapshotStore(Path(temp_dir) / "gateway.db")
        gateway = KISGateway(settings(allow_orders=True, real_trading_ack="I_UNDERSTAND_REAL_TRADING"), store)
        order_id = gateway.create_order_intent(side="buy", symbol="AAPL", quantity=1, price="250")["order"]["id"]
        client = FakeOrderClient()
        client.order_failure = failure
        return store, gateway, order_id, client

    def test_unknown_outcome_blocks_resubmission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store, gateway, order_id, client = self._setup(temp_dir, KISOutcomeUnknown("timeout"))
            result = gateway.submit_order(order_id=order_id, client=client, execute=True)
            self.assertEqual(result["status"], "SUBMIT_UNKNOWN")
            client.order_failure = None
            with self.assertRaises(OrderStateError):
                gateway.submit_order(order_id=order_id, client=client, execute=True)
            orders_sent = [c for c in client.calls if isinstance(c, dict)]
            self.assertEqual(len(orders_sent), 1)
            self.assertEqual(store.get_broker_order(order_id)["status"], "SUBMIT_UNKNOWN")

    def test_crash_during_send_leaves_submitting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store, gateway, order_id, client = self._setup(temp_dir, KeyboardInterrupt())
            with self.assertRaises(KeyboardInterrupt):
                gateway.submit_order(order_id=order_id, client=client, execute=True)
            self.assertEqual(store.get_broker_order(order_id)["status"], "SUBMITTING")
            client.order_failure = None
            with self.assertRaises(OrderStateError):
                gateway.submit_order(order_id=order_id, client=client, execute=True)

    def test_rejection_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store, gateway, order_id, client = self._setup(temp_dir, KISError("APBK0013: 주문가능금액 부족"))
            with self.assertRaises(KISError):
                gateway.submit_order(order_id=order_id, client=client, execute=True)
            self.assertEqual(store.get_broker_order(order_id)["status"], "REJECTED")

    def test_unknown_order_voids_only_with_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store, gateway, order_id, client = self._setup(temp_dir, KISOutcomeUnknown("timeout"))
            gateway.submit_order(order_id=order_id, client=client, execute=True)
            with self.assertRaises(ValueError):
                gateway.void_order(order_id=order_id)
            result = gateway.void_order(order_id=order_id, confirm_not_placed=True, reason="KIS 앱 확인")
            self.assertEqual(result["status"], "VOIDED")


class FakeOrderClient:
    def __init__(
        self,
        max_buy_quantity: str = "10",
        available_quantity: str = "10",
    ) -> None:
        self.max_buy_quantity = max_buy_quantity
        self.available_quantity = available_quantity
        self.calls = []
        self.order_failure: BaseException | None = None

    def overseas_order(self, **kwargs):
        self.calls.append(kwargs)
        if self.order_failure is not None:
            raise self.order_failure
        return {"ODNO": "KIS123"}

    def overseas_buying_power(self, symbol: str, price: str, exchange: str):
        self.calls.append(("buying_power", symbol, price, exchange))
        return {"max_ord_psbl_qty": self.max_buy_quantity}

    def overseas_balance(self, exchange: str, currency: str):
        self.calls.append(("balance", exchange, currency))
        return {
            "positions": [
                {
                    "ovrs_pdno": "AAPL",
                    "ord_psbl_qty": self.available_quantity,
                    "ovrs_cblc_qty": self.available_quantity,
                }
            ],
            "summary": [],
        }

    def overseas_cancel_order(
        self,
        *,
        symbol: str,
        original_order_id: str,
        quantity: int,
        exchange: str,
    ):
        self.calls.append(("cancel", symbol, original_order_id, quantity, exchange))
        return {"ODNO": "CANCEL123"}


class FakeBuyingPowerClient:
    def __init__(self, max_quantity: str) -> None:
        self.max_quantity = max_quantity
        self.calls = []

    def overseas_buying_power(self, symbol: str, price: str, exchange: str):
        self.calls.append((symbol, price, exchange))
        return {"max_ord_psbl_qty": self.max_quantity}


class FakePositionClient:
    def __init__(self, available_quantity: str) -> None:
        self.available_quantity = available_quantity
        self.calls = []

    def overseas_balance(self, exchange: str, currency: str):
        self.calls.append((exchange, currency))
        return {
            "positions": [
                {
                    "ovrs_pdno": "AAPL",
                    "ord_psbl_qty": self.available_quantity,
                    "ovrs_cblc_qty": self.available_quantity,
                }
            ],
            "summary": [],
        }
