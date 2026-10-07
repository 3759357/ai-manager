from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from core.models import NormalizedQuote, UnifiedOrderRequest
from core.portfolio import BrokerPortfolio
from core.protocols import BrokerAdapter


class BrokerService:
    def __init__(self, adapters: dict[str, BrokerAdapter]) -> None:
        self.adapters = adapters

    def capabilities(self) -> list[dict[str, Any]]:
        return [
            self.adapters[name].capabilities().to_dict()
            for name in sorted(self.adapters)
        ]

    def quotes(self, symbol: str, broker: str) -> list[NormalizedQuote]:
        names = sorted(self.adapters) if broker == "both" else [broker]
        return [self._adapter(name).quote(symbol) for name in names]

    def compare_quotes(
        self,
        symbol: str,
        *,
        max_divergence_pct: Decimal,
    ) -> dict[str, Any]:
        quotes = self.quotes(symbol, "both")
        if len(quotes) != 2:
            raise ValueError("시세 비교에는 두 브로커가 필요합니다.")
        first, second = quotes
        midpoint = (first.price + second.price) / Decimal("2")
        divergence = (
            abs(first.price - second.price) / midpoint * Decimal("100")
            if midpoint > 0
            else Decimal("0")
        ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        return {
            "symbol": symbol.upper(),
            "quotes": [quote.to_dict() for quote in quotes],
            "divergence_pct": format(divergence, "f"),
            "max_divergence_pct": format(max_divergence_pct, "f"),
            "within_limit": divergence <= max_divergence_pct,
        }

    def create_order_candidate(
        self,
        request: UnifiedOrderRequest,
    ) -> dict[str, Any]:
        return self._adapter(request.broker).create_order_candidate(request)

    def list_orders(self, broker: str, limit: int = 20) -> dict[str, Any]:
        if broker == "both":
            return {
                name: self._adapter(name).list_orders(limit=limit)
                for name in sorted(self.adapters)
            }
        return {broker: self._adapter(broker).list_orders(limit=limit)}

    def submit_order(
        self,
        broker: str,
        order_id: int,
        *,
        execute: bool,
    ) -> dict[str, Any]:
        return self._adapter(broker).submit_order(order_id, execute=execute)

    def cancel_order(
        self,
        broker: str,
        order_id: int,
        *,
        execute: bool,
    ) -> dict[str, Any]:
        return self._adapter(broker).cancel_order(order_id, execute=execute)

    def collect_broker_portfolios(self) -> list[BrokerPortfolio]:
        """브로커별 현금·보유 포지션·예약 노출을 수집한다.

        한 브로커 수집 실패가 다른 브로커 수집을 막지 않도록 오류는
        `error`에 담아 돌려준다.
        """
        results = []
        for name in sorted(self.adapters):
            adapter = self.adapters[name]
            portfolio = BrokerPortfolio(broker=name)
            try:
                portfolio.cash = adapter.cash()
                portfolio.positions = adapter.positions()
                portfolio.pending_buy_amount = adapter.pending_buy_amount()
            except Exception as exc:  # noqa: BLE001 - 브로커별 격리
                portfolio.error = str(exc)
            results.append(portfolio)
        return results

    def _adapter(self, name: str) -> BrokerAdapter:
        try:
            return self.adapters[name]
        except KeyError as exc:
            raise ValueError(f"사용할 수 없는 브로커입니다: {name}") from exc
