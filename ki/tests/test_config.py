import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ki.config import REAL_TRADING_ACK, Settings


class SettingsTest(unittest.TestCase):
    def test_real_order_requires_explicit_ack(self) -> None:
        settings = Settings(
            environment="real",
            app_key="key",
            app_secret="secret",
            account_no="12345678",
            allow_orders=True,
        )
        with self.assertRaises(PermissionError):
            settings.require_order_permission(execute=True)

    def test_real_order_is_allowed_with_all_guards(self) -> None:
        settings = Settings(
            environment="real",
            app_key="key",
            app_secret="secret",
            account_no="12345678",
            allow_orders=True,
            real_trading_ack=REAL_TRADING_ACK,
        )
        settings.require_order_permission(execute=True)

    def test_from_env_defaults_to_demo(self) -> None:
        env = {
            "KIS_APP_KEY": "key",
            "KIS_APP_SECRET": "secret",
            "KIS_ACCOUNT_NO": "12345678",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env(Path("does-not-exist.env"))
        self.assertEqual(settings.environment, "demo")
        self.assertFalse(settings.allow_orders)

    def test_default_env_can_use_central_ai_manager_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "KIS_APP_KEY=central-key\n"
                "KIS_APP_SECRET=central-secret\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"AI_MANAGER_ENV_PATH": str(env_path)},
                clear=True,
            ):
                settings = Settings.from_env()
        self.assertEqual(settings.app_key, "central-key")
