from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ki.killswitch import resolve_kill_switch_path


DEMO_BASE_URL = "https://openapivts.koreainvestment.com:29443"
REAL_BASE_URL = "https://openapi.koreainvestment.com:9443"
REAL_TRADING_ACK = "I_UNDERSTAND_REAL_TRADING"


def _load_dotenv(path: Path = Path(".env")) -> None:
    path = _resolve_dotenv(path)
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


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
    environment: str
    app_key: str
    app_secret: str
    account_no: str
    account_product_code: str = "01"
    allow_orders: bool = False
    real_trading_ack: str = ""
    max_order_amount: int = 100_000
    max_overseas_order_amount_usd: Decimal = Decimal("500")
    timeout_seconds: float = 10.0
    database_path: Path = Path("data/ki.db")
    # core와 같은 중앙 Kill Switch 파일. None이면 아래 legacy 파일만 확인한다.
    kill_switch_path: Path | None = None

    @property
    def kill_switch_paths(self) -> tuple[Path | None, ...]:
        # 예전 ki 전용 스위치(ki DB 옆 KILL_SWITCH)도 계속 차단 신호로 인정한다.
        return (self.kill_switch_path, self.database_path.parent / "KILL_SWITCH")

    @classmethod
    def from_env(cls, dotenv_path: Path = Path(".env")) -> "Settings":
        dotenv_path = _resolve_dotenv(dotenv_path)
        _load_dotenv(dotenv_path)
        environment = os.getenv("KIS_ENV", "demo").strip().lower()
        database_path = Path(
            _env("KI_DB_PATH", "KISPILOT_DB_PATH", default="data/ki.db")
        ).expanduser()
        if not database_path.is_absolute():
            database_path = (dotenv_path.resolve().parent / database_path).resolve()
        settings = cls(
            environment=environment,
            app_key=os.getenv("KIS_APP_KEY", "").strip(),
            app_secret=os.getenv("KIS_APP_SECRET", "").strip(),
            account_no=os.getenv("KIS_ACCOUNT_NO", "").strip(),
            account_product_code=os.getenv(
                "KIS_ACCOUNT_PRODUCT_CODE", "01"
            ).strip(),
            allow_orders=_as_bool(os.getenv("KIS_ALLOW_ORDERS")),
            real_trading_ack=os.getenv("KIS_REAL_TRADING_ACK", "").strip(),
            max_order_amount=int(os.getenv("KIS_MAX_ORDER_AMOUNT", "100000")),
            max_overseas_order_amount_usd=_as_decimal(
                os.getenv("KIS_MAX_OVERSEAS_ORDER_AMOUNT_USD"),
                "500",
            ),
            timeout_seconds=float(os.getenv("KIS_TIMEOUT_SECONDS", "10")),
            database_path=database_path,
            kill_switch_path=resolve_kill_switch_path(
                _env("CORE_KILL_SWITCH_PATH"),
                base=dotenv_path.resolve().parent,
            ),
        )
        settings.validate()
        return settings

    @property
    def base_url(self) -> str:
        return REAL_BASE_URL if self.environment == "real" else DEMO_BASE_URL

    @property
    def is_real(self) -> bool:
        return self.environment == "real"

    def validate(self) -> None:
        if self.environment not in {"demo", "real"}:
            raise ValueError("KIS_ENV는 demo 또는 real이어야 합니다.")
        if not self.app_key or not self.app_secret:
            raise ValueError("KIS_APP_KEY와 KIS_APP_SECRET을 설정하세요.")
        if self.account_no and (
            len(self.account_no) != 8 or not self.account_no.isdigit()
        ):
            raise ValueError("KIS_ACCOUNT_NO는 계좌번호 앞 8자리여야 합니다.")
        if (
            len(self.account_product_code) != 2
            or not self.account_product_code.isdigit()
        ):
            raise ValueError("KIS_ACCOUNT_PRODUCT_CODE는 계좌번호 뒤 2자리입니다.")
        if self.max_order_amount <= 0:
            raise ValueError("KIS_MAX_ORDER_AMOUNT는 0보다 커야 합니다.")
        if self.max_overseas_order_amount_usd <= 0:
            raise ValueError("KIS_MAX_OVERSEAS_ORDER_AMOUNT_USD는 0보다 커야 합니다.")

    def require_account(self) -> None:
        if not self.account_no:
            raise ValueError("잔고 및 주문에는 KIS_ACCOUNT_NO가 필요합니다.")

    def require_order_permission(self, execute: bool) -> None:
        if not execute:
            raise PermissionError("실제 전송에는 --execute 옵션이 필요합니다.")
        if not self.allow_orders:
            raise PermissionError("KIS_ALLOW_ORDERS=true 설정이 필요합니다.")
        if self.is_real and self.real_trading_ack != REAL_TRADING_ACK:
            raise PermissionError(
                f"실전 주문에는 KIS_REAL_TRADING_ACK={REAL_TRADING_ACK} 설정이 필요합니다."
            )
