from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


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
    return Path(__file__).resolve().parents[2] / ".env"


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None:
            return value.strip()
    return default


def _decimal(value: str | None, default: str) -> Decimal:
    try:
        return Decimal((value or default).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError("CORE 설정값은 숫자여야 합니다.") from exc


def _optional_decimal(value: str) -> Decimal | None:
    """비어 있으면 None(정책 파일 값을 쓴다)."""
    return _decimal(value, "0") if value.strip() else None


# 배수를 적지 않은 레버리지 종목은 3배로 가정한다(모르면 보수적으로).
DEFAULT_LEVERAGE_MULTIPLIER = Decimal("3")


def _leveraged(value: str) -> tuple[tuple[str, Decimal], ...]:
    """'SOXL:3,TQQQ:3,MUU' → ((SOXL, 3), (TQQQ, 3), (MUU, 3))."""
    out: list[tuple[str, Decimal]] = []
    for item in value.split(","):
        symbol, _, multiplier = item.strip().partition(":")
        if not symbol.strip():
            continue
        factor = _decimal(multiplier, "0") if multiplier.strip() else DEFAULT_LEVERAGE_MULTIPLIER
        if factor <= 1:
            raise ValueError("CORE_LEVERAGED_SYMBOLS의 배수는 1보다 커야 합니다. 예: SOXL:3")
        out.append((symbol.strip().upper(), factor))
    return tuple(out)


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _path(value: str | None, default: str, *, base: Path) -> Path:
    path = Path(value or default).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


@dataclass(frozen=True)
class Settings:
    default_broker: str = "toss"
    max_quote_divergence_pct: Decimal = Decimal("0.20")
    # 리스크 한도는 정책 파일(policy/v1.toml)에서 온다. 아래 값은 설정했을 때만
    # 정책보다 엄격한 쪽으로 반영한다(None = 정책 그대로).
    min_cash_pct: Decimal | None = None
    max_leveraged_exposure_pct: Decimal | None = None
    max_single_position_pct: Decimal | None = None
    max_daily_new_buy_usd: Decimal | None = None
    panic_buy_block_minutes: int = 30
    core_database_path: Path = Path("data/core.db")
    kill_switch_path: Path = Path("data/kill-switch.json")
    notify_channels: tuple[str, ...] = ("console",)
    leveraged: tuple[tuple[str, Decimal], ...] = ()
    strategy_spool_path: Path = Path("data/spool")
    strategy_role: str = "reviewer"
    strategy_cli: str = "codex"
    strategy_model: str = "gpt-5-codex"
    strategy_prompt_path: Path = Path("../mind/prompts/reviewer.md")
    strategy_prompt_version: str = "reviewer-v1"
    strategy_timeout_seconds: float = 120.0
    strategy_cache_ttl_minutes: int = 30
    strategy_band_pct: Decimal = Decimal("10")
    strategy_max_calls_per_window: int = 20
    strategy_window_hours: int = 5
    strategy_max_calls_per_day: int = 60
    strategy_min_interval_seconds: int = 30
    strategy_reserve_calls: int = 3
    discord_webhook_url: str = ""
    discord_username: str = "ai-manager"
    kis_environment: str = "demo"
    kis_app_key: str = ""
    kis_app_secret: str = ""
    kis_account_no: str = ""
    kis_account_product_code: str = "01"
    kis_allow_orders: bool = False
    kis_real_trading_ack: str = ""
    kis_max_order_amount: int = 100_000
    kis_max_overseas_order_amount_usd: Decimal = Decimal("500")
    kis_timeout_seconds: float = 10.0
    kis_database_path: Path = Path("../ki/data/ki.db")
    toss_client_id: str = ""
    toss_client_secret: str = ""
    toss_account_seq: int | None = None
    toss_allow_orders: bool = False
    toss_real_trading_ack: str = ""
    toss_max_order_amount_usd: Decimal = Decimal("500")
    toss_max_order_amount_krw: Decimal = Decimal("500000")
    toss_max_slippage_pct: Decimal = Decimal("0.10")
    toss_timeout_seconds: float = 10.0
    toss_database_path: Path = Path("../toss/data/toss.db")

    @classmethod
    def from_env(cls, dotenv_path: Path = Path(".env")) -> "Settings":
        dotenv_path = _resolve_dotenv(dotenv_path)
        _load_dotenv(dotenv_path)
        base = dotenv_path.resolve().parent
        toss_account_seq = os.getenv("TOSS_ACCOUNT_SEQ", "").strip()
        settings = cls(
            default_broker=os.getenv(
                "CORE_DEFAULT_BROKER",
                os.getenv("TRADEPILOT_DEFAULT_BROKER", "toss"),
            ).strip().lower(),
            max_quote_divergence_pct=_decimal(
                _env(
                    "CORE_MAX_QUOTE_DIVERGENCE_PCT",
                    "TRADEPILOT_MAX_QUOTE_DIVERGENCE_PCT",
                ),
                "0.20",
            ),
            min_cash_pct=_optional_decimal(
                _env("CORE_MIN_CASH_PCT", "TRADEPILOT_MIN_CASH_PCT"),
            ),
            max_leveraged_exposure_pct=_optional_decimal(
                _env(
                    "CORE_MAX_LEVERAGED_EXPOSURE_PCT",
                    "TRADEPILOT_MAX_LEVERAGED_EXPOSURE_PCT",
                ),
            ),
            max_single_position_pct=_optional_decimal(
                _env(
                    "CORE_MAX_SINGLE_POSITION_PCT",
                    "TRADEPILOT_MAX_SINGLE_POSITION_PCT",
                ),
            ),
            max_daily_new_buy_usd=_optional_decimal(
                _env(
                    "CORE_MAX_DAILY_NEW_BUY_USD",
                    "TRADEPILOT_MAX_DAILY_NEW_BUY_USD",
                ),
            ),
            panic_buy_block_minutes=int(
                _env(
                    "CORE_PANIC_BUY_BLOCK_MINUTES",
                    "TRADEPILOT_PANIC_BUY_BLOCK_MINUTES",
                    default="30",
                )
            ),
            core_database_path=_path(
                _env("CORE_DATABASE_PATH", "CORE_DB_PATH"),
                "data/core.db",
                base=base,
            ),
            kill_switch_path=_path(
                _env("CORE_KILL_SWITCH_PATH"),
                "data/kill-switch.json",
                base=base,
            ),
            notify_channels=tuple(
                channel.strip().lower()
                for channel in _env(
                    "CORE_NOTIFY_CHANNELS",
                    default="console",
                ).split(",")
                if channel.strip()
            ),
            leveraged=_leveraged(_env("CORE_LEVERAGED_SYMBOLS")),
            strategy_spool_path=_path(
                _env("CORE_STRATEGY_SPOOL_PATH"),
                "data/spool",
                base=base,
            ),
            strategy_role=_env(
                "CORE_STRATEGY_ROLE", default="reviewer"
            ).strip().lower(),
            strategy_cli=_env(
                "CORE_STRATEGY_CLI", default="codex"
            ).strip().lower(),
            strategy_model=_env(
                "CORE_STRATEGY_MODEL", default="gpt-5-codex"
            ).strip(),
            strategy_prompt_path=_path(
                _env("CORE_STRATEGY_PROMPT_PATH"),
                "../mind/prompts/reviewer.md",
                base=base,
            ),
            strategy_prompt_version=_env(
                "CORE_STRATEGY_PROMPT_VERSION", default="reviewer-v1"
            ).strip(),
            strategy_timeout_seconds=float(
                _env("CORE_STRATEGY_TIMEOUT_SECONDS", default="120")
            ),
            strategy_cache_ttl_minutes=int(
                _env("CORE_STRATEGY_CACHE_TTL_MINUTES", default="30")
            ),
            strategy_band_pct=_decimal(
                _env("CORE_STRATEGY_BAND_PCT"), "10"
            ),
            strategy_max_calls_per_window=int(
                _env("CORE_STRATEGY_MAX_CALLS_PER_WINDOW", default="20")
            ),
            strategy_window_hours=int(
                _env("CORE_STRATEGY_WINDOW_HOURS", default="5")
            ),
            strategy_max_calls_per_day=int(
                _env("CORE_STRATEGY_MAX_CALLS_PER_DAY", default="60")
            ),
            strategy_min_interval_seconds=int(
                _env("CORE_STRATEGY_MIN_INTERVAL_SECONDS", default="30")
            ),
            strategy_reserve_calls=int(
                _env("CORE_STRATEGY_RESERVE_CALLS", default="3")
            ),
            discord_webhook_url=os.getenv(
                "CORE_DISCORD_WEBHOOK_URL",
                os.getenv("TRADEPILOT_DISCORD_WEBHOOK_URL", ""),
            ).strip(),
            discord_username=os.getenv(
                "CORE_DISCORD_USERNAME",
                os.getenv("TRADEPILOT_DISCORD_USERNAME", "ai-manager"),
            ).strip(),
            kis_environment=os.getenv("KIS_ENV", "demo").strip().lower(),
            kis_app_key=os.getenv("KIS_APP_KEY", "").strip(),
            kis_app_secret=os.getenv("KIS_APP_SECRET", "").strip(),
            kis_account_no=os.getenv("KIS_ACCOUNT_NO", "").strip(),
            kis_account_product_code=os.getenv(
                "KIS_ACCOUNT_PRODUCT_CODE",
                "01",
            ).strip(),
            kis_allow_orders=_bool(os.getenv("KIS_ALLOW_ORDERS")),
            kis_real_trading_ack=os.getenv("KIS_REAL_TRADING_ACK", "").strip(),
            kis_max_order_amount=int(
                os.getenv("KIS_MAX_ORDER_AMOUNT", "100000")
            ),
            kis_max_overseas_order_amount_usd=_decimal(
                os.getenv("KIS_MAX_OVERSEAS_ORDER_AMOUNT_USD"),
                "500",
            ),
            kis_timeout_seconds=float(
                os.getenv("KIS_TIMEOUT_SECONDS", "10")
            ),
            kis_database_path=_path(
                _env("KI_DB_PATH", "KISPILOT_DB_PATH"),
                "../ki/data/ki.db",
                base=base,
            ),
            toss_client_id=os.getenv("TOSS_CLIENT_ID", "").strip(),
            toss_client_secret=os.getenv("TOSS_CLIENT_SECRET", "").strip(),
            toss_account_seq=(
                int(toss_account_seq) if toss_account_seq else None
            ),
            toss_allow_orders=_bool(os.getenv("TOSS_ALLOW_ORDERS")),
            toss_real_trading_ack=os.getenv(
                "TOSS_REAL_TRADING_ACK",
                "",
            ).strip(),
            toss_max_order_amount_usd=_decimal(
                os.getenv("TOSS_MAX_ORDER_AMOUNT_USD"),
                "500",
            ),
            toss_max_order_amount_krw=_decimal(
                os.getenv("TOSS_MAX_ORDER_AMOUNT_KRW"),
                "500000",
            ),
            toss_max_slippage_pct=_decimal(
                os.getenv("TOSS_MAX_SLIPPAGE_PCT"),
                "0.10",
            ),
            toss_timeout_seconds=float(
                os.getenv("TOSS_TIMEOUT_SECONDS", "10")
            ),
            toss_database_path=_path(
                _env("TOSS_DB_PATH", "TOSSPILOT_DB_PATH"),
                "../toss/data/toss.db",
                base=base,
            ),
        )
        settings.validate()
        return settings

    @property
    def leveraged_symbols(self) -> tuple[str, ...]:
        return tuple(symbol for symbol, _ in self.leveraged)

    @property
    def leverage_multipliers(self) -> dict[str, Decimal]:
        return dict(self.leveraged)

    def validate(self) -> None:
        if self.default_broker not in {"kis", "toss"}:
            raise ValueError("CORE_DEFAULT_BROKER는 kis 또는 toss여야 합니다.")
        if self.max_quote_divergence_pct < 0:
            raise ValueError(
                "CORE_MAX_QUOTE_DIVERGENCE_PCT는 0 이상이어야 합니다."
            )
        for name, value in (
            ("CORE_MIN_CASH_PCT", self.min_cash_pct),
            ("CORE_MAX_LEVERAGED_EXPOSURE_PCT", self.max_leveraged_exposure_pct),
            ("CORE_MAX_SINGLE_POSITION_PCT", self.max_single_position_pct),
            ("CORE_MAX_DAILY_NEW_BUY_USD", self.max_daily_new_buy_usd),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name}는 0 이상이어야 합니다.")
        if self.panic_buy_block_minutes < 0:
            raise ValueError(
                "CORE_PANIC_BUY_BLOCK_MINUTES는 0 이상이어야 합니다."
            )
        if not self.notify_channels:
            raise ValueError("CORE_NOTIFY_CHANNELS는 비워둘 수 없습니다.")
        for channel in self.notify_channels:
            if channel not in {"console", "discord"}:
                raise ValueError(
                    "CORE_NOTIFY_CHANNELS는 console, discord만 지원합니다."
                )
        if "discord" in self.notify_channels and not self.discord_webhook_url:
            raise ValueError(
                "discord 채널을 쓰려면 CORE_DISCORD_WEBHOOK_URL이 필요합니다."
            )
        if self.discord_webhook_url and not self.discord_webhook_url.startswith(
            ("https://", "http://")
        ):
            raise ValueError("CORE_DISCORD_WEBHOOK_URL은 URL이어야 합니다.")
        if not self.discord_username:
            raise ValueError("CORE_DISCORD_USERNAME은 비워둘 수 없습니다.")
        if self.strategy_cli not in {"codex", "claude"}:
            raise ValueError("CORE_STRATEGY_CLI는 codex 또는 claude여야 합니다.")
        if not self.strategy_role:
            raise ValueError("CORE_STRATEGY_ROLE은 비워둘 수 없습니다.")
        if self.strategy_timeout_seconds <= 0:
            raise ValueError(
                "CORE_STRATEGY_TIMEOUT_SECONDS는 0보다 커야 합니다."
            )
        if self.strategy_cache_ttl_minutes < 0:
            raise ValueError(
                "CORE_STRATEGY_CACHE_TTL_MINUTES는 0 이상이어야 합니다."
            )
        if self.strategy_band_pct <= 0:
            raise ValueError("CORE_STRATEGY_BAND_PCT는 0보다 커야 합니다.")
        if self.strategy_reserve_calls >= self.strategy_max_calls_per_window:
            raise ValueError(
                "CORE_STRATEGY_RESERVE_CALLS는 "
                "CORE_STRATEGY_MAX_CALLS_PER_WINDOW보다 작아야 합니다."
            )
        if self.kis_environment not in {"demo", "real"}:
            raise ValueError("KIS_ENV는 demo 또는 real이어야 합니다.")
        if self.toss_account_seq is not None and self.toss_account_seq <= 0:
            raise ValueError("TOSS_ACCOUNT_SEQ는 1 이상의 정수여야 합니다.")
