from __future__ import annotations

import os
from pathlib import Path


DEPRECATED_KEYS = {
    "CORE_KIS_ENV_PATH",
    "CORE_TOSS_ENV_PATH",
}

KEY_ALIASES = {
    "TRADEPILOT_DEFAULT_BROKER": "CORE_DEFAULT_BROKER",
    "TRADEPILOT_MAX_QUOTE_DIVERGENCE_PCT": "CORE_MAX_QUOTE_DIVERGENCE_PCT",
    "TRADEPILOT_MIN_CASH_PCT": "CORE_MIN_CASH_PCT",
    "TRADEPILOT_MAX_LEVERAGED_EXPOSURE_PCT": "CORE_MAX_LEVERAGED_EXPOSURE_PCT",
    "TRADEPILOT_MAX_SINGLE_POSITION_PCT": "CORE_MAX_SINGLE_POSITION_PCT",
    "TRADEPILOT_MAX_DAILY_NEW_BUY_USD": "CORE_MAX_DAILY_NEW_BUY_USD",
    "TRADEPILOT_PANIC_BUY_BLOCK_MINUTES": "CORE_PANIC_BUY_BLOCK_MINUTES",
    "TRADEPILOT_DISCORD_WEBHOOK_URL": "CORE_DISCORD_WEBHOOK_URL",
    "TRADEPILOT_DISCORD_USERNAME": "CORE_DISCORD_USERNAME",
    "KISPILOT_DB_PATH": "KI_DB_PATH",
    "TOSSPILOT_DB_PATH": "TOSS_DB_PATH",
}


def _values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def main() -> None:
    repo = Path(__file__).resolve().parents[2]
    core_dir = repo / "core"
    target = core_dir / ".env"
    example = core_dir / ".env.example"
    sources = [target, repo / "ki/.env", repo / "toss/.env"]

    merged: dict[str, str] = {}
    for source in sources:
        for key, value in _values(source).items():
            if key not in DEPRECATED_KEYS:
                merged[KEY_ALIASES.get(key, key)] = value

    relative_database_paths = {
        "KI_DB_PATH": "../ki/data/ki.db",
        "TOSS_DB_PATH": "../toss/data/toss.db",
    }
    for key, core_relative_path in relative_database_paths.items():
        configured = merged.get(key, "")
        if not configured or configured.startswith("data/"):
            merged[key] = core_relative_path

    output: list[str] = []
    emitted: set[str] = set()
    for raw_line in example.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            output.append(raw_line)
            continue
        key, default = raw_line.split("=", 1)
        output.append(f"{key}={merged.get(key, default)}")
        emitted.add(key)

    extra_keys = sorted(set(merged) - emitted - DEPRECATED_KEYS - set(KEY_ALIASES))
    if extra_keys:
        output.extend(["", "# Additional settings"])
        output.extend(f"{key}={merged[key]}" for key in extra_keys)

    temporary = target.with_suffix(".env.tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)

    for source in sources[1:]:
        if source.exists():
            source.unlink()

    print("중앙 설정 이전 완료: core/.env")
    print("브로커별 .env 제거 완료")


if __name__ == "__main__":
    main()
