#!/usr/bin/env bash
# 스풀 폴링 루프.
#   requests/<id>.json  을 읽어 agent-run 에 넣고
#   responses/<id>.json 또는 responses/<id>.error 를 쓴다.
#
# core 컨테이너에 docker.sock 을 주지 않기 위한 구조다.
# 워커와 에이전트 사이에 네트워크가 없어도 동작한다.
set -uo pipefail
umask 077

SPOOL="${SPOOL_DIR:-/spool}"
INTERVAL="${POLL_INTERVAL:-1}"
WORK="${WORK_DIR:-/tmp/mind}"

mkdir -p "$SPOOL/requests" "$SPOOL/responses" "$WORK"
echo "[mind] ${AGENT_CLI} 게이트웨이 시작. spool=${SPOOL}" >&2

while true; do
  for request in "$SPOOL"/requests/*.json; do
    [ -e "$request" ] || continue

    id="$(basename "$request" .json)"
    case "$id" in .*) continue ;; esac
    [ -e "$SPOOL/responses/$id.json" ] && continue
    [ -e "$SPOOL/responses/$id.error" ] && continue

    # 처리 중 표시로 옮긴다. mv 가 실패하면 다른 루프가 가져간 것이므로 건너뛴다.
    claimed="$WORK/$id.json"
    mv "$request" "$claimed" 2>/dev/null || continue

    # 역할은 요청이 들고 온다. 워커가 프로필을 갈아끼울 수 있게 한다.
    role="$(jq -r '._role // empty' < "$claimed")"
    [ -n "$role" ] || role="${AGENT_ROLE:-reviewer}"

    if body="$(AGENT_ROLE="$role" agent-run < "$claimed" 2>"$WORK/$id.err")"; then
      printf '%s' "$body" > "$SPOOL/responses/.$id.tmp"
      mv "$SPOOL/responses/.$id.tmp" "$SPOOL/responses/$id.json"
      echo "[mind] ok $id ($role)" >&2
    else
      head -c 2000 "$WORK/$id.err" > "$SPOOL/responses/.$id.tmp"
      mv "$SPOOL/responses/.$id.tmp" "$SPOOL/responses/$id.error"
      echo "[mind] fail $id ($role)" >&2
    fi

    rm -f "$claimed" "$WORK/$id.err"
  done
  sleep "$INTERVAL"
done
