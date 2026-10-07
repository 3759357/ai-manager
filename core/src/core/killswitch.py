from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.events import utc_now_iso


class KillSwitch:
    """파일 기반 중앙 Kill Switch.

    파일이 존재하면 켜진 상태다. 컨테이너들이 같은 볼륨을 공유하므로
    호스트에서 파일 하나로 전체 주문 경로를 멈출 수 있다.
    파일이 손상되어 읽을 수 없으면 보수적으로 켜진 것으로 본다.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def status(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"active": False}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {
                "active": True,
                "reason": "Kill Switch 파일을 읽을 수 없어 보수적으로 차단",
            }
        return {
            "active": True,
            "reason": str(data.get("reason") or ""),
            "activated_at": str(data.get("activated_at") or ""),
        }

    def activate(self, reason: str = "") -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "activated_at": utc_now_iso(),
                    "reason": reason.strip(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return self.status()

    def deactivate(self) -> dict[str, Any]:
        if self.path.exists():
            self.path.unlink()
        return self.status()

    def assert_allows_orders(self) -> None:
        status = self.status()
        if status["active"]:
            reason = status.get("reason") or "사유 없음"
            raise PermissionError(
                f"Kill Switch가 켜져 있어 주문을 차단합니다: {reason}. "
                "해제: core kill-switch off"
            )
