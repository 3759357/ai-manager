#!/usr/bin/env bash
# 인증이 살아 있는지 확인한다.
# 토큰이 만료되면 워커는 조용히 실패만 반복하므로 헬스체크로 드러낸다.
set -eu

case "${AGENT_CLI}" in
  codex)  test -s "${CODEX_HOME}/auth.json" ;;
  claude) test -s "${CLAUDE_CONFIG_DIR}/.credentials.json" ;;
  *) echo "AGENT_CLI 불명" >&2; exit 1 ;;
esac

test -d "${SPOOL_DIR:-/spool}/requests"
