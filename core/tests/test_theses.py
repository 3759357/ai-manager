import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.manager_cli import RequestStore, main
from core.theses import ThesisStore, validate_card

SEED = Path(__file__).resolve().parents[1] / "seed" / "theses-2026-09-25.json"


class ThesisStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = RequestStore(self.root)
        self.theses = ThesisStore(self.store)

    def test_draft_has_no_effect_until_approved(self):
        item = self.theses.add("msft", "마소 너무 싸졌다", thesis_id="msft-test")
        self.assertEqual(item["status"], "draft")
        self.assertEqual(item["symbol"], "MSFT")
        self.assertEqual(self.theses.effective(), [])
        with self.assertRaises(ValueError):  # 요약이 비어 있으면 승인 불가
            self.theses.approve("msft-test", 1)
        self.theses.edit("msft-test", {"thesis": "과도한 할인"})
        with self.assertRaises(ValueError):  # 사용자가 본 버전과 다르면 승인 불가
            self.theses.approve("msft-test", 1)
        approved = self.theses.approve("msft-test", 2)
        self.assertEqual(approved["status"], "active")
        self.assertEqual([c["symbol"] for c in self.theses.effective()], ["MSFT"])

    def test_edits_after_approval_keep_old_version_effective(self):
        self.theses.add("AMD", "좋다", thesis_id="amd-test", card={"thesis": "공급 부족 수혜"})
        self.theses.approve("amd-test", 1)
        edited = self.theses.edit("amd-test", {"conviction": 3})
        self.assertTrue(edited["pending_changes"])
        self.assertEqual(self.theses.effective()[0]["conviction"], 2)
        self.theses.approve("amd-test", 2)
        self.assertEqual(self.theses.effective()[0]["conviction"], 3)

    def test_limits_are_enforced(self):
        with self.assertRaises(ValueError):
            self.theses.add("AAPL", "좋다", card={"sizing": {"maxWeightPct": 40, "addOnDip": True}})
        with self.assertRaises(ValueError):
            self.theses.add("AAPL", "좋다", conviction=5)
        with self.assertRaises(ValueError):
            self.theses.add("ARM", "관찰", kind="watch", card={"sizing": {"maxWeightPct": 5, "addOnDip": False}})
        with self.assertRaises(ValueError):
            self.theses.add("AAPL", "")
        with self.assertRaises(ValueError):
            validate_card({"symbol": "AAPL", "userStatement": "x", "orders": []})
        self.theses.add("AAPL", "좋다", thesis_id="aapl-test")
        with self.assertRaises(ValueError):
            self.theses.edit("aapl-test", {"symbol": "MSFT"})

    def test_pause_resume_close_and_notes(self):
        self.theses.add("AMZN", "스테디셀러", thesis_id="amzn-test", card={"thesis": "AWS"})
        with self.assertRaises(ValueError):  # 승인 전에는 재개할 수 없다
            self.theses.set_status("amzn-test", "active")
        self.theses.approve("amzn-test", 1)
        self.assertEqual(self.theses.set_status("amzn-test", "paused")["status"], "paused")
        self.assertEqual(self.theses.effective(), [])
        self.theses.set_status("amzn-test", "active")
        self.theses.note("amzn-test", "실적 좋았음")
        with self.assertRaises(ValueError):
            self.theses.set_status("amzn-test", "closed")
        closed = self.theses.set_status("amzn-test", "closed", "목표 달성")
        self.assertEqual(closed["status"], "closed")
        self.assertIn("note", [e["kind"] for e in closed["events"]])
        with self.assertRaises(ValueError):
            self.theses.edit("amzn-test", {"conviction": 1})

    def test_watch_card_becomes_watching(self):
        self.theses.add("ARM", "200% 먹고 뺐지", kind="watch", thesis_id="arm-test", card={"thesis": "재진입 관찰"})
        item = self.theses.approve("arm-test", 1)
        self.assertEqual(item["status"], "watching")
        self.assertEqual(item["approved_card"]["sizing"]["maxWeightPct"], 0)

    def test_health_does_not_touch_approval(self):
        self.theses.add("QCOM", "Arm 베팅", thesis_id="qcom-test", card={"thesis": "Arm"})
        self.theses.approve("qcom-test", 1)
        item = self.theses.set_health("qcom-test", "weakened", "비스마트폰 매출 감소")
        self.assertEqual((item["health"], item["status"], item["version"]), ("weakened", "active", 1))

    def test_seed_import_is_idempotent_and_valid(self):
        first = self.theses.import_file(SEED)
        self.assertEqual({r["result"] for r in first}, {"created"})
        self.assertEqual(len(first), 6)
        self.assertEqual({r["result"] for r in self.theses.import_file(SEED)}, {"skipped"})
        symbols = {t["symbol"]: t for t in self.theses.list()}
        self.assertEqual(set(symbols), {"AMD", "AAPL", "QCOM", "MSFT", "AMZN", "ARM"})
        self.assertTrue(all(t["status"] == "draft" for t in symbols.values()))
        self.assertEqual(symbols["ARM"]["kind"], "watch")


class ThesisAIDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.theses = ThesisStore(RequestStore(self.root))
        self.spool = self.root / "spool"
        self.theses.add("AMD", "AMD 좋다", thesis_id="amd-ai")

    def _respond(self, call_id, body):
        path = self.spool / "responses" / f"{call_id}.json"
        path.write_text(json.dumps(body, ensure_ascii=False))

    def _draft(self):
        return {"thesis": "공급 부족 수혜", "relatedSymbols": ["NVDA"],
                "checks": [{"id": "dc", "what": "데이터센터 매출", "source": "실적", "healthy": "증가", "warning": "감소"}],
                "counterThesis": ["엔비디아 공급 정상화"], "invalidationAuto": [], "invalidationAlertOnly": ["고객 이탈"]}

    def test_ai_fills_draft_but_does_not_approve(self):
        req = self.theses.request_ai_draft("amd-ai", self.spool)
        body = json.loads((self.spool / "requests" / f"{req['call_id']}.json").read_text())
        self.assertEqual(body["_role"], "thesis")
        self.assertEqual(self.theses.request_ai_draft("amd-ai", self.spool)["requested"], False)
        self._respond(req["call_id"], self._draft())
        self.assertEqual(self.theses.ingest_ai_drafts(self.spool), [{"id": "amd-ai", "result": "drafted"}])
        item = self.theses.get("amd-ai")
        self.assertEqual((item["status"], item["version"], item["draft_card"]["thesis"]), ("draft", 2, "공급 부족 수혜"))
        self.assertEqual(item["draft_card"]["userStatement"], "AMD 좋다")
        self.assertEqual(self.theses.ingest_ai_drafts(self.spool), [])  # 한 번만 반영

    def test_user_edit_wins_over_late_ai_response(self):
        req = self.theses.request_ai_draft("amd-ai", self.spool)
        self.theses.edit("amd-ai", {"thesis": "내가 직접 씀"})
        self._respond(req["call_id"], self._draft())
        self.assertEqual(self.theses.ingest_ai_drafts(self.spool), [{"id": "amd-ai", "result": "stale"}])
        self.assertEqual(self.theses.get("amd-ai")["draft_card"]["thesis"], "내가 직접 씀")

    def test_invalid_ai_response_is_rejected(self):
        req = self.theses.request_ai_draft("amd-ai", self.spool)
        bad = self._draft()
        bad["checks"] = [{"id": "BAD ID", "what": "x"}]
        self._respond(req["call_id"], bad)
        self.assertEqual(self.theses.ingest_ai_drafts(self.spool), [{"id": "amd-ai", "result": "invalid"}])
        self.assertEqual(self.theses.get("amd-ai")["version"], 1)


class ThesisCLITests(unittest.TestCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--data-dir", self.tmp.name, *args])
        return code, out.getvalue()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_add_edit_approve_flow(self):
        with patch.dict(os.environ, {"AI_MANAGER_PLANNER_ENABLED": "false"}):
            code, out = self.run_cli("thesis", "add", "MSFT", "마소 너무 싸졌다", "--id", "msft-cli", "--conviction", "3")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["ai"]["requested"], False)
            self.run_cli("thesis", "edit", "msft-cli", "--thesis", "과도한 할인", "--max-weight", "12", "--add-on-dip", "on")
            _, out = self.run_cli("thesis", "approve", "msft-cli", "--version", "2")
            card = json.loads(out)["approved_card"]
            self.assertEqual((card["conviction"], card["sizing"]), (3, {"maxWeightPct": 12, "addOnDip": True}))
            _, out = self.run_cli("thesis", "list")
            self.assertIn("msft-cli", out)
            self.assertIn("active", out)

    def test_import_seed(self):
        _, out = self.run_cli("thesis", "import", str(SEED))
        self.assertEqual(len(json.loads(out)["imported"]), 6)


if __name__ == "__main__":
    unittest.main()
