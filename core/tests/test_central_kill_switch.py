"""core kill-switch 파일 하나로 KIS·토스 제출 경로가 모두 멈추는지 확인한다."""
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from ki.config import Settings as KISSettings
from ki.gateway import KISGateway
from ki.storage import SnapshotStore
from toss.config import REAL_TRADING_ACK as TOSS_ACK
from toss.config import Settings as TossSettings
from toss.gateway import TossGateway
from toss.storage import OrderStore

from core.config import Settings as CoreSettings
from core.killswitch import KillSwitch


class FakeKISClient:
    def __init__(self) -> None:
        self.orders: list[dict] = []

    def overseas_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"ODNO": "KIS1"}

    def overseas_buying_power(self, symbol: str, price: str, exchange: str):
        return {"max_ord_psbl_qty": "10"}


class FakeTossClient:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.modified: list[str] = []

    def buying_power(self, currency: str):
        return {"currency": currency, "cashBuyingPower": "1000"}

    def orderbook(self, symbol: str):
        return {"asks": [{"price": "200"}], "bids": [{"price": "199.9"}]}

    def create_order(self, payload: dict):
        self.created.append(payload)
        return {"orderId": "T1"}

    def modify_order(self, order_id: str, payload: dict):
        self.modified.append(order_id)
        return {"orderId": order_id}


class CentralKillSwitchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.switch_path = self.root / "core" / "data" / "kill-switch.json"
        self.switch = KillSwitch(self.switch_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def kis(self):
        settings = KISSettings(
            environment="demo", app_key="k", app_secret="s", account_no="12345678",
            allow_orders=True, database_path=self.root / "ki" / "ki.db",
            kill_switch_path=self.switch_path,
        )
        store = SnapshotStore(settings.database_path)
        return KISGateway(settings, store), store, FakeKISClient()

    def toss(self):
        settings = TossSettings(
            client_id="c", client_secret="s", account_seq=1, allow_orders=True,
            real_trading_ack=TOSS_ACK, max_order_amount_usd=Decimal("500"),
            database_path=self.root / "toss" / "toss.db", kill_switch_path=self.switch_path,
        )
        store = OrderStore(settings.database_path)
        client = FakeTossClient()
        return TossGateway(settings, client, store), store, client

    def test_kis_submit_is_blocked_without_touching_the_ledger(self) -> None:
        gateway, store, client = self.kis()
        created = gateway.create_order_intent(side="buy", symbol="AAPL", quantity=1, price="200",
                                              client=client, check_buying_power=True)
        order_id = created["order"]["id"]
        self.switch.activate("점검")

        with self.assertRaises(PermissionError) as caught:
            gateway.submit_order(order_id=order_id, client=client, execute=True)

        self.assertIn("점검", str(caught.exception))
        self.assertEqual(store.get_broker_order(order_id)["status"], "RISK_APPROVED")
        self.assertEqual(client.orders, [])

        self.switch.deactivate()
        result = gateway.submit_order(order_id=order_id, client=client, execute=True)
        self.assertEqual(result["status"], "ACCEPTED")

    def test_kis_new_candidates_are_blocked(self) -> None:
        gateway, _, client = self.kis()
        self.switch.activate("점검")
        result = gateway.create_order_intent(side="buy", symbol="AAPL", quantity=1, price="200")

        self.assertEqual(result["risk_decision"]["decision"], "BLOCK")
        self.assertIsNone(result["order"])

    def test_toss_submit_and_modify_are_blocked_but_cancel_is_not(self) -> None:
        gateway, store, client = self.toss()
        created = gateway.create_order_candidate(side="buy", symbol="AAPL", quantity=1,
                                                 price="200", currency="USD")
        self.switch.activate("점검")

        with self.assertRaises(PermissionError):
            gateway.submit_order(created["order_id"], execute=True)
        with self.assertRaises(PermissionError):
            gateway.modify_order(created["order_id"], price="199", execute=True)
        # 취소는 위험을 줄이므로 Kill Switch 대상이 아니다(여기서는 토스 주문번호가 없어 멈춘다).
        with self.assertRaises(ValueError) as cancel:
            gateway.cancel_order(created["order_id"], execute=True)

        self.assertIn("토스 주문번호", str(cancel.exception))
        self.assertEqual(store.get_order(created["order_id"])["status"], "RISK_APPROVED")
        self.assertEqual(client.created, [])
        self.assertEqual(client.modified, [])

    def test_toss_new_candidates_are_blocked(self) -> None:
        gateway, _, _ = self.toss()
        self.switch.activate()
        result = gateway.create_order_candidate(side="buy", symbol="AAPL", quantity=1,
                                                price="200", currency="USD")
        self.assertEqual(result["status"], "BLOCKED")

    def test_corrupt_switch_file_still_blocks(self) -> None:
        gateway, _, client = self.toss()
        self.switch_path.parent.mkdir(parents=True, exist_ok=True)
        self.switch_path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(PermissionError):
            gateway.submit_order(1, execute=True)

    def test_all_packages_resolve_the_same_default_path(self) -> None:
        env_file = self.root / "core" / ".env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text(
            "KIS_APP_KEY=k\nKIS_APP_SECRET=s\nTOSS_CLIENT_ID=c\nTOSS_CLIENT_SECRET=s\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            core_path = CoreSettings.from_env(env_file).kill_switch_path
            kis_path = KISSettings.from_env(env_file).kill_switch_path
            toss_path = TossSettings.from_env(env_file).kill_switch_path
        with mock.patch.dict(os.environ, {"CORE_KILL_SWITCH_PATH": "/srv/stop.json"}, clear=True):
            overridden = {
                CoreSettings.from_env(env_file).kill_switch_path,
                KISSettings.from_env(env_file).kill_switch_path,
                TossSettings.from_env(env_file).kill_switch_path,
            }

        self.assertEqual(core_path, (self.root / "core" / "data" / "kill-switch.json").resolve())
        self.assertEqual({core_path, kis_path, toss_path}, {core_path})
        self.assertEqual(overridden, {Path("/srv/stop.json")})


if __name__ == "__main__":
    unittest.main()
