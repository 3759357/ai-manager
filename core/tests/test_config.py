import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import Settings


class SettingsTest(unittest.TestCase):
    def test_core_env_contains_both_broker_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_APP_KEY=kis-key",
                        "KIS_APP_SECRET=kis-secret",
                        "TOSS_CLIENT_ID=toss-client",
                        "TOSS_CLIENT_SECRET=toss-secret",
                        "TOSS_ACCOUNT_SEQ=1",
                        "CORE_MIN_CASH_PCT=45",
                        "CORE_MAX_LEVERAGED_EXPOSURE_PCT=30",
                        "CORE_MAX_SINGLE_POSITION_PCT=20",
                        "CORE_MAX_DAILY_NEW_BUY_USD=1200",
                        "CORE_PANIC_BUY_BLOCK_MINUTES=20",
                        "CORE_DISCORD_WEBHOOK_URL=https://discord.example/webhook",
                        "CORE_DISCORD_USERNAME=test-bot",
                        "KI_DB_PATH=../ki/data/ki.db",
                        "TOSS_DB_PATH=../toss/data/toss.db",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                settings = Settings.from_env(env_path)

            self.assertEqual(settings.kis_app_key, "kis-key")
            self.assertEqual(settings.toss_client_id, "toss-client")
            self.assertEqual(settings.toss_account_seq, 1)
            self.assertEqual(str(settings.min_cash_pct), "45")
            self.assertEqual(str(settings.max_leveraged_exposure_pct), "30")
            self.assertEqual(str(settings.max_single_position_pct), "20")
            self.assertEqual(str(settings.max_daily_new_buy_usd), "1200")
            self.assertEqual(settings.panic_buy_block_minutes, 20)
            self.assertEqual(
                settings.discord_webhook_url,
                "https://discord.example/webhook",
            )
            self.assertEqual(settings.discord_username, "test-bot")
            self.assertEqual(
                settings.kis_database_path,
                (env_path.parent / "../ki/data/ki.db").resolve(),
            )
            self.assertEqual(
                settings.toss_database_path,
                (env_path.parent / "../toss/data/toss.db").resolve(),
            )


if __name__ == "__main__":
    unittest.main()
