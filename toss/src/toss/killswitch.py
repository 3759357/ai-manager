"""중앙 Kill Switch 확인.

core가 `core kill-switch on`으로 만드는 파일(기본: core/.env 옆 data/kill-switch.json,
`CORE_KILL_SWITCH_PATH`로 변경)을 브로커 패키지도 그대로 읽는다. 파일이 있으면 켜진 상태이고,
내용을 읽을 수 없어도 켜진 것으로 본다. 취소처럼 위험을 줄이는 요청은 막지 않는다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

DEFAULT_RELATIVE_PATH = "data/kill-switch.json"


def resolve_kill_switch_path(configured: str, *, base: Path) -> Path:
    path = Path(configured or DEFAULT_RELATIVE_PATH).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def kill_switch_reason(paths: Iterable[Path | None]) -> str | None:
    """켜진 스위치가 있으면 사유를, 없으면 None을 돌려준다."""
    for path in paths:
        if path is None or not path.exists():
            continue
        try:
            reason = str(json.loads(path.read_text(encoding="utf-8")).get("reason") or "")
        except (OSError, ValueError, AttributeError):
            reason = "Kill Switch 파일을 읽을 수 없어 보수적으로 차단"
        return reason or "사유 없음"
    return None


def assert_orders_allowed(paths: Iterable[Path | None]) -> None:
    reason = kill_switch_reason(paths)
    if reason is not None:
        raise PermissionError(
            f"Kill Switch가 켜져 있어 주문을 차단합니다: {reason}. 해제: core kill-switch off"
        )
