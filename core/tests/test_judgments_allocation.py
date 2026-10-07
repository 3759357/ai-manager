import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.allocation import V1Params, equity_cap, sleeve_targets
from core.judgments import JudgmentStore
from core.manager_cli import RequestStore
from core.theses import ThesisStore

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "allocation_golden.json"
T0 = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


class JudgmentStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RequestStore(Path(self.tmp.name))
        self.theses = ThesisStore(self.store)
        self.theses.add("AMD", "좋다", thesis_id="amd-t", card={
            "thesis": "공급 부족 수혜",
            "checks": [{"id": "dc", "what": "데이터센터 매출", "source": "", "healthy": "", "warning": ""}],
            "invalidation": {"auto": ["데이터센터 매출 2분기 연속 감소"], "alertOnly": []},
            "sizing": {"maxWeightPct": 10, "addOnDip": True}})
        self.theses.approve("amd-t", 1)
        self.j = JudgmentStore(self.store)

    def rec(self, jid, kind, value, start=T0, days=3, **kw):
        return self.j.record(judgment_id=jid, thesis_id="amd-t", kind=kind, value=value,
                             valid_from=iso(start), valid_until=iso(start + timedelta(days=days)),
                             source="llm", model="test", **kw)

    def test_append_only_enforced_by_database(self):
        self.rec("jid-j1", "tilt", 1)
        with sqlite3.connect(self.store.path) as db:
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("UPDATE llm_judgments SET value='2'")
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("DELETE FROM llm_judgments")
        self.assertEqual(self.rec("jid-j1", "tilt", 1)["value"], 1)  # 같은 ID는 멱등

    def test_broken_requires_approved_rule(self):
        weak = self.rec("jid-h1", "thesis_health", "broken", rule="대충 나빠 보임")
        self.assertEqual(weak["value"], "weakened")
        self.assertIn("downgraded", weak["detail"])
        broken = self.rec("jid-h2", "thesis_health", "broken", rule="데이터센터 매출 2분기 연속 감소")
        self.assertEqual((broken["value"], broken["rule"]), ("broken", "데이터센터 매출 2분기 연속 감소"))
        self.assertEqual(self.theses.get("amd-t")["health"], "broken")

    def test_values_are_validated(self):
        with self.assertRaises(ValueError):
            self.rec("jid-x1", "tilt", 5)
        with self.assertRaises(ValueError):
            self.rec("jid-x2", "thesis_health", "great")
        with self.assertRaises(ValueError):
            self.rec("jid-x3", "thesis_health", "intact", detail={"checks": [{"id": "unknown-check", "status": "ok"}]})
        with self.assertRaises(ValueError):
            self.rec("jid-x4", "tilt", 1, days=30)
        with self.assertRaises(ValueError):
            self.rec("jid-x5", "event_risk", "rumor")

    def test_point_in_time_and_expiry(self):
        now = datetime.now(timezone.utc)
        base = now - timedelta(days=2)
        self.rec("jid-a", "tilt", 1, start=base)
        self.rec("jid-b", "tilt", -1, start=base + timedelta(days=1))
        self.assertEqual(self.j.current()["amd-t"]["tilt"]["value"], -1)  # 나중에 시작한 판단이 이김
        self.assertEqual(self.j.current(now + timedelta(days=4)), {})        # 둘 다 만료
        # 판단이 기록되기 전 시점으로 조회하면 보이지 않는다(그때는 몰랐던 정보)
        self.assertEqual(self.j.current(base + timedelta(hours=12)), {})

    def test_reapproval_invalidates_old_judgments(self):
        now = datetime.now(timezone.utc)
        self.rec("jid-old", "tilt", 2, start=now - timedelta(hours=1))
        self.assertIn("amd-t", self.j.current())
        self.theses.edit("amd-t", {"conviction": 3})
        self.assertIn("amd-t", self.j.current())  # 재승인 전에는 기존 승인본 유지
        self.theses.approve("amd-t", 2)
        self.assertEqual(self.j.current(), {})

    def test_unapproved_thesis_rejected(self):
        self.theses.add("AAPL", "좋다", thesis_id="aapl-d")
        with self.assertRaises(ValueError):
            self.j.record(judgment_id="u1", thesis_id="aapl-d", kind="tilt", value=1, valid_from=iso(T0),
                          valid_until=iso(T0 + timedelta(days=1)), source="llm", model="t")

    def test_daily_request_and_ingest(self):
        spool = Path(self.tmp.name) / "spool"
        now = datetime(2026, 9, 25, 0, 30, tzinfo=timezone.utc)  # KST 09:30
        req = self.j.request_daily(spool, now=now)
        self.assertTrue(req["requested"])
        self.assertFalse(self.j.request_daily(spool, now=now)["requested"])
        body = json.loads((spool / "requests" / f"{req['call_id']}.json").read_text())
        self.assertEqual((body["_role"], [t["id"] for t in body["theses"]]), ("judge", ["amd-t"]))
        (spool / "responses" / f"{req['call_id']}.json").write_text(json.dumps({"judgments": [
            {"thesisId": "amd-t", "health": "weakened", "rule": "", "tilt": -1,
             "checks": [{"id": "dc", "status": "warning", "evidence": "사용자 메모"}],
             "eventRisks": [{"type": "earnings", "at": "", "note": "날짜 미상"}], "explanation": "근거"},
            {"thesisId": "not-requested", "health": "intact", "rule": "", "tilt": 0, "checks": [],
             "eventRisks": [], "explanation": ""}]}, ensure_ascii=False))
        result = self.j.ingest(spool)
        self.assertEqual(result[0]["written"], 1)
        self.assertEqual(result[0]["skipped"], 1)
        self.assertEqual(self.j.ingest(spool), [])
        kinds = {r["kind"] for r in self.j.history("amd-t")}
        self.assertEqual(kinds, {"thesis_health", "tilt", "event_risk"})


class EquityCapTests(unittest.TestCase):
    def test_matches_research_backtest_golden_value(self):
        data = json.loads(FIXTURE.read_text())
        got = equity_cap(data["closes"], data["navs"])
        self.assertEqual(got["computed_on_index"], data["expected_computed_on_index"])
        self.assertAlmostEqual(got["cap"], data["expected_cap"], places=12)

    def test_calm_uptrend_is_capped_at_wmax_and_brake_recovers_slowly(self):
        closes = [100 * 1.0004 ** i for i in range(300)]
        navs = list(closes)
        calm = equity_cap(closes, navs)
        self.assertAlmostEqual(calm["cap"], 0.80)
        self.assertTrue(calm["trend_on"])
        # 계좌만 10% 빠진 경우 → 브레이크 0.5, 회복 후에도 20거래일 전에는 복귀하지 않음
        navs2 = navs[:280] + [navs[279] * 0.89] * 10 + [navs[279] * 1.01] * 10
        braked = equity_cap(closes, navs2)
        self.assertLess(braked["brake_factor"], 1)

    def test_input_validation(self):
        with self.assertRaises(ValueError):
            equity_cap([100], [1])
        with self.assertRaises(ValueError):
            equity_cap([100, 101], [1])
        with self.assertRaises(ValueError):
            equity_cap([100, -1], [1, 1])


class SleeveTests(unittest.TestCase):
    def card(self, cid, symbol, conviction=2, max_weight=15, kind="position"):
        return {"id": cid, "symbol": symbol, "kind": kind, "conviction": conviction,
                "sizing": {"maxWeightPct": max_weight if kind == "position" else 0, "addOnDip": True}}

    def test_limits_and_index_remainder(self):
        cards = [self.card("a", "AAPL"), self.card("m", "MSFT"), self.card("d", "AMD", max_weight=10),
                 self.card("w", "ARM", kind="watch")]
        res = sleeve_targets(0.70, cards, {}, vols={"AAPL": 0.28, "MSFT": 0.25, "AMD": 0.55})
        self.assertLessEqual(res.targets["AAPL"], 0.15)
        self.assertLessEqual(res.targets["AMD"], 0.10)
        self.assertNotIn("ARM", res.targets)
        self.assertAlmostEqual(sum(res.targets.values()) + res.index_weight, 0.70, places=6)
        self.assertAlmostEqual(res.cash_weight, 0.30)

    def test_health_tilt_and_earnings(self):
        now = datetime(2026, 9, 25, tzinfo=timezone.utc)
        cards = [self.card("a", "AAPL"), self.card("q", "QCOM"), self.card("m", "MSFT")]
        judg = {
            "a": {"thesis_health": {"value": "broken", "rule": "아이폰 3분기 감소"}},
            "q": {"thesis_health": {"value": "weakened"}},
            "m": {"tilt": {"value": 2}, "event_risk": {"value": "earnings", "detail": {"at": (now + timedelta(hours=20)).isoformat()}}},
        }
        res = sleeve_targets(0.30, cards, judg, vols={"AAPL": 0.3, "QCOM": 0.3, "MSFT": 0.3},
                             current={"QCOM": 0.04}, now=now)
        self.assertEqual(res.targets["AAPL"], 0.0)
        self.assertLessEqual(res.targets["QCOM"], 0.04)   # weakened: 추가 매수 없음
        self.assertFalse(res.buy_allowed["QCOM"])
        self.assertLessEqual(res.targets["MSFT"], 0.07)   # 실적 임박 한도
        self.assertTrue(res.buy_allowed["MSFT"])

    def test_brake_and_emergency_block_buys(self):
        cards = [self.card("a", "AAPL")]
        self.assertFalse(sleeve_targets(0.4, cards, {}, brake_active=True).buy_allowed["AAPL"])
        res = sleeve_targets(0.4, cards, {}, emergency_no_buys=True)
        self.assertFalse(res.buy_allowed["AAPL"])
        self.assertFalse(res.buy_allowed["QQQ"])


if __name__ == "__main__":
    unittest.main()
