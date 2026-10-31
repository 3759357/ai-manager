from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ki.models import BrokerOrder, Fill, OrderEvent, OrderIntent, RiskDecision


class SnapshotStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def save_overseas_account(
        self,
        *,
        environment: str,
        symbol: str,
        exchange: str,
        currency: str,
        quote_price: str,
        balance: dict[str, Any],
        buying_power: dict[str, Any],
    ) -> int:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        captured_at = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            cursor = connection.execute(
                """
                INSERT INTO overseas_account_snapshots (
                    captured_at,
                    environment,
                    symbol,
                    exchange,
                    currency,
                    quote_price,
                    balance_json,
                    buying_power_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    captured_at,
                    environment,
                    symbol,
                    exchange,
                    currency,
                    quote_price,
                    json.dumps(balance, ensure_ascii=False, sort_keys=True),
                    json.dumps(buying_power, ensure_ascii=False, sort_keys=True),
                ),
            )
            snapshot_id = cursor.lastrowid

        if snapshot_id is None:
            raise RuntimeError("계좌 스냅샷 ID를 생성하지 못했습니다.")
        return snapshot_id

    def save_order_intent(self, intent: OrderIntent) -> int:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        created_at = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            cursor = connection.execute(
                """
                INSERT INTO order_intents (
                    created_at,
                    source,
                    market,
                    symbol,
                    side,
                    quantity,
                    order_type,
                    limit_price,
                    currency,
                    exchange,
                    estimated_amount,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    intent.source,
                    intent.market,
                    intent.symbol,
                    intent.side,
                    intent.quantity,
                    intent.order_type,
                    format(intent.limit_price, "f"),
                    intent.currency,
                    intent.exchange,
                    format(intent.estimated_amount, "f"),
                    intent.status,
                ),
            )
            intent_id = cursor.lastrowid

        if intent_id is None:
            raise RuntimeError("주문 후보 ID를 생성하지 못했습니다.")
        return intent_id

    def save_risk_decision(self, decision: RiskDecision) -> int:
        if decision.order_intent_id is None:
            raise ValueError("RiskDecision 저장에는 order_intent_id가 필요합니다.")

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        created_at = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            cursor = connection.execute(
                """
                INSERT INTO risk_decisions (
                    created_at,
                    order_intent_id,
                    decision,
                    reasons_json,
                    checks_json
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    decision.order_intent_id,
                    decision.decision,
                    json.dumps(decision.reasons, ensure_ascii=False),
                    json.dumps(decision.checks, ensure_ascii=False, sort_keys=True),
                ),
            )
            decision_id = cursor.lastrowid

        if decision_id is None:
            raise RuntimeError("리스크 결정 ID를 생성하지 못했습니다.")
        return decision_id

    def save_broker_order(self, order: BrokerOrder) -> int:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        created_at = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            cursor = connection.execute(
                """
                INSERT INTO broker_orders (
                    created_at,
                    updated_at,
                    order_intent_id,
                    symbol,
                    side,
                    quantity,
                    order_type,
                    limit_price,
                    currency,
                    exchange,
                    estimated_amount,
                    status,
                    kis_order_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    created_at,
                    order.order_intent_id,
                    order.symbol,
                    order.side,
                    order.quantity,
                    order.order_type,
                    format(order.limit_price, "f"),
                    order.currency,
                    order.exchange,
                    format(order.estimated_amount, "f"),
                    order.status,
                    order.kis_order_id,
                ),
            )
            order_id = cursor.lastrowid

        if order_id is None:
            raise RuntimeError("주문 원장 ID를 생성하지 못했습니다.")
        return order_id

    def save_order_event(self, event: OrderEvent) -> int:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        created_at = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            cursor = connection.execute(
                """
                INSERT INTO order_events (
                    created_at,
                    order_id,
                    event_type,
                    status,
                    message,
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    event.order_id,
                    event.event_type,
                    event.status,
                    event.message,
                    json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                ),
            )
            event_id = cursor.lastrowid

        if event_id is None:
            raise RuntimeError("주문 이벤트 ID를 생성하지 못했습니다.")
        return event_id

    def save_fill(self, fill: Fill) -> int:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        created_at = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            if fill.fill_signature:
                row = connection.execute(
                    "SELECT id FROM fills WHERE fill_signature = ?",
                    (fill.fill_signature,),
                ).fetchone()
                if row is not None:
                    return int(row[0])
            cursor = connection.execute(
                """
                INSERT INTO fills (
                    created_at,
                    order_id,
                    symbol,
                    side,
                    quantity,
                    price,
                    currency,
                    fee,
                    fill_signature,
                    raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    fill.order_id,
                    fill.symbol,
                    fill.side,
                    fill.quantity,
                    format(fill.price, "f"),
                    fill.currency,
                    format(fill.fee, "f"),
                    fill.fill_signature,
                    json.dumps(fill.raw, ensure_ascii=False, sort_keys=True),
                ),
            )
            fill_id = cursor.lastrowid

        if fill_id is None:
            raise RuntimeError("체결 ID를 생성하지 못했습니다.")
        return fill_id

    def find_broker_order_by_kis_order_id(
        self,
        kis_order_id: str,
    ) -> dict[str, Any] | None:
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT
                    id,
                    created_at,
                    updated_at,
                    order_intent_id,
                    symbol,
                    side,
                    quantity,
                    order_type,
                    limit_price,
                    currency,
                    exchange,
                    estimated_amount,
                    status,
                    kis_order_id
                FROM broker_orders
                WHERE kis_order_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (kis_order_id,),
            ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def get_broker_order(self, order_id: int) -> dict[str, Any] | None:
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT
                    id,
                    created_at,
                    updated_at,
                    order_intent_id,
                    symbol,
                    side,
                    quantity,
                    order_type,
                    limit_price,
                    currency,
                    exchange,
                    estimated_amount,
                    status,
                    kis_order_id
                FROM broker_orders
                WHERE id = ?
                """,
                (order_id,),
            ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def update_broker_order_status(self, order_id: int, status: str) -> None:
        updated_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.execute(
                """
                UPDATE broker_orders
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, updated_at, order_id),
            )

    def find_unlinked_submissions(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
    ) -> list[dict[str, Any]]:
        """KIS 주문번호 없이 전송됐을 수 있는 주문(전송 중·결과 불명·주문번호 미확인)."""
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT id, symbol, side, quantity, limit_price, status, created_at
                FROM broker_orders
                WHERE kis_order_id IS NULL
                  AND status IN ('SUBMITTING', 'SUBMIT_UNKNOWN', 'SUBMITTED')
                  AND symbol = ? AND side = ? AND quantity = ?
                ORDER BY id
                """,
                (symbol, side, quantity),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def link_kis_order(self, order_id: int, *, kis_order_id: str, status: str) -> bool:
        """주문번호가 아직 없는 원장 주문에만 KIS 주문번호를 붙인다."""
        updated_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            cursor = connection.execute(
                "UPDATE broker_orders SET kis_order_id = ?, status = ?, updated_at = ? "
                "WHERE id = ? AND kis_order_id IS NULL",
                (kis_order_id, status, updated_at, order_id),
            )
        return cursor.rowcount == 1

    def claim_broker_order(self, order_id: int, *, expected: str, status: str) -> bool:
        """expected 상태일 때만 원자적으로 바꾼다(동시 제출 방지)."""
        updated_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            cursor = connection.execute(
                "UPDATE broker_orders SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (status, updated_at, order_id, expected),
            )
        return cursor.rowcount == 1

    def mark_broker_order_submitted(
        self,
        order_id: int,
        *,
        status: str,
        kis_order_id: str | None,
    ) -> None:
        updated_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.execute(
                """
                UPDATE broker_orders
                SET status = ?, kis_order_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, kis_order_id, updated_at, order_id),
            )

    def list_broker_orders(self, limit: int = 20) -> list[dict[str, Any]]:
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT
                    id,
                    created_at,
                    updated_at,
                    order_intent_id,
                    symbol,
                    side,
                    quantity,
                    order_type,
                    limit_price,
                    currency,
                    exchange,
                    estimated_amount,
                    status,
                    kis_order_id
                FROM broker_orders
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def fill_totals(self, order_id: int) -> tuple[int, Decimal]:
        """주문에 이미 기록한 체결 수량 합계와 금액 합계."""
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            rows = connection.execute(
                "SELECT quantity, price FROM fills WHERE order_id = ?",
                (order_id,),
            ).fetchall()
        quantity = sum(int(row[0]) for row in rows)
        amount = sum((Decimal(str(row[1])) * int(row[0]) for row in rows), Decimal("0"))
        return quantity, amount

    def list_fills(self, limit: int = 20) -> list[dict[str, Any]]:
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT
                    id,
                    created_at,
                    order_id,
                    symbol,
                    side,
                    quantity,
                    price,
                    currency,
                    fee,
                    fill_signature,
                    raw_json
                FROM fills
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        fills = [_row_to_dict(row) for row in rows]
        for fill in fills:
            fill["raw"] = json.loads(fill.pop("raw_json"))
        return fills

    def upsert_watchlist_item(
        self,
        *,
        symbol: str,
        quote_exchange: str,
        account_exchange: str,
        enabled: bool,
        quantity: int,
        hot_up_pct: str,
        hot_down_pct: str,
        action_on_hot_up: str,
        action_on_hot_down: str,
        limit_offset_pct: str,
    ) -> int:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        symbol = symbol.upper()
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            row = connection.execute(
                "SELECT id FROM watchlist WHERE symbol = ?",
                (symbol,),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    """
                    INSERT INTO watchlist (
                        created_at,
                        updated_at,
                        symbol,
                        quote_exchange,
                        account_exchange,
                        enabled,
                        quantity,
                        hot_up_pct,
                        hot_down_pct,
                        action_on_hot_up,
                        action_on_hot_down,
                        limit_offset_pct
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        now,
                        symbol,
                        quote_exchange.upper(),
                        account_exchange.upper(),
                        1 if enabled else 0,
                        quantity,
                        hot_up_pct,
                        hot_down_pct,
                        action_on_hot_up,
                        action_on_hot_down,
                        limit_offset_pct,
                    ),
                )
                item_id = cursor.lastrowid
            else:
                item_id = int(row[0])
                connection.execute(
                    """
                    UPDATE watchlist
                    SET
                        updated_at = ?,
                        quote_exchange = ?,
                        account_exchange = ?,
                        enabled = ?,
                        quantity = ?,
                        hot_up_pct = ?,
                        hot_down_pct = ?,
                        action_on_hot_up = ?,
                        action_on_hot_down = ?,
                        limit_offset_pct = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        quote_exchange.upper(),
                        account_exchange.upper(),
                        1 if enabled else 0,
                        quantity,
                        hot_up_pct,
                        hot_down_pct,
                        action_on_hot_up,
                        action_on_hot_down,
                        limit_offset_pct,
                        item_id,
                    ),
                )
        if item_id is None:
            raise RuntimeError("관심종목 ID를 생성하지 못했습니다.")
        return int(item_id)

    def list_watchlist(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.row_factory = sqlite3.Row
            query = """
                SELECT
                    id,
                    created_at,
                    updated_at,
                    symbol,
                    quote_exchange,
                    account_exchange,
                    enabled,
                    quantity,
                    hot_up_pct,
                    hot_down_pct,
                    action_on_hot_up,
                    action_on_hot_down,
                    limit_offset_pct
                FROM watchlist
            """
            params: tuple[Any, ...] = ()
            if enabled_only:
                query += " WHERE enabled = ?"
                params = (1,)
            query += " ORDER BY symbol"
            rows = connection.execute(query, params).fetchall()
        items = [_row_to_dict(row) for row in rows]
        for item in items:
            item["enabled"] = bool(item["enabled"])
        return items

    def remove_watchlist_item(self, symbol: str) -> bool:
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            cursor = connection.execute(
                "DELETE FROM watchlist WHERE symbol = ?",
                (symbol.upper(),),
            )
        return cursor.rowcount > 0

    def save_market_snapshot(
        self,
        *,
        symbol: str,
        price: str,
        quote: dict[str, Any],
    ) -> int:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        captured_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            cursor = connection.execute(
                """
                INSERT INTO market_snapshots (
                    captured_at,
                    symbol,
                    price,
                    quote_json
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    captured_at,
                    symbol.upper(),
                    price,
                    json.dumps(quote, ensure_ascii=False, sort_keys=True),
                ),
            )
            snapshot_id = cursor.lastrowid
        if snapshot_id is None:
            raise RuntimeError("시장 스냅샷 ID를 생성하지 못했습니다.")
        return snapshot_id

    def latest_market_snapshot(self, symbol: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT
                    id,
                    captured_at,
                    symbol,
                    price,
                    quote_json
                FROM market_snapshots
                WHERE symbol = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (symbol.upper(),),
            ).fetchone()
        if row is None:
            return None
        snapshot = _row_to_dict(row)
        snapshot["quote"] = json.loads(snapshot.pop("quote_json"))
        return snapshot

    def save_market_signal(
        self,
        *,
        symbol: str,
        signal_type: str,
        severity: str,
        message: str,
        payload: dict[str, Any],
        order_id: int | None = None,
    ) -> int:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        created_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            cursor = connection.execute(
                """
                INSERT INTO market_signals (
                    created_at,
                    symbol,
                    signal_type,
                    severity,
                    message,
                    payload_json,
                    order_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    symbol.upper(),
                    signal_type,
                    severity,
                    message,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    order_id,
                ),
            )
            signal_id = cursor.lastrowid
        if signal_id is None:
            raise RuntimeError("시장 시그널 ID를 생성하지 못했습니다.")
        return signal_id

    def list_market_signals(
        self,
        limit: int = 20,
        *,
        symbol: str | None = None,
        signal_type: str | None = None,
    ) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if symbol:
            where.append("symbol = ?")
            params.append(symbol.upper())
        if signal_type:
            where.append("signal_type = ?")
            params.append(signal_type)
        where_clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"""
                SELECT
                    id,
                    created_at,
                    symbol,
                    signal_type,
                    severity,
                    message,
                    payload_json,
                    order_id
                FROM market_signals
                {where_clause}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        signals = [_row_to_dict(row) for row in rows]
        for signal in signals:
            signal["payload"] = json.loads(signal.pop("payload_json"))
        return signals

    def save_orderbook_snapshot(
        self,
        *,
        symbol: str,
        tr_key: str,
        session: str,
        bid_price: str,
        ask_price: str,
        bid_quantity: str,
        ask_quantity: str,
        total_bid_quantity: str,
        total_ask_quantity: str,
        spread: str,
        pressure_score: str,
        raw: dict[str, Any],
    ) -> int:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        captured_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            cursor = connection.execute(
                """
                INSERT INTO orderbook_snapshots (
                    captured_at,
                    symbol,
                    tr_key,
                    session,
                    bid_price,
                    ask_price,
                    bid_quantity,
                    ask_quantity,
                    total_bid_quantity,
                    total_ask_quantity,
                    spread,
                    pressure_score,
                    raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    captured_at,
                    symbol.upper(),
                    tr_key,
                    session,
                    bid_price,
                    ask_price,
                    bid_quantity,
                    ask_quantity,
                    total_bid_quantity,
                    total_ask_quantity,
                    spread,
                    pressure_score,
                    json.dumps(raw, ensure_ascii=False, sort_keys=True),
                ),
            )
            snapshot_id = cursor.lastrowid
        if snapshot_id is None:
            raise RuntimeError("호가 스냅샷 ID를 생성하지 못했습니다.")
        return snapshot_id

    def list_orderbook_snapshots(self, limit: int = 20) -> list[dict[str, Any]]:
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT
                    id,
                    captured_at,
                    symbol,
                    tr_key,
                    session,
                    bid_price,
                    ask_price,
                    bid_quantity,
                    ask_quantity,
                    total_bid_quantity,
                    total_ask_quantity,
                    spread,
                    pressure_score,
                    raw_json
                FROM orderbook_snapshots
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        snapshots = [_row_to_dict(row) for row in rows]
        for snapshot in snapshots:
            snapshot["raw"] = json.loads(snapshot.pop("raw_json"))
        return snapshots

    def save_trade_tick(
        self,
        *,
        symbol: str,
        tr_key: str,
        session: str,
        local_date: str,
        local_time: str,
        korea_date: str,
        korea_time: str,
        price: str,
        quantity: str,
        cumulative_volume: str,
        cumulative_amount: str,
        sell_trade_quantity: str,
        buy_trade_quantity: str,
        execution_strength: str,
        raw: dict[str, Any],
    ) -> int:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        captured_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            cursor = connection.execute(
                """
                INSERT INTO trade_ticks (
                    captured_at,
                    symbol,
                    tr_key,
                    session,
                    local_date,
                    local_time,
                    korea_date,
                    korea_time,
                    price,
                    quantity,
                    cumulative_volume,
                    cumulative_amount,
                    sell_trade_quantity,
                    buy_trade_quantity,
                    execution_strength,
                    raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    captured_at,
                    symbol.upper(),
                    tr_key,
                    session,
                    local_date,
                    local_time,
                    korea_date,
                    korea_time,
                    price,
                    quantity,
                    cumulative_volume,
                    cumulative_amount,
                    sell_trade_quantity,
                    buy_trade_quantity,
                    execution_strength,
                    json.dumps(raw, ensure_ascii=False, sort_keys=True),
                ),
            )
            tick_id = cursor.lastrowid
        if tick_id is None:
            raise RuntimeError("체결 틱 ID를 생성하지 못했습니다.")
        return int(tick_id)

    def upsert_trade_candle(
        self,
        *,
        symbol: str,
        session: str,
        interval_minutes: int,
        bucket_at: str,
        price: str,
        quantity: str,
    ) -> dict[str, Any]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        updated_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.execute(
                """
                INSERT INTO trade_candles (
                    symbol,
                    session,
                    interval_minutes,
                    bucket_at,
                    open_price,
                    high_price,
                    low_price,
                    close_price,
                    volume,
                    trade_count,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(symbol, session, interval_minutes, bucket_at)
                DO UPDATE SET
                    high_price = CASE
                        WHEN CAST(excluded.high_price AS REAL)
                            > CAST(trade_candles.high_price AS REAL)
                        THEN excluded.high_price
                        ELSE trade_candles.high_price
                    END,
                    low_price = CASE
                        WHEN CAST(excluded.low_price AS REAL)
                            < CAST(trade_candles.low_price AS REAL)
                        THEN excluded.low_price
                        ELSE trade_candles.low_price
                    END,
                    close_price = excluded.close_price,
                    volume = CAST(
                        CAST(trade_candles.volume AS REAL)
                        + CAST(excluded.volume AS REAL)
                        AS TEXT
                    ),
                    trade_count = trade_candles.trade_count + 1,
                    updated_at = excluded.updated_at
                """,
                (
                    symbol.upper(),
                    session,
                    interval_minutes,
                    bucket_at,
                    price,
                    price,
                    price,
                    price,
                    quantity,
                    updated_at,
                ),
            )
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT *
                FROM trade_candles
                WHERE symbol = ?
                  AND session = ?
                  AND interval_minutes = ?
                  AND bucket_at = ?
                """,
                (symbol.upper(), session, interval_minutes, bucket_at),
            ).fetchone()
        if row is None:
            raise RuntimeError("체결 캔들을 저장하지 못했습니다.")
        return _row_to_dict(row)

    def list_trade_ticks(self, limit: int = 20) -> list[dict[str, Any]]:
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM trade_ticks ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ticks = [_row_to_dict(row) for row in rows]
        for tick in ticks:
            tick["raw"] = json.loads(tick.pop("raw_json"))
        return ticks

    def list_trade_candles(
        self,
        *,
        symbol: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM trade_candles"
        params: list[Any] = []
        if symbol:
            query += " WHERE symbol = ?"
            params.append(symbol.upper())
        query += " ORDER BY bucket_at DESC LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, params).fetchall()
        return [_row_to_dict(row) for row in rows]

    def previous_trade_candle_volumes(
        self,
        *,
        symbol: str,
        session: str,
        interval_minutes: int,
        before_bucket_at: str,
        limit: int,
    ) -> list[str]:
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            rows = connection.execute(
                """
                SELECT volume
                FROM trade_candles
                WHERE symbol = ?
                  AND session = ?
                  AND interval_minutes = ?
                  AND bucket_at < ?
                ORDER BY bucket_at DESC
                LIMIT ?
                """,
                (
                    symbol.upper(),
                    session,
                    interval_minutes,
                    before_bucket_at,
                    limit,
                ),
            ).fetchall()
        return [str(row[0]) for row in rows]

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS overseas_account_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                environment TEXT NOT NULL,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                currency TEXT NOT NULL,
                quote_price TEXT NOT NULL,
                balance_json TEXT NOT NULL,
                buying_power_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS order_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                order_type TEXT NOT NULL,
                limit_price TEXT NOT NULL,
                currency TEXT NOT NULL,
                exchange TEXT NOT NULL,
                estimated_amount TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS risk_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                order_intent_id INTEGER NOT NULL,
                decision TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                checks_json TEXT NOT NULL,
                FOREIGN KEY(order_intent_id) REFERENCES order_intents(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS broker_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                order_intent_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                order_type TEXT NOT NULL,
                limit_price TEXT NOT NULL,
                currency TEXT NOT NULL,
                exchange TEXT NOT NULL,
                estimated_amount TEXT NOT NULL,
                status TEXT NOT NULL,
                kis_order_id TEXT,
                FOREIGN KEY(order_intent_id) REFERENCES order_intents(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                order_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES broker_orders(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                order_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price TEXT NOT NULL,
                currency TEXT NOT NULL,
                fee TEXT NOT NULL,
                fill_signature TEXT,
                raw_json TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES broker_orders(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                symbol TEXT NOT NULL UNIQUE,
                quote_exchange TEXT NOT NULL,
                account_exchange TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                hot_up_pct TEXT NOT NULL,
                hot_down_pct TEXT NOT NULL,
                action_on_hot_up TEXT NOT NULL,
                action_on_hot_down TEXT NOT NULL,
                limit_offset_pct TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                price TEXT NOT NULL,
                quote_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS market_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                order_id INTEGER,
                FOREIGN KEY(order_id) REFERENCES broker_orders(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS orderbook_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                tr_key TEXT NOT NULL,
                session TEXT NOT NULL,
                bid_price TEXT NOT NULL,
                ask_price TEXT NOT NULL,
                bid_quantity TEXT NOT NULL,
                ask_quantity TEXT NOT NULL,
                total_bid_quantity TEXT NOT NULL,
                total_ask_quantity TEXT NOT NULL,
                spread TEXT NOT NULL,
                pressure_score TEXT NOT NULL,
                raw_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                tr_key TEXT NOT NULL,
                session TEXT NOT NULL,
                local_date TEXT NOT NULL,
                local_time TEXT NOT NULL,
                korea_date TEXT NOT NULL,
                korea_time TEXT NOT NULL,
                price TEXT NOT NULL,
                quantity TEXT NOT NULL,
                cumulative_volume TEXT NOT NULL,
                cumulative_amount TEXT NOT NULL,
                sell_trade_quantity TEXT NOT NULL,
                buy_trade_quantity TEXT NOT NULL,
                execution_strength TEXT NOT NULL,
                raw_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                session TEXT NOT NULL,
                interval_minutes INTEGER NOT NULL,
                bucket_at TEXT NOT NULL,
                open_price TEXT NOT NULL,
                high_price TEXT NOT NULL,
                low_price TEXT NOT NULL,
                close_price TEXT NOT NULL,
                volume TEXT NOT NULL,
                trade_count INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(symbol, session, interval_minutes, bucket_at)
            )
            """
        )
        _ensure_column(connection, "fills", "fill_signature", "TEXT")
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_signature
            ON fills(fill_signature)
            WHERE fill_signature IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_market_snapshots_symbol_id
            ON market_snapshots(symbol, id)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_orderbook_snapshots_symbol_id
            ON orderbook_snapshots(symbol, id)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trade_ticks_symbol_id
            ON trade_ticks(symbol, id)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trade_candles_symbol_bucket
            ON trade_candles(symbol, bucket_at)
            """
        )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_type: str,
) -> None:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    if column_name not in {str(row[1]) for row in rows}:
        connection.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
        )
