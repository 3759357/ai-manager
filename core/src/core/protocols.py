from __future__ import annotations

from typing import Any, Protocol

from core.models import (
    BrokerCapabilities,
    NormalizedQuote,
    UnifiedOrderRequest,
)


class BrokerAdapter(Protocol):
    name: str

    def capabilities(self) -> BrokerCapabilities: ...

    def quote(self, symbol: str) -> NormalizedQuote: ...

    def create_order_candidate(
        self,
        request: UnifiedOrderRequest,
    ) -> dict[str, Any]: ...

    def list_orders(self, limit: int = 20) -> list[dict[str, Any]]: ...

    def order_summary(self, order_id: int) -> dict[str, Any]: ...

    def submit_order(self, order_id: int, *, execute: bool) -> dict[str, Any]: ...

    def cancel_order(self, order_id: int, *, execute: bool) -> dict[str, Any]: ...
