"""컨테이너 권한 원칙이 설정 파일에서 깨지지 않았는지 확인한다(도커 없이 실행)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.reports.yml", "docker-compose.planner.yml")


def service_blocks(text: str) -> dict[str, str]:
    body = text.split("\nservices:\n", 1)[1]
    body = re.split(r"\n(?=\S)", body, maxsplit=1)[0]
    parts = re.split(r"^  ([\w-]+):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


class ContainerPolicyTest(unittest.TestCase):
    def test_every_service_runs_as_a_non_root_user(self) -> None:
        for name in COMPOSE_FILES:
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotRegex(text, r'(?m)user:\s*"?(0|root)(:0)?"?\s*$')
            for service, block in service_blocks(text).items():
                if "build:" not in block and "<<: *ki-service" not in block:
                    continue  # 다른 파일의 서비스에 값만 덧붙이는 overlay
                with self.subTest(file=name, service=service):
                    self.assertTrue("user:" in block or "<<: *ki-service" in block)

    def test_report_worker_sees_only_the_apns_key_directory(self) -> None:
        text = (ROOT / "docker-compose.reports.yml").read_text(encoding="utf-8")
        mounts = re.findall(r"^\s*- (\./secrets[^:]*):", text, flags=re.M)
        self.assertEqual(mounts, ["./secrets/apns"])

    def test_images_drop_root_and_pin_cli_versions(self) -> None:
        for name in ("core/Dockerfile", "ki/Dockerfile"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertRegex(text, r"(?m)^USER app$", name)
        for name in ("mind/Dockerfile.codex", "mind/Dockerfile.claude"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("@latest", text, name)
            self.assertRegex(text, r"(?m)^USER agent$", name)

    def test_llm_containers_get_no_broker_env_or_docker_socket(self) -> None:
        for name, service in (("docker-compose.yml", "strategy-agent"),
                              ("docker-compose.planner.yml", "planner")):
            block = service_blocks((ROOT / name).read_text(encoding="utf-8"))[service]
            with self.subTest(service=service):
                self.assertNotIn("env_file", block)
                self.assertNotIn("docker.sock", block)
                self.assertNotIn("core/.env", block)
                self.assertIn("read_only: true", block)


if __name__ == "__main__":
    unittest.main()
