from __future__ import annotations

from typing import Final


class OrderStateError(ValueError):
    """주문 상태 전이가 허용되지 않을 때 발생합니다."""


RISK_APPROVED: Final = "RISK_APPROVED"
SUBMITTING: Final = "SUBMITTING"
SUBMIT_UNKNOWN: Final = "SUBMIT_UNKNOWN"
SUBMITTED: Final = "SUBMITTED"
ACCEPTED: Final = "ACCEPTED"
PARTIALLY_FILLED: Final = "PARTIALLY_FILLED"
FILLED: Final = "FILLED"
REJECTED: Final = "REJECTED"
PREFLIGHT_BLOCKED: Final = "PREFLIGHT_BLOCKED"
CANCEL_REQUESTED: Final = "CANCEL_REQUESTED"
CANCELED: Final = "CANCELED"
VOIDED: Final = "VOIDED"
EXPIRED: Final = "EXPIRED"

ORDER_STATUS_DESCRIPTIONS: Final[dict[str, str]] = {
    RISK_APPROVED: "리스크 검증을 통과했지만 아직 KIS에 전송되지 않은 주문",
    SUBMITTING: "KIS에 전송 중인 주문. 이 상태에서는 다시 전송할 수 없음",
    SUBMIT_UNKNOWN: "전송 결과를 알 수 없는 주문. 재전송 금지, 대사로 확인",
    SUBMITTED: "KIS에 전송했지만 주문번호 확인이 필요한 주문",
    ACCEPTED: "KIS 주문번호가 확인되어 접수된 주문",
    PARTIALLY_FILLED: "일부 체결되고 잔량이 남은 주문",
    FILLED: "전량 체결된 주문",
    REJECTED: "KIS에서 거부된 주문",
    PREFLIGHT_BLOCKED: "실전 전송 직전 재검증에서 차단된 주문",
    CANCEL_REQUESTED: "KIS에 취소 요청을 접수한 주문",
    CANCELED: "취소 확정된 주문",
    VOIDED: "KIS에 전송하지 않고 로컬에서 폐기한 주문 후보",
    EXPIRED: "체결되지 않은 잔량 없이 주문 기간이 끝난 주문(일부 체결 후 소멸 포함)",
}

TERMINAL_ORDER_STATUSES: Final[frozenset[str]] = frozenset(
    {
        FILLED,
        REJECTED,
        PREFLIGHT_BLOCKED,
        CANCELED,
        VOIDED,
        EXPIRED,
    }
)

SUBMITTABLE_ORDER_STATUSES: Final[frozenset[str]] = frozenset({RISK_APPROVED})
CANCELABLE_ORDER_STATUSES: Final[frozenset[str]] = frozenset(
    {
        SUBMITTED,
        ACCEPTED,
        PARTIALLY_FILLED,
    }
)
VOIDABLE_ORDER_STATUSES: Final[frozenset[str]] = frozenset({RISK_APPROVED, SUBMIT_UNKNOWN})

ALLOWED_ORDER_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    RISK_APPROVED: frozenset({SUBMITTING, SUBMITTED, ACCEPTED, PREFLIGHT_BLOCKED, VOIDED}),
    # 전송 중 프로세스가 죽은 주문은 대사로 실제 결과를 찾아 연결할 수 있다.
    SUBMITTING: frozenset({SUBMITTED, ACCEPTED, PARTIALLY_FILLED, FILLED, REJECTED, CANCELED, EXPIRED, SUBMIT_UNKNOWN}),
    SUBMIT_UNKNOWN: frozenset({SUBMITTED, ACCEPTED, PARTIALLY_FILLED, FILLED, REJECTED, CANCELED, VOIDED, EXPIRED}),
    SUBMITTED: frozenset({ACCEPTED, PARTIALLY_FILLED, FILLED, REJECTED, CANCEL_REQUESTED, CANCELED, EXPIRED}),
    ACCEPTED: frozenset({PARTIALLY_FILLED, FILLED, REJECTED, CANCEL_REQUESTED, CANCELED, EXPIRED}),
    PARTIALLY_FILLED: frozenset({FILLED, CANCEL_REQUESTED, CANCELED, EXPIRED}),
    CANCEL_REQUESTED: frozenset({CANCELED, PARTIALLY_FILLED, FILLED, REJECTED, EXPIRED}),
    PREFLIGHT_BLOCKED: frozenset(),
    FILLED: frozenset(),
    REJECTED: frozenset(),
    CANCELED: frozenset(),
    VOIDED: frozenset(),
    EXPIRED: frozenset(),
}


def assert_known_order_status(status: str) -> None:
    if status not in ORDER_STATUS_DESCRIPTIONS:
        raise OrderStateError(f"알 수 없는 주문 상태입니다: {status}")


def assert_order_transition(current: str, next_status: str) -> None:
    assert_known_order_status(current)
    assert_known_order_status(next_status)
    if current == next_status:
        return
    if next_status not in ALLOWED_ORDER_TRANSITIONS[current]:
        raise OrderStateError(
            f"허용되지 않은 주문 상태 전이입니다: {current} -> {next_status}"
        )


def assert_order_submittable(status: str) -> None:
    assert_known_order_status(status)
    if status not in SUBMITTABLE_ORDER_STATUSES:
        raise OrderStateError(
            "실전 전송은 리스크 승인을 받은 주문만 가능합니다: "
            f"{status}"
        )


def assert_order_cancelable(status: str) -> None:
    assert_known_order_status(status)
    if status not in CANCELABLE_ORDER_STATUSES:
        raise OrderStateError(
            "취소는 KIS에 전송되어 체결 대기 중인 주문만 가능합니다: "
            f"{status}"
        )


def assert_order_voidable(status: str) -> None:
    assert_known_order_status(status)
    if status not in VOIDABLE_ORDER_STATUSES:
        raise OrderStateError(
            "폐기는 KIS에 전송되지 않은 리스크 승인 주문 후보만 가능합니다: "
            f"{status}"
        )
