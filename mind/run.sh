#!/usr/bin/env bash
# 무상태 단발 호출.
#   stdin  : 요청 JSON (MarketEvent + PositionSummary + RiskBudget)
#   stdout : 스키마를 통과한 결과 JSON 한 덩어리
#   stderr : 진행 로그
# 세션을 유지하지 않는다. 컨텍스트 오염과 재현 불가를 막기 위함.
set -euo pipefail

ROLE="${AGENT_ROLE:-strategist}"
PROMPT_FILE="/prompts/${ROLE}.md"
SCHEMA_FILE="/schema/${ROLE}.schema.json"

[[ -f "$PROMPT_FILE" ]] || { echo "프롬프트 없음: $PROMPT_FILE" >&2; exit 78; }
[[ -f "$SCHEMA_FILE" ]] || { echo "스키마 없음: $SCHEMA_FILE" >&2; exit 78; }

INPUT="$(cat)"
[[ -n "$INPUT" ]] || { echo "빈 입력" >&2; exit 78; }

case "${AGENT_CLI}" in
  codex)
    # --sandbox read-only : 기본값이지만 명시한다
    # --ignore-user-config: 호스트 설정 무시. 머신이 달라도 같은 결과
    # --output-schema     : 스키마 강제. 프롬프트로 JSON 을 부탁하지 않는다
    output="$(mktemp)"
    trap 'rm -f "$output"' EXIT
    codex exec \
      --sandbox read-only \
      --ephemeral --skip-git-repo-check \
      --output-schema "$SCHEMA_FILE" \
      --output-last-message "$output" \
      "$(cat "$PROMPT_FILE")

<input>
${INPUT}
</input>" >&2
    cat "$output"
    ;;

  claude)
    # --bare 는 쓰지 않는다. 구독 로그인을 무시하고 ANTHROPIC_API_KEY 를 요구한다.
    #   대신 빈 작업 디렉터리 + CLAUDE.md 부재로 컨텍스트를 비운다.
    # --permission-mode dontAsk : 허용 규칙 밖은 전부 거부
    # --allowedTools ""         : 도구 없음. 순수 텍스트 in/out
    # --json-schema             : 구조화 출력 → .structured_output
    printf '%s' "$INPUT" | claude -p "$(cat "$PROMPT_FILE")" \
      --model "${AGENT_MODEL:-sonnet}" \
      --output-format json \
      --json-schema "$(cat "$SCHEMA_FILE")" \
      --permission-mode dontAsk \
      --allowedTools "" \
    | jq -ce '.structured_output'
    ;;

  *)
    echo "AGENT_CLI 는 codex 또는 claude 여야 한다: ${AGENT_CLI:-<빈값>}" >&2
    exit 78
    ;;
esac
