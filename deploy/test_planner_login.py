"""planner-login.sh를 가짜 docker로 검증한다. 실제 로그인·네트워크는 하지 않는다.

Run on Linux: python3 -m unittest discover -s deploy -p 'test_*.py' -v
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "planner-login.sh"

FAKE_DOCKER = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
record = {"args": args}
if "--with-api-key" in args:
    record["stdin_len"] = len(sys.stdin.read().strip())
with open(os.environ["DOCKER_CALLS"], "a") as log:
    log.write(json.dumps(record) + "\\n")
if os.environ.get("DOCKER_FAIL") and os.environ["DOCKER_FAIL"] in args:
    sys.exit(1)
'''


@unittest.skipUnless(sys.platform == "linux", "Deployment targets Linux")
class PlannerLoginTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.deploy = self.root / "ai-manager"
        release = self.deploy / "releases/abc"
        release.mkdir(parents=True)
        (self.deploy / "current").symlink_to(release)
        (self.deploy / "shared/secrets").mkdir(parents=True)
        (self.deploy / "bin").mkdir()
        deploy_sh = self.deploy / "bin/deploy.sh"
        deploy_sh.write_text('#!/bin/sh\necho deployed >> "$DEPLOY_CALLS"\n')
        deploy_sh.chmod(0o755)
        fake_bin = self.root / "fakebin"
        fake_bin.mkdir()
        docker = fake_bin / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        self.calls = self.root / "docker.jsonl"
        self.deploys = self.root / "deploys.txt"
        self.env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "AI_MANAGER_DEPLOY_ROOT": str(self.deploy),
                    "DOCKER_CALLS": str(self.calls), "DEPLOY_CALLS": str(self.deploys)}

    def run_script(self, *args, stdin="", fail=""):
        result = subprocess.run(["bash", str(SCRIPT), *args], input=stdin, text=True,
                                env={**self.env, "DOCKER_FAIL": fail},
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return result, calls

    def test_device_login_builds_logs_in_fixes_permissions_and_checks_status(self):
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout)
        args = [c["args"] for c in calls]
        self.assertIn("build", args[0])
        self.assertIn("--device-auth", args[1])
        self.assertIn('cli_auth_credentials_store="file"', args[1])
        self.assertIn("chmod 600 /home/agent/.codex/auth.json", " ".join(args[2]))
        self.assertEqual(args[3][-2:], ["login", "status"])
        for call in args:
            self.assertIn("docker-compose.planner.yml", call)
        secrets = self.deploy / "shared/secrets/codex-planner"
        self.assertEqual(stat.S_IMODE(secrets.stat().st_mode), 0o700)
        self.assertFalse(self.deploys.exists())

    def test_api_key_goes_through_stdin_not_arguments(self):
        result, calls = self.run_script("--api-key", stdin="sk-test-not-a-real-key\n")
        self.assertEqual(result.returncode, 0, result.stdout)
        login = next(c for c in calls if "--with-api-key" in c["args"])
        self.assertEqual(login["stdin_len"], len("sk-test-not-a-real-key"))
        self.assertIn("-T", login["args"])
        self.assertNotIn("sk-test", self.calls.read_text().replace('"stdin_len"', ""))
        self.assertNotIn("sk-test", result.stdout)

    def test_status_only_does_not_build_or_login(self):
        result, calls = self.run_script("--status")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["args"][-2:], ["login", "status"])

    def test_failed_login_stops_before_enabling(self):
        result, calls = self.run_script("--enable", fail="--device-auth")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("status" in c["args"] for c in calls))
        self.assertFalse(self.deploys.exists())
        self.assertFalse((self.deploy / "shared/deploy.env").exists())

    def test_enable_adds_planner_to_existing_stacks_and_redeploys(self):
        env_file = self.deploy / "shared/deploy.env"
        env_file.write_text('# comment\nAI_MANAGER_DEPLOY_STACKS="trading reports"\n')
        result, _ = self.run_script("--status", "--enable")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('AI_MANAGER_DEPLOY_STACKS="trading reports planner"', env_file.read_text())
        self.assertIn("# comment", env_file.read_text())
        self.assertEqual(self.deploys.read_text(), "deployed\n")
        result, _ = self.run_script("--status", "--enable")
        self.assertEqual(env_file.read_text().count("planner"), 1)

    def test_enable_creates_setting_when_missing(self):
        result, _ = self.run_script("--status", "--enable")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((self.deploy / "shared/deploy.env").read_text(), 'AI_MANAGER_DEPLOY_STACKS="reports planner"\n')

    def test_requires_existing_release_and_rejects_unknown_options(self):
        (self.deploy / "current").unlink()
        result, calls = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("먼저 reports를 배포", result.stdout)
        self.assertEqual(calls, [])
        result, _ = self.run_script("--wat")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
