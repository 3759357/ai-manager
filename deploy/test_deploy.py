"""Exercise deployment success and failures with an isolated Git repo and fake Docker.

Run on Linux: python3 -m unittest discover -s deploy -p 'test_*.py' -v
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


@unittest.skipUnless(sys.platform == "linux", "Deployment targets Linux")
class DeploymentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "app"
        self.app.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.app)], check=True)
        for package in ("core", "ki", "toss", "mind", "deploy"):
            (self.app / package).mkdir()
            (self.app / package / "README.md").write_text("fixture\n")
        for name in ("docker-compose.yml", "docker-compose.reports.yml", "docker-compose.planner.yml"):
            (self.app / name).write_text("services: {}\n")
        for name in ("deploy.sh", "post-receive", "ai-manager"):
            (self.app / "deploy" / name).write_text(f"# new {name}\n")
        subprocess.run(["git", "-C", str(self.app), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(self.app), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm", "feat: fixture",
        ], check=True)
        self.deploy = self.root / "deployment"
        for package in ("core", "ki", "toss"):
            (self.deploy / "shared" / package / "data").mkdir(parents=True)
        (self.deploy / "shared/core/.env").write_text("TEST=1\n")
        (self.deploy / "shared/spool").mkdir()
        (self.deploy / "shared/secrets").mkdir()
        self.old = self.deploy / "releases/previous"
        self.old.mkdir(parents=True)
        (self.old / "deploy-images.yml").write_text("services: {}\n")
        (self.old / "deploy-reports-images.yml").write_text("services: {}\n")
        (self.deploy / "current").symlink_to(self.old)
        (self.app / ".git/hooks").mkdir(exist_ok=True)
        self.home = self.root / "home"
        self.home.mkdir()
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        docker = fake_bin / "docker"
        docker.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["DOCKER_CALLS"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if "up" in args:
    with open(os.environ["DOCKER_CALLS"] + ".uid", "a") as log:
        log.write(os.environ.get("AI_MANAGER_UID", "unset") + ":" + os.environ.get("AI_MANAGER_GID", "unset") + "\\n")
mode = os.environ.get("DOCKER_FAILURE")
if mode == "test" and args[0] == "run":
    sys.exit(1)
if mode == "startup" and "up" in args and not any("/previous/" in a for a in args):
    sys.exit(1)
if mode == "smoke" and "exec" in args:
    sys.exit(1)
''')
        docker.chmod(0o755)
        curl = fake_bin / "curl"
        curl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CURL_CALLS"\n')
        curl.chmod(0o755)
        self.calls = self.root / "calls.jsonl"
        self.curl_calls = self.root / "curl.txt"
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "AI_MANAGER_DEPLOY_ROOT": str(self.deploy),
            "AI_MANAGER_DEPLOY_REPO": str(self.app / ".git"),
            "DOCKER_CALLS": str(self.calls),
            "CURL_CALLS": str(self.curl_calls),
        }

    def run_deploy(self, failure=""):
        result = subprocess.run(
            ["bash", str(HERE / "deploy.sh")],
            env={**self.env, "DOCKER_FAILURE": failure},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return result, calls

    @staticmethod
    def ups(calls, project):
        return [c for c in calls if "up" in c and project in c]

    def test_default_deploys_reports_and_planner_only(self):
        result, calls = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotEqual((self.deploy / "current").resolve(), self.old)
        test_call = next(c for c in calls if c[0] == "run")
        self.assertEqual(test_call[test_call.index("--network") + 1], "none")
        self.assertFalse(any("--env-file" in c for c in calls))
        self.assertEqual(self.ups(calls, "ai-manager"), [])  # trading 묶음은 기본으로 기동하지 않는다
        reports = self.ups(calls, "ai-manager-reports")
        self.assertEqual(len(reports), 1)
        self.assertTrue(any(a.endswith("docker-compose.planner.yml") for a in reports[0]))
        self.assertTrue(any("exec" in c and "thesis" in c for c in calls))
        self.assertIn("DEPLOY OK", result.stdout)
        # 배포 도구도 새 릴리스 버전으로 설치된다
        self.assertEqual((self.deploy / "bin/deploy.sh").read_text(), "# new deploy.sh\n")
        self.assertEqual((self.app / ".git/hooks/post-receive").read_text(), "# new post-receive\n")
        self.assertEqual((self.home / ".local/bin/ai-manager").read_text(), "# new ai-manager\n")

    def test_trading_stack_is_opt_in(self):
        (self.deploy / "shared/deploy.env").write_text('AI_MANAGER_DEPLOY_STACKS="trading reports"\n')
        result, calls = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.ups(calls, "ai-manager")), 1)
        reports = self.ups(calls, "ai-manager-reports")
        self.assertFalse(any(a.endswith("docker-compose.planner.yml") for a in reports[0]))

    def test_unknown_stack_is_rejected(self):
        (self.deploy / "shared/deploy.env").write_text('AI_MANAGER_DEPLOY_STACKS="everything"\n')
        result, calls = self.run_deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def test_failing_tests_leave_current_and_containers_unchanged(self):
        result, calls = self.run_deploy("test")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.deploy / "current").resolve(), self.old)
        self.assertFalse(any("up" in c or "down" in c for c in calls))

    def test_startup_failure_restores_previous_containers(self):
        result, calls = self.run_deploy("startup")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.deploy / "current").resolve(), self.old)
        reports = self.ups(calls, "ai-manager-reports")
        self.assertEqual(len(reports), 2)
        self.assertIn(str(self.old / "deploy-reports-images.yml"), reports[-1])

    def test_smoke_failure_restores_and_keeps_current(self):
        result, calls = self.run_deploy("smoke")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.deploy / "current").resolve(), self.old)
        self.assertIn(str(self.old / "deploy-reports-images.yml"), self.ups(calls, "ai-manager-reports")[-1])
        self.assertFalse((self.deploy / "bin/deploy.sh").exists())

    def test_discord_notification_on_success_and_failure(self):
        (self.deploy / "shared/core/.env").write_text("CORE_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1/x\n")
        self.run_deploy()
        self.assertIn("배포 성공", self.curl_calls.read_text())
        self.curl_calls.unlink()
        self.run_deploy("test")
        text = self.curl_calls.read_text()
        self.assertIn("배포 실패", text)
        self.assertIn("test", text)

    def test_no_notification_without_webhook(self):
        self.run_deploy()
        self.assertFalse(self.curl_calls.exists())

    def test_containers_run_as_the_deploy_user_after_one_ownership_fix(self):
        result, calls = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stdout)
        runs = [c for c in calls if c[0] == "run"]
        self.assertEqual(len(runs), 2)  # 격리 테스트 1회 + 소유자 정리 1회
        fix = runs[1]
        self.assertEqual(fix[fix.index("--user") + 1], "0")
        self.assertEqual(fix[fix.index("--network") + 1], "none")
        self.assertIn(f"{self.deploy}/shared:/shared", fix)
        uid, gid = str(os.getuid()), str(os.getgid())
        self.assertEqual(fix[-2:], [uid, gid])
        up_users = Path(str(self.calls) + ".uid").read_text().split()
        self.assertTrue(up_users)
        self.assertEqual(set(up_users), {f"{uid}:{gid}"})

    def test_ownership_fix_runs_only_after_tests_pass(self):
        result, calls = self.run_deploy("test")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len([c for c in calls if c[0] == "run"]), 1)


@unittest.skipUnless(sys.platform == "linux", "Hook test uses a Linux shell")
class PostCommitHookTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.server = root / "server.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.server)], check=True)
        self.repo = root / "repo"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        hooks = self.repo / ".githooks"
        hooks.mkdir()
        (hooks / "post-commit").write_text((HERE.parent / ".githooks/post-commit").read_text())
        (hooks / "post-commit").chmod(0o755)
        for args in (["config", "core.hooksPath", ".githooks"], ["remote", "add", "server", str(self.server)],
                     ["config", "user.name", "Test"], ["config", "user.email", "t@example.invalid"]):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True)

    def commit(self, message, env=None, branch=None):
        if branch:
            subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "-b", branch], check=True)
        (self.repo / "f.txt").write_text(message)
        subprocess.run(["git", "-C", str(self.repo), "add", "f.txt"], check=True)
        return subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", message],
                              env={**os.environ, "AI_MANAGER_DEPLOY_WAIT": "1", **(env or {})},
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def server_head(self):
        r = subprocess.run(["git", "--git-dir", str(self.server), "rev-parse", "--verify", "-q", "refs/heads/main"],
                           stdout=subprocess.PIPE, text=True)
        return r.stdout.strip() or None

    def test_commit_on_main_pushes(self):
        out = self.commit("feat: one")
        local = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], stdout=subprocess.PIPE, text=True).stdout.strip()
        self.assertEqual(self.server_head(), local, out.stdout)
        self.assertTrue((self.repo / ".git/ai-manager-deploy.log").exists())

    def test_opt_out_and_other_branches_do_not_push(self):
        self.commit("feat: skip", env={"AI_MANAGER_AUTO_DEPLOY": "0"})
        self.assertIsNone(self.server_head())
        self.commit("feat: branch", branch="topic")
        self.assertIsNone(self.server_head())


if __name__ == "__main__":
    unittest.main()
