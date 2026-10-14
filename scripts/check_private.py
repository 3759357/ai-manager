#!/usr/bin/env python3
"""커밋·공개 전에 개인정보와 비밀 값을 검사한다.

    python3 scripts/check_private.py --staged   # 커밋될 내용(pre-commit 훅이 사용)
    python3 scripts/check_private.py --all      # 추적 중인 모든 파일(CI가 사용)
    python3 scripts/check_private.py FILE...    # 지정한 파일

다음 중 하나라도 있으면 실패한다. 결과에는 파일 이름과 종류만 나오고 값은 출력하지 않는다.
- 커밋하면 안 되는 경로: private/, 실제 설정 파일(.env, deploy/local.env, Local.xcconfig), 키·DB 파일
- 비밀 값 형식: 개인키, GitHub·OpenAI·AWS 토큰, Discord 웹훅, 사설 IP
- 내 로컬 설정 파일에 들어 있는 실제 값(예시 파일과 같은 자리표시 값은 제외)

오탐인 줄에는 `check-private: allow`를 적으면 그 줄은 내용 검사에서 제외한다.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    "github-token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "openai-key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    "aws-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "webhook": re.compile(r"https://(?:discord(?:app)?\.com)/api/webhooks/\d+/[A-Za-z0-9_-]{20,}"),
    "private-address": re.compile(r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"),
}
BLOCKED_NAMES = {".env", "local.env", "Local.xcconfig", "auth.json", ".credentials.json", "매매일지.md"}
BLOCKED_SUFFIXES = {".pem", ".key", ".p8", ".p12", ".pfx", ".mobileprovision", ".db", ".sqlite", ".sqlite3"}
SECRET_KEYS = re.compile(r"(?:APP_KEY|APP_SECRET|CLIENT_ID|CLIENT_SECRET|ACCOUNT_NO|ACCOUNT_SEQ|WEBHOOK_URL|TEAM_ID|KEY_ID|DEVELOPMENT_TEAM|API_KEY|TOKEN|PASSWORD)$")
# 실제 설정 파일 → 형식을 보여 주는 예시 파일
LOCAL_FILES = {
    "core/.env": "core/.env.example",
    "ki/.env": "ki/.env.example",
    "toss/.env": "toss/.env.example",
    "deploy/local.env": "deploy/local.env.example",
    "ios/Config/Local.xcconfig": "ios/Config/Local.xcconfig.example",
}
# 이 파일들은 키 이름과 상관없이 모든 값이 개인 값이다(서버 별칭, 번들 ID 등).
ALL_VALUES_PERSONAL = {"deploy/local.env", "ios/Config/Local.xcconfig"}
ALLOW_MARK = "check-private: allow"


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def assignments(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    pairs = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith(("#", "//")):
            continue
        line = re.sub(r"\s+(?:#|//).*$", "", line)
        key, sep, value = line.partition("=")
        if sep:
            pairs[key.strip().removeprefix("export ").strip()] = value.strip().strip("\"'")
    return pairs


def known_values(root: Path) -> set[str]:
    """로컬 설정 파일의 실제 값. 메모리에서만 쓰고 출력하지 않는다."""
    values = set()
    for name, example in LOCAL_FILES.items():
        placeholders = set(assignments(root / example).values())
        for key, value in assignments(root / name).items():
            personal = name in ALL_VALUES_PERSONAL or SECRET_KEYS.search(key)
            if personal and len(value) >= 4 and value not in placeholders:
                values.add(value)
    return values


def blocked_path(path: str) -> str | None:
    p = PurePosixPath(path)
    if p.parts and p.parts[0] == "private":
        return "private-folder"
    if p.name in BLOCKED_NAMES or (p.name.startswith(".env.") and p.name != ".env.example"):
        return "local-config-file"
    if p.suffix.lower() in BLOCKED_SUFFIXES:
        return "key-or-database-file"
    return None


def scan_text(text: str, known: set[str]) -> list[str]:
    hits: set[str] = set()
    known_patterns = [re.compile(rf"(?<![\w-]){re.escape(v)}(?![\w-])") for v in known]
    for line in text.splitlines():
        if ALLOW_MARK in line:
            continue
        hits.update(name for name, pattern in PATTERNS.items() if pattern.search(line))
        if any(p.search(line) for p in known_patterns):
            hits.add("known-local-value")
    return sorted(hits)


def check(root: Path, files: dict[str, bytes]) -> list[tuple[str, str]]:
    known = known_values(root)
    problems = []
    for path, data in sorted(files.items()):
        reason = blocked_path(path)
        if reason:
            problems.append((path, reason))
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue  # 이미지 등 바이너리
        problems.extend((path, hit) for hit in scan_text(text, known))
    return problems


def staged_files(root: Path) -> dict[str, bytes]:
    names = git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").decode().split("\0")
    return {n: git(root, "show", f":{n}") for n in names if n}


def tracked_files(root: Path) -> dict[str, bytes]:
    names = git(root, "ls-files", "-z").decode().split("\0")
    return {n: (root / n).read_bytes() for n in names if n and (root / n).is_file()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="커밋될 내용 검사")
    mode.add_argument("--all", action="store_true", help="추적 중인 모든 파일 검사")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
    if args.staged:
        files = staged_files(root)
    elif args.all:
        files = tracked_files(root)
    else:
        files = {str(Path(p).resolve().relative_to(root)): Path(p).read_bytes() for p in args.paths}
    problems = check(root, files)
    for path, kind in problems:
        print(f"개인정보 검사 실패: {path} ({kind})", file=sys.stderr)
    if problems:
        print("값은 출력하지 않습니다. 파일을 고치거나, 오탐인 줄에 'check-private: allow'를 적으세요.", file=sys.stderr)
        return 1
    print(f"개인정보 검사 통과: {len(files)}개 파일")
    return 0


if __name__ == "__main__":
    sys.exit(main())
