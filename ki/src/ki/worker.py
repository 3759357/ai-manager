from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from ki.account import sync_overseas_account
from ki.client import KISClient
from ki.config import Settings
from ki.reconcile import OrderReconciliationService
from ki.storage import SnapshotStore


@dataclass(frozen=True)
class WorkerOptions:
    interval_seconds: float = 60.0
    exchange: str = "NASD"
    days: int = 7
    sync_account: bool = False
    account_symbol: str = "AAPL"
    quote_exchange: str = "NAS"
    currency: str = "USD"
    max_cycles: int | None = None
    continue_on_error: bool = True


class OperationsWorker:
    def __init__(
        self,
        client: KISClient,
        settings: Settings,
        store: SnapshotStore,
        *,
        options: WorkerOptions | None = None,
        sleep: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.store = store
        self.options = options or WorkerOptions()
        self.sleep = sleep
        self.logger = logger or logging.getLogger(__name__)
        if self.options.interval_seconds <= 0:
            raise ValueError("worker interval은 0보다 커야 합니다.")
        if self.options.max_cycles is not None and self.options.max_cycles <= 0:
            raise ValueError("max_cycles는 1 이상이어야 합니다.")

    def run_once(self, cycle: int = 1) -> dict[str, Any]:
        result: dict[str, Any] = {"cycle": cycle}
        service = OrderReconciliationService(self.client, self.store)
        result["reconcile"] = service.reconcile_overseas(
            exchange=self.options.exchange,
            days=self.options.days,
        )
        if self.options.sync_account:
            result["account_sync"] = sync_overseas_account(
                self.client,
                self.settings,
                self.store,
                symbol=self.options.account_symbol,
                quote_exchange=self.options.quote_exchange,
                account_exchange=self.options.exchange,
                currency=self.options.currency,
            )
        return result

    def run(self) -> dict[str, Any]:
        cycle = 0
        last_result: dict[str, Any] | None = None

        while True:
            cycle += 1
            try:
                last_result = self.run_once(cycle=cycle)
                self.logger.info("worker cycle completed: %s", last_result)
            except Exception as exc:
                if not self.options.continue_on_error:
                    raise
                last_result = {
                    "cycle": cycle,
                    "error": type(exc).__name__,
                    "message": str(exc),
                }
                self.logger.exception("worker cycle failed")

            if self.options.max_cycles is not None and cycle >= self.options.max_cycles:
                return {"cycles": cycle, "last_result": last_result}

            self.sleep(self.options.interval_seconds)
