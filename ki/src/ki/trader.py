from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from ki.client import KISClient
from ki.config import Settings


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThresholdStrategy:
    symbol: str
    quantity: int
    buy_below: int | None = None
    sell_above: int | None = None

    def decide(self, price: int) -> str | None:
        if self.buy_below is not None and price <= self.buy_below:
            return "buy"
        if self.sell_above is not None and price >= self.sell_above:
            return "sell"
        return None

    def validate(self) -> None:
        if self.quantity <= 0:
            raise ValueError("수량은 1주 이상이어야 합니다.")
        if self.buy_below is None and self.sell_above is None:
            raise ValueError("--buy-below 또는 --sell-above 중 하나는 필요합니다.")
        if self.buy_below is not None and self.buy_below <= 0:
            raise ValueError("매수 목표가는 0보다 커야 합니다.")
        if self.sell_above is not None and self.sell_above <= 0:
            raise ValueError("매도 목표가는 0보다 커야 합니다.")
        if (
            self.buy_below is not None
            and self.sell_above is not None
            and self.buy_below >= self.sell_above
        ):
            raise ValueError("매수 목표가는 매도 목표가보다 낮아야 합니다.")


class AutoTrader:
    def __init__(
        self,
        client: KISClient,
        settings: Settings,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.settings = settings
        self.sleep = sleep

    def run(
        self,
        strategy: ThresholdStrategy,
        interval_seconds: float,
        max_orders: int,
        execute: bool,
    ) -> list[dict[str, object]]:
        strategy.validate()
        if interval_seconds < 1:
            raise ValueError("조회 간격은 1초 이상이어야 합니다.")
        if max_orders <= 0:
            raise ValueError("최대 주문 횟수는 1 이상이어야 합니다.")
        if execute:
            self.settings.require_order_permission(execute=True)

        orders: list[dict[str, object]] = []
        while len(orders) < max_orders:
            price = self.client.current_price(strategy.symbol)
            side = strategy.decide(price)
            logger.info("현재가 %s원, 신호 %s", f"{price:,}", side or "대기")

            if side is None:
                self.sleep(interval_seconds)
                continue

            estimated_amount = price * strategy.quantity
            if estimated_amount > self.settings.max_order_amount:
                raise ValueError(
                    f"추정 주문금액 {estimated_amount:,}원이 한도 "
                    f"{self.settings.max_order_amount:,}원을 초과합니다."
                )
            if side == "sell":
                holding = self.client.holding_quantity(strategy.symbol)
                if holding < strategy.quantity:
                    raise ValueError(
                        f"매도 가능 수량이 부족합니다: 보유 {holding}주, 요청 {strategy.quantity}주"
                    )

            intent: dict[str, object] = {
                "mode": "execute" if execute else "dry-run",
                "side": side,
                "symbol": strategy.symbol,
                "quantity": strategy.quantity,
                "price": price,
                "estimated_amount": estimated_amount,
            }
            if execute:
                intent["result"] = self.client.order(
                    side=side,
                    symbol=strategy.symbol,
                    quantity=strategy.quantity,
                    price=price,
                    order_type="limit",
                )
            orders.append(intent)

            if len(orders) < max_orders:
                self.sleep(interval_seconds)
        return orders
