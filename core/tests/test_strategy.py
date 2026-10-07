import unittest
from decimal import Decimal

from core.strategy import (
    ReviewRejected,
    candidate_signature,
    parse_decision,
    validate_decision,
)


def _raw(**overrides):
    payload = {
        "decision": "APPROVE",
        "confidence": 0.8,
        "reasons": ["매도 압력이 완화되고 저점이 안정됨"],
        "invalidation": {"type": "PRICE_BELOW", "price": "94.00"},
        "expires_in_minutes": 120,
    }
    payload.update(overrides)
    return payload


class ParseDecisionTest(unittest.TestCase):
    def test_parses_valid_payload(self) -> None:
        decision = parse_decision(_raw())
        self.assertTrue(decision.approved)
        self.assertEqual(decision.invalidation_price, Decimal("94.00"))

    def test_rejects_non_object(self) -> None:
        with self.assertRaises(ReviewRejected) as ctx:
            parse_decision("APPROVE")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_rejects_unknown_decision(self) -> None:
        with self.assertRaises(ReviewRejected):
            parse_decision(_raw(decision="MAYBE"))

    def test_rejects_confidence_out_of_range(self) -> None:
        with self.assertRaises(ReviewRejected):
            parse_decision(_raw(confidence=1.4))

    def test_rejects_boolean_expiry(self) -> None:
        with self.assertRaises(ReviewRejected):
            parse_decision(_raw(expires_in_minutes=True))


class ValidateDecisionTest(unittest.TestCase):
    def test_accepts_consistent_decision(self) -> None:
        validate_decision(
            parse_decision(_raw()),
            lowest_entry_price=Decimal("97.00"),
            reference_price=Decimal("100.00"),
        )

    def test_rejects_invalidation_above_entry(self) -> None:
        decision = parse_decision(
            _raw(invalidation={"type": "PRICE_BELOW", "price": "99.00"})
        )
        with self.assertRaises(ReviewRejected) as ctx:
            validate_decision(
                decision,
                lowest_entry_price=Decimal("97.00"),
                reference_price=Decimal("100.00"),
            )
        self.assertEqual(ctx.exception.code, "INVALIDATION_ABOVE_ENTRY")

    def test_rejects_entry_outside_band(self) -> None:
        # 후보가 만들어진 뒤 시장이 크게 움직인 경우다.
        # 오래된 후보로 주문이 나가는 것을 여기서 막는다.
        decision = parse_decision(
            _raw(invalidation={"type": "PRICE_BELOW", "price": "65.00"})
        )
        with self.assertRaises(ReviewRejected) as ctx:
            validate_decision(
                decision,
                lowest_entry_price=Decimal("70.00"),
                reference_price=Decimal("100.00"),
            )
        self.assertEqual(ctx.exception.code, "BAND_VIOLATION")

    def test_rejects_low_confidence(self) -> None:
        with self.assertRaises(ReviewRejected) as ctx:
            validate_decision(
                parse_decision(_raw(confidence=0.2)),
                lowest_entry_price=Decimal("97.00"),
                reference_price=Decimal("100.00"),
            )
        self.assertEqual(ctx.exception.code, "LOW_CONFIDENCE")

    def test_reject_decision_skips_consistency_checks(self) -> None:
        decision = parse_decision(
            _raw(decision="REJECT", confidence=0.1, invalidation=None)
        )
        validate_decision(
            decision,
            lowest_entry_price=Decimal("97.00"),
            reference_price=Decimal("100.00"),
        )


class SignatureTest(unittest.TestCase):
    def test_same_situation_has_same_signature(self) -> None:
        left = candidate_signature(
            {"symbol": "SOXX", "stage": 1, "drop_bucket": "-8%", "noise": 1}
        )
        right = candidate_signature(
            {"symbol": "SOXX", "stage": 1, "drop_bucket": "-8%", "noise": 2}
        )
        self.assertEqual(left, right)

    def test_different_symbol_changes_signature(self) -> None:
        self.assertNotEqual(
            candidate_signature({"symbol": "SOXX"}),
            candidate_signature({"symbol": "SOXS"}),
        )


if __name__ == "__main__":
    unittest.main()
