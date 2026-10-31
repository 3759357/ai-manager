from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from toss.killswitch import resolve_kill_switch_path


BASE_URL = "https://openapi.tossinvest.com"
REAL_TRADING_ACK = "I_UNDERSTAND_TOSS_REAL_TRADING"


def _load_dotenv(path: Path = Path(".env")) -> None:
    path = _resolve_dotenv(path)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _resolve_dotenv(path: Path) -> Path:
    if path.exists() or path.name != ".env":
        return path
    configured = _env("AI_MANAGER_ENV_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[3] / "core/.env"


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None:
            return value.strip()
    return default


def _as_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_decimal(value: str | None, default: str) -> Decimal:
    try:
        return Decimal((value or default).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError("금액 설정은 숫자여야 합니다.") from exc


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    account_seq: int | None = None
    allow_orders: bool = False
    real_trading_ack: str = ""
    max_order_amount_usd: Decimal = Decimal("500")
    max_order_amount_krw: Decimal = Decimal("500000")
    max_slippage_pct: Decimal = Decimal("0.10")
    timeout_seconds: float = 10.0
    candidate_ttl_seconds: int = 1800
    database_path: Path = Path("data/toss.db")
    base_url: str = BASE_URL
    # core와 같은 중앙 Kill Switch 파일.
    kill_switch_path: Path | None = None

    @classmethod
    def from_env(cls, dotenv_path: Path = Path(".env")) -> "Settings":
        dotenv_path = _resolve_dotenv(dotenv_path)
        _load_dotenv(dotenv_path)
        account_seq_text = os.getenv("TOSS_ACCOUNT_SEQ", "").strip()
        database_path = Path(
            _env("TOSS_DB_PATH", "TOSSPILOT_DB_PATH", default="data/toss.db")
        ).expanduser()
        if not database_path.is_absolute():
            database_path = (dotenv_path.resolve().parent / database_path).resolve()
        settings = cls(
            client_id=os.getenv("TOSS_CLIENT_ID", "").strip(),
            client_secret=os.getenv("TOSS_CLIENT_SECRET", "").strip(),
            account_seq=int(account_seq_text) if account_seq_text else None,
            allow_orders=_as_bool(os.getenv("TOSS_ALLOW_ORDERS")),
            real_trading_ack=os.getenv("TOSS_REAL_TRADING_ACK", "").strip(),
            max_order_amount_usd=_as_decimal(
                os.getenv("TOSS_MAX_ORDER_AMOUNT_USD"),
                "500",
            ),
            max_order_amount_krw=_as_decimal(
                os.getenv("TOSS_MAX_ORDER_AMOUNT_KRW"),
                "500000",
            ),
            max_slippage_pct=_as_decimal(
                os.getenv("TOSS_MAX_SLIPPAGE_PCT"),
                "0.10",
            ),
            timeout_seconds=float(os.getenv("TOSS_TIMEOUT_SECONDS", "10")),
            candidate_ttl_seconds=int(os.getenv("TOSS_CANDIDATE_TTL_SECONDS", "1800")),
            database_path=database_path,
            kill_switch_path=resolve_kill_switch_path(
                _env("CORE_KILL_SWITCH_PATH"),
                base=dotenv_path.resolve().parent,
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.client_id or not self.client_secret:
            raise ValueError("TOSS_CLIENT_ID와 TOSS_CLIENT_SECRET을 설정하세요.")
        if self.account_seq is not None and self.account_seq <= 0:
            raise ValueError("TOSS_ACCOUNT_SEQ는 1 이상의 정수여야 합니다.")
        if self.max_order_amount_usd <= 0 or self.max_order_amount_krw <= 0:
            raise ValueError("주문 한도는 0보다 커야 합니다.")
        if self.candidate_ttl_seconds <= 0:
            raise ValueError("TOSS_CANDIDATE_TTL_SECONDS는 1 이상이어야 합니다.")
        if self.max_slippage_pct < 0:
            raise ValueError("TOSS_MAX_SLIPPAGE_PCT는 0 이상이어야 합니다.")

    def require_account(self) -> int:
        if self.account_seq is None:
            raise ValueError(
                "계좌 API에는 TOSS_ACCOUNT_SEQ가 필요합니다. "
                "먼저 `toss accounts`로 조회하세요."
            )
        return self.account_seq

    def require_order_permission(self, *, execute: bool) -> None:
        if not execute:
            raise PermissionError("실제 주문에는 --execute 옵션이 필요합니다.")
        if not self.allow_orders:
            raise PermissionError("TOSS_ALLOW_ORDERS=true 설정이 필요합니다.")
        if self.real_trading_ack != REAL_TRADING_ACK:
            raise PermissionError(
                f"실전 주문에는 TOSS_REAL_TRADING_ACK={REAL_TRADING_ACK} "
                "설정이 필요합니다."
            )
