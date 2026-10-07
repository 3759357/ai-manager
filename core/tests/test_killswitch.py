import tempfile
import unittest
from pathlib import Path

from core.killswitch import KillSwitch


class KillSwitchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.switch = KillSwitch(Path(self._tmp.name) / "kill-switch.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_inactive_by_default(self) -> None:
        self.assertEqual(self.switch.status(), {"active": False})
        self.switch.assert_allows_orders()

    def test_activate_blocks_orders_with_reason(self) -> None:
        status = self.switch.activate("폭락장 수동 차단")

        self.assertTrue(status["active"])
        self.assertEqual(status["reason"], "폭락장 수동 차단")
        self.assertTrue(status["activated_at"])
        with self.assertRaises(PermissionError) as ctx:
            self.switch.assert_allows_orders()
        self.assertIn("폭락장 수동 차단", str(ctx.exception))

    def test_deactivate_restores_orders(self) -> None:
        self.switch.activate("test")

        status = self.switch.deactivate()

        self.assertEqual(status, {"active": False})
        self.switch.assert_allows_orders()

    def test_corrupt_file_is_conservatively_active(self) -> None:
        self.switch.path.parent.mkdir(parents=True, exist_ok=True)
        self.switch.path.write_text("{깨진 json", encoding="utf-8")

        status = self.switch.status()

        self.assertTrue(status["active"])
        with self.assertRaises(PermissionError):
            self.switch.assert_allows_orders()

    def test_activate_is_idempotent(self) -> None:
        self.switch.activate("first")
        status = self.switch.activate("second")

        self.assertTrue(status["active"])
        self.assertEqual(status["reason"], "second")


if __name__ == "__main__":
    unittest.main()
