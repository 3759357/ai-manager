import unittest

from ki.order_state import (
    ACCEPTED,
    CANCELED,
    CANCEL_REQUESTED,
    FILLED,
    ORDER_STATUS_DESCRIPTIONS,
    PREFLIGHT_BLOCKED,
    RISK_APPROVED,
    SUBMITTED,
    VOIDED,
    OrderStateError,
    assert_order_cancelable,
    assert_order_submittable,
    assert_order_transition,
    assert_order_voidable,
)


class OrderStateTest(unittest.TestCase):
    def test_known_statuses_have_descriptions(self) -> None:
        self.assertIn(RISK_APPROVED, ORDER_STATUS_DESCRIPTIONS)
        self.assertIn(CANCEL_REQUESTED, ORDER_STATUS_DESCRIPTIONS)

    def test_allows_submit_flow_transitions(self) -> None:
        assert_order_transition(RISK_APPROVED, ACCEPTED)
        assert_order_transition(RISK_APPROVED, SUBMITTED)
        assert_order_transition(RISK_APPROVED, PREFLIGHT_BLOCKED)
        assert_order_transition(RISK_APPROVED, VOIDED)

    def test_allows_cancel_flow_transitions(self) -> None:
        assert_order_transition(ACCEPTED, CANCEL_REQUESTED)
        assert_order_transition(SUBMITTED, CANCEL_REQUESTED)
        assert_order_transition(CANCEL_REQUESTED, CANCELED)

    def test_blocks_terminal_status_transition(self) -> None:
        with self.assertRaises(OrderStateError):
            assert_order_transition(FILLED, CANCEL_REQUESTED)

    def test_submit_and_cancel_guards(self) -> None:
        assert_order_submittable(RISK_APPROVED)
        assert_order_cancelable(ACCEPTED)
        assert_order_voidable(RISK_APPROVED)

        with self.assertRaises(OrderStateError):
            assert_order_submittable(ACCEPTED)
        with self.assertRaises(OrderStateError):
            assert_order_cancelable(PREFLIGHT_BLOCKED)
        with self.assertRaises(OrderStateError):
            assert_order_voidable(ACCEPTED)
