import unittest

from core.notify import DiscordNotifier, NotificationError


class DiscordNotifierTest(unittest.TestCase):
    def test_dry_run_returns_payload_without_webhook_url(self) -> None:
        result = DiscordNotifier("", username="tester").send(
            "hello",
            dry_run=True,
        )

        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["payload"]["content"], "hello")
        self.assertEqual(result["payload"]["username"], "tester")

    def test_live_send_uses_injected_poster(self) -> None:
        calls = []

        def poster(url, data, headers, timeout):
            calls.append((url, data, headers, timeout))
            return 204, ""

        result = DiscordNotifier(
            "https://discord.example/webhook",
            username="tester",
            timeout_seconds=3,
            poster=poster,
        ).send("hello")

        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["status"], 204)
        self.assertEqual(calls[0][0], "https://discord.example/webhook")
        self.assertIn(b"hello", calls[0][1])
        self.assertEqual(calls[0][2]["Content-Type"], "application/json")
        self.assertEqual(calls[0][3], 3)

    def test_live_send_requires_webhook_url(self) -> None:
        with self.assertRaises(NotificationError):
            DiscordNotifier("").send("hello")


if __name__ == "__main__":
    unittest.main()
