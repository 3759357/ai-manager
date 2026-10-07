import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from toss.config import REAL_TRADING_ACK, Settings


class SettingsTest(unittest.TestCase):
    def test_live_order_requires_all_guards(self) -> None:
        settings = Settings(
            client_id="client",
            client_secret="secret",
            account_seq=1,
            allow_orders=True,
        )
        with self.assertRaises(PermissionError):
            settings.require_order_permission(execute=True)

    def test_live_order_allowed_with_ack(self) -> None:
        settings = Settings(
            client_id="client",
            client_secret="secret",
            account_seq=1,
            allow_orders=True,
            real_trading_ack=REAL_TRADING_ACK,
            max_order_amount_usd=Decimal("500"),
        )
        settings.require_order_permission(execute=True)

    def test_account_is_required_for_account_apis(self) -> None:
        settings = Settings(client_id="client", client_secret="secret")
        with self.assertRaises(ValueError):
            settings.require_account()

    def test_account_seq_is_loaded_as_numeric_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "TOSS_CLIENT_ID=client\n"
                "TOSS_CLIENT_SECRET=secret\n"
                "TOSS_ACCOUNT_SEQ=1\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                settings = Settings.from_env(env_path)
        self.assertEqual(settings.account_seq, 1)

    def test_default_env_can_use_central_ai_manager_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "TOSS_CLIENT_ID=central-client\n"
                "TOSS_CLIENT_SECRET=central-secret\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"AI_MANAGER_ENV_PATH": str(env_path)},
                clear=True,
            ):
                settings = Settings.from_env()
        self.assertEqual(settings.client_id, "central-client")


if __name__ == "__main__":
    unittest.main()
