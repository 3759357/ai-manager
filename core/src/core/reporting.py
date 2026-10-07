"""Read-only account reports; all amounts remain in their original currency."""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.events import utc_now_iso


def number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    try:
        result = Decimal(str(value).replace(",", "").strip())
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def decimal_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def position(row: dict, broker: str, domestic: bool = False) -> dict:
    if broker == "toss":
        symbol, currency = row.get("symbol"), row.get("currency")
        qty, price = number(row.get("quantity")), number(row.get("lastPrice"))
        market = row.get("marketValue", {})
        market = market if isinstance(market, dict) else {}
        value, cost = number(market.get("amount")), number(market.get("purchaseAmount"))
    else:
        symbol = row.get("pdno") if domestic else row.get("ovrs_pdno")
        currency = "KRW" if domestic else row.get("tr_crcy_cd", "USD")
        qty = number(row.get("hldg_qty") if domestic else row.get("ovrs_cblc_qty"))
        price = number(row.get("prpr") if domestic else row.get("now_pric2"))
        value = number(row.get("evlu_amt") if domestic else row.get("ovrs_stck_evlu_amt"))
        cost = number(row.get("pchs_amt") if domestic else row.get("frcr_pchs_amt1"))
    if not symbol or not re.fullmatch(r"[A-Z]{3}", str(currency)) or qty is None or qty < 0:
        raise ValueError("invalid position identity/quantity")
    if price is not None and price < 0:
        price = None
    if value is not None and value < 0:
        value = None
    if value is None and price is not None:
        value = qty * price
    if cost is not None and cost < 0:
        cost = None
    return dict(broker=broker, symbol=str(symbol), currency=currency,
                quantity=decimal_text(qty), price=decimal_text(price),
                market_value=decimal_text(value), purchase_amount=decimal_text(cost),
                unrealized_pnl=decimal_text(value - cost if value is not None and cost is not None else None))


def recent_order(row: dict, broker: str) -> dict:
    """Normalize read-only broker history without treating an order as a fill."""
    if broker == "toss":
        execution = row.get("execution") or {}
        if not isinstance(execution, dict):
            raise ValueError("invalid execution")
        symbol = row.get("symbol")
        side = str(row.get("side") or "").upper()
        quantity = number(row.get("quantity"))
        raw_filled = execution.get("filledQuantity")
        filled = number(raw_filled) if raw_filled is not None else Decimal(0)
        price = number(execution.get("averageFilledPrice"))
        amount = number(execution.get("filledAmount"))
        commission = number(execution.get("commission"))
        status = str(row.get("status") or "UNKNOWN").upper()
        ordered_at = str(row.get("orderedAt") or "")
        currency = str(row.get("currency") or "")
        order_id = str(row.get("orderId") or "")
    else:
        symbol = row.get("pdno") or row.get("ovrs_pdno")
        side = {"01": "SELL", "02": "BUY"}.get(str(row.get("sll_buy_dvsn_cd") or row.get("sll_buy_dvsn") or ""), "")
        quantity = number(row.get("ft_ord_qty"))
        raw_filled = row.get("ft_ccld_qty")
        filled = number(raw_filled) if raw_filled is not None else Decimal(0)
        price = number(row.get("ft_ccld_unpr3"))
        amount = number(row.get("ft_ccld_amt3"))
        commission = None
        remaining = number(row.get("nccs_qty"))
        cancel = str(row.get("rvse_cncl_dvsn_name") or row.get("rvse_cncl_dvsn_cd") or "")
        if "취소" in cancel or cancel in {"Y", "02"}:
            status = "CANCELED"
        elif filled > 0 and remaining == 0:
            status = "FILLED"
        elif filled > 0:
            status = "PARTIALLY_FILLED"
        else:
            status = "OPEN" if remaining is not None and remaining > 0 else "UNKNOWN"
        date = str(row.get("ord_dt") or "")
        ordered_at = f"{date[:4]}-{date[4:6]}-{date[6:8]}" if re.fullmatch(r"\d{8}", date) else ""
        currency = str(row.get("tr_crcy_cd") or "USD")
        order_id = str(row.get("odno") or "")
    if not order_id or not symbol or side not in {"BUY", "SELL"} or quantity is None or quantity <= 0 or filled is None or filled < 0 or filled > quantity or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("invalid broker order")
    if status == "FILLED" and filled == 0:
        raise ValueError("filled order missing execution quantity")
    if any(value is not None and value < 0 for value in (price, amount, commission)):
        raise ValueError("invalid broker execution")
    return dict(broker=broker, order_id=order_id, symbol=str(symbol), side=side,
                status=status, currency=currency, quantity=decimal_text(quantity),
                filled_quantity=decimal_text(filled), average_filled_price=decimal_text(price),
                filled_amount=decimal_text(amount), commission=decimal_text(commission),
                ordered_at=ordered_at)


def collect_report(report_id: str, brokers: str, client_factory=None) -> dict:
    """Independent source failures produce an explicitly partial report."""
    report = dict(schema_version=1, report_id=report_id, collected_from=utc_now_iso(),
                  positions=[], buying_power=[], recent_orders=[], sources=[], warnings=[])
    factory = client_factory or make_client
    for broker in (("kis", "toss") if brokers == "both" else (brokers,)):
        try:
            client = factory(broker)
        except Exception:
            report["sources"].append(dict(source=broker, status="unavailable"))
            report["warnings"].append(f"{broker}: 연결 설정을 확인하세요. 계좌 전체를 확인하지 못했습니다.")
            continue
        scopes = [("holdings", client.holdings, False)] if broker == "toss" else [
            ("KR", client.balance, True), ("NASD/USD", client.overseas_balance, False)]
        for scope, fetch, domestic in scopes:
            source = dict(source=f"{broker}:{scope}", status="ok", collected_at=utc_now_iso())
            report["sources"].append(source)
            try:
                raw = fetch()
                rows = raw["items" if broker == "toss" else "positions"]
                if not isinstance(rows, list):
                    raise ValueError("invalid positions")
                for row in rows:
                    try:
                        parsed = position(row, broker, domestic)
                        report["positions"].append(parsed)
                        if parsed["market_value"] is None:
                            source["status"] = "partial"
                    except (ValueError, TypeError, AttributeError):
                        source["status"] = "partial"
                if any(str(v).strip() for v in raw.get("context", {}).values()) or raw.get("hasNext"):
                    source["status"] = "partial"
            except Exception:
                source["status"] = "unavailable"
            if source["status"] != "ok":
                report["warnings"].append(f"{source['source']}: 일부 데이터가 누락되었거나 추가 페이지 확인이 필요합니다.")
        if broker == "toss":
            for currency in ("KRW", "USD"):
                try:
                    raw = client.buying_power(currency)
                    amount = number(raw.get("cashBuyingPower"))
                    if amount is None or raw.get("currency", currency) != currency:
                        raise ValueError("invalid buying power")
                    report["buying_power"].append(dict(broker=broker, currency=currency, amount=decimal_text(amount)))
                except Exception:
                    report["warnings"].append(f"toss:{currency} 주문가능금액 조회 실패")
        try:
            if broker == "toss":
                batches = [client.orders(status=status, limit=20) for status in ("OPEN", "CLOSED")]
                if any(not isinstance(batch.get("orders"), list) for batch in batches):
                    raise ValueError("invalid order list")
                rows = [row for batch in batches for row in batch["orders"]]
            else:
                end = datetime.now(ZoneInfo("Asia/Seoul")).date()
                start = end - timedelta(days=7)
                rows = client.overseas_pending_orders() + client.overseas_order_history(
                    start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
                if not isinstance(rows, list):
                    raise ValueError("invalid order list")
            source = dict(source=f"{broker}:recent_orders", status="ok", collected_at=utc_now_iso())
            report["sources"].append(source)
            deduped = {}
            for row in rows:
                try:
                    parsed = recent_order(row, broker)
                    key = (parsed["ordered_at"], parsed["order_id"]) if broker == "kis" else parsed["order_id"]
                    deduped[key] = parsed
                except (ValueError, TypeError, AttributeError):
                    source["status"] = "partial"
            report["recent_orders"].extend(deduped.values())
            if source["status"] != "ok":
                report["warnings"].append(f"{broker}:recent_orders 일부 주문을 해석하지 못했습니다.")
        except Exception:
            report["sources"].append(dict(source=f"{broker}:recent_orders", status="unavailable"))
            report["warnings"].append(f"{broker}:recent_orders 조회 실패. 주문 내역이 빠졌습니다.")
    report["recent_orders"].sort(key=lambda row: row["ordered_at"], reverse=True)
    totals: dict[str, Decimal] = {}
    for p in report["positions"]:
        value = number(p["market_value"])
        if value is not None:
            totals[p["currency"]] = totals.get(p["currency"], Decimal(0)) + value
    report["market_value_by_currency"] = {k: decimal_text(v) for k, v in sorted(totals.items())}
    report["collected_to"] = utc_now_iso()
    report["status"] = "partial" if report["warnings"] else "ok"
    report["scope_note"] = (
        "수집된 보유주식 평가금액입니다. 현금·미체결·기타 자산과 환산 총자산은 포함하지 않습니다. "
        "주문가능금액은 현금잔고와 다릅니다. KIS는 국내 및 NASD/USD 조회 범위이며 "
        "국내 잔고의 추가 페이지 여부는 현재 클라이언트에서 확인할 수 없습니다. "
        "수집 시간은 시세 기준 시각이 아니며 실시간 가격을 보장하지 않습니다."
    )
    report["order_scope_note"] = (
        "토스 주문은 미체결·종료 주문 각각 최근 20건, KIS는 미체결 및 최근 7일 NASD/USD 주문만 조회합니다. "
        "전체 거래·입출금 원장이 아니며, 주문 접수와 실제 체결을 구분합니다. "
        "주문 내역을 보유 잔고에 다시 더하거나 빼지 않습니다."
    )
    return report


def make_client(broker: str):
    # Only API clients: no order gateway or order ledger is constructed.
    if broker == "toss":
        from toss.client import TossClient
        from toss.config import Settings
        return TossClient(Settings.from_env())
    from ki.client import KISClient
    from ki.config import Settings
    return KISClient(Settings.from_env())


def summary(report: dict) -> str:
    totals = ", ".join(f"{k} {v}" for k, v in report["market_value_by_currency"].items()) or "확인된 평가금액 없음"
    return (f"AI Manager 계좌 보고 · {report['report_id']}\n"
            f"수집 완료: {report['collected_to']}\n"
            f"조회 상태: {report['status']} · 보유 항목 {len(report['positions'])}개\n"
            f"조회된 주식 평가액: {totals}\n"
            f"최근 브로커 주문: {len(report.get('recent_orders', []))}건 (조회 범위 제한)\n"
            f"주의 사항: {len(report['warnings'])}개\n"
            "현금·미체결·기타 자산 제외. 통화 간 합산 없음.\n"
            "상세: 서버의 동일 보고서 ID에 HTML / Markdown / JSON 저장")


def markdown(report: dict) -> str:
    def safe(value):
        text = html.escape(str(value if value is not None else "미확인"))
        return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text).replace("\n", " ").replace("\r", " ")
    lines = ["# AI Manager 계좌 보고", "", safe(summary(report)), "", safe(report["scope_note"]), "",
             "| 브로커 | 종목 | 통화 | 수량 | 평가액 | 평가손익 |", "|---|---|---|---:|---:|---:|"]
    for p in report["positions"]:
        lines.append("| " + " | ".join(safe(p[k]) for k in ("broker", "symbol", "currency", "quantity", "market_value", "unrealized_pnl")) + " |")
    lines += ["", "## 주문가능금액 (현금잔고 아님)", ""]
    lines += [safe(f"{p['broker']} {p['currency']} {p['amount']}") for p in report["buying_power"]]
    lines += ["", "## 최근 브로커 주문", "", safe(report["order_scope_note"]), "",
              "| 브로커 | 종목 | 방향 | 상태 | 통화 | 주문수량 | 체결수량 | 체결가 | 수수료 |",
              "|---|---|---|---|---|---:|---:|---:|---:|"]
    for order in report["recent_orders"]:
        lines.append("| " + " | ".join(safe(order[k]) for k in
                     ("broker", "symbol", "side", "status", "currency", "quantity", "filled_quantity", "average_filled_price", "commission")) + " |")
    lines += ["", "## 조회 범위 및 주의 사항", ""]
    lines += ["- " + safe(f"{s['source']}: {s['status']}") for s in report["sources"]]
    lines += ["- " + safe(w) for w in report["warnings"]]
    return "\n".join(lines) + "\n"


def write_artifacts(root: Path, report: dict) -> dict[str, str]:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", report["report_id"]):
        raise ValueError("invalid report ID")
    target = root / report["report_id"]
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    md = markdown(report)
    def safe(value):
        return html.escape(str(value if value is not None else "미확인"))
    rows = "".join("<tr>" + "".join(f"<td>{safe(p[k])}</td>" for k in (
        "broker", "symbol", "currency", "quantity", "market_value", "unrealized_pnl")) + "</tr>"
        for p in report["positions"])
    warnings = "".join(f"<li>{safe(w)}</li>" for w in report["warnings"])
    sources = "".join(f"<li>{safe(s['source'])}: {safe(s['status'])}</li>" for s in report["sources"])
    power = "".join(f"<li>{safe(p['broker'])} · {safe(p['currency'])} {safe(p['amount'])}</li>" for p in report["buying_power"])
    orders = "".join("<li>" + " · ".join(safe(order[k]) for k in
                     ("broker", "symbol", "side", "status", "filled_quantity", "average_filled_price", "currency")) + "</li>"
                     for order in report["recent_orders"])
    page = ('<!doctype html><html lang="ko"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
            '<title>AI Manager 계좌 보고</title><style>body{max-width:960px;margin:32px auto;padding:0 18px;'
            'font:16px/1.7 system-ui;background:#101824;color:#e2e8f0}pre{white-space:pre-wrap;overflow-wrap:anywhere}'
            '.table{overflow-x:auto}table{border-collapse:collapse;width:100%}td,th{padding:10px;text-align:right;border-bottom:1px solid #334155}'
            'th{white-space:nowrap}h1,h2{color:#7dd3fc}</style>'
            '<body><h1>AI Manager 계좌 보고</h1><pre>' + safe(summary(report)) + '</pre><p>' + safe(report["scope_note"]) + '</p>'
            '<div class="table"><table><thead><tr><th>브로커</th><th>종목</th><th>통화</th><th>수량</th><th>평가액</th><th>평가손익</th></tr></thead>'
            '<tbody>' + rows + '</tbody></table></div><h2>주문가능금액 (현금잔고 아님)</h2><ul>' + power + '</ul>'
            '<h2>최근 브로커 주문</h2><p>' + safe(report["order_scope_note"]) + '</p><ul>' + orders + '</ul>'
            '<h2>조회 범위 및 주의 사항</h2><ul>' + sources + warnings + '</ul></body></html>')
    result = {}
    for ext, content in {"json": json.dumps(report, ensure_ascii=False, indent=2), "md": md, "html": page}.items():
        path = target / f"report.{ext}"
        temporary = target / f".{ext}.tmp"
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        result[ext] = str(path.resolve())
    return result
