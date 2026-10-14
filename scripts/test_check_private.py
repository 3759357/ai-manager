import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import check_private


class CheckPrivateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write("deploy/local.env.example", "AI_MANAGER_SERVER=myserver\n")
        self.write("ios/Config/Local.xcconfig.example", "AI_MANAGER_BUNDLE_ID = com.example.ai-manager\n")
        self.write("core/.env.example", "TOSS_CLIENT_SECRET=\nCORE_DEFAULT_BROKER=toss\n")

    def write(self, name, text, add=True):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if add:
            subprocess.run(["git", "-C", str(self.root), "add", "-f", name], check=True)
        return path

    def run_check(self, *args):
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                code = check_private.main(list(args))
        finally:
            os.chdir(cwd)
        return code, err.getvalue()

    def test_clean_repository_passes(self):
        self.write("core/app.py", 'server = "myserver"  # example placeholder is fine\n')
        self.assertEqual(self.run_check("--staged")[0], 0)

    def test_private_folder_and_local_config_files_are_blocked(self):
        for name in ("private/notes.md", "core/.env", "deploy/local.env", "ios/Config/Local.xcconfig", "secrets/apns.p8", "core/data/x.db"):
            with self.subTest(name=name):
                self.write(name, "x\n")
                code, err = self.run_check("--staged")
                self.assertEqual(code, 1)
                self.assertIn(name, err)
                subprocess.run(["git", "-C", str(self.root), "rm", "-q", "--cached", name], check=True)

    def test_real_local_values_are_blocked_without_printing_them(self):
        self.write("deploy/local.env", "AI_MANAGER_SERVER=homebox7\n", add=False)
        self.write("ios/Config/Local.xcconfig", "AI_MANAGER_BUNDLE_ID = com.person.app\n", add=False)
        self.write("core/.env", "TOSS_CLIENT_SECRET=s3cr3t-value-123\nCORE_DEFAULT_BROKER=toss\n", add=False)
        for leak in ("ssh homebox7", "bundle com.person.app.tests", "secret = 's3cr3t-value-123'"):
            with self.subTest(leak=leak):
                self.write("docs/guide.md", leak + "\n")
                code, err = self.run_check("--staged")
                self.assertEqual(code, 1)
                self.assertIn("known-local-value", err)
                for value in ("homebox7", "com.person.app", "s3cr3t-value-123"):
                    self.assertNotIn(value, err)
        # 일반 설정값(브로커 이름)과 부분 일치는 개인 값으로 보지 않는다
        self.write("docs/guide.md", "broker toss, host homebox77\n")
        self.assertEqual(self.run_check("--staged")[0], 0)

    def test_secret_patterns_and_allow_marker(self):
        token = "ghp_" + "a" * 36
        self.write("core/app.py", f'TOKEN = "{token}"\n')
        code, err = self.run_check("--staged")
        self.assertEqual(code, 1)
        self.assertIn("github-token", err)
        self.assertNotIn(token, err)
        self.write("core/app.py", 'ip = "192.168.0.10"  # check-private: allow\n')
        self.assertEqual(self.run_check("--staged")[0], 0)

    def test_staged_mode_checks_index_not_working_tree(self):
        self.write("core/app.py", "ok = 1\n")
        (self.root / "core/app.py").write_text('url = "https://discord.com/api/webhooks/123/' + "b" * 30 + '"\n')
        self.assertEqual(self.run_check("--staged")[0], 0)
        self.assertEqual(self.run_check("--all")[0], 1)


if __name__ == "__main__":
    unittest.main()
