#!/usr/bin/env bash
# 서버에서 실행: planner(LLM 판단기) 전용 Codex 인증을 shared/secrets/codex-planner에 만든다.
#   ~/ai-manager/current/deploy/planner-login.sh                     # 기기 코드 로그인(ChatGPT 계정)
#   ~/ai-manager/current/deploy/planner-login.sh --api-key < key.txt # API 키를 표준입력으로
#   ~/ai-manager/current/deploy/planner-login.sh --status            # 로그인 상태만 확인
#   위 명령에 --enable을 붙이면 성공 후 deploy.env에 planner를 켜고 다시 배포한다.
# 개인 ~/.codex는 복사하지 않는다. API 키는 인자·환경변수로 받지 않는다(셸 기록·프로세스 목록 노출 방지).
set -euo pipefail
# deploy.sh와 같은 비root UID로 실행한다.
AI_MANAGER_UID=$(id -u)
AI_MANAGER_GID=$(id -g)
export AI_MANAGER_UID AI_MANAGER_GID

deploy_root=${AI_MANAGER_DEPLOY_ROOT:-$HOME/ai-manager}
app=$deploy_root/current
secrets=$deploy_root/shared/secrets/codex-planner
mode=device
enable=0
for arg in "$@"; do
    case $arg in
        --api-key) mode=api-key ;;
        --status) mode=status ;;
        --enable) enable=1 ;;
        -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "알 수 없는 옵션: $arg" >&2; exit 2 ;;
    esac
done

[[ -d $app ]] || { echo "배포된 릴리스가 없습니다: $app (먼저 reports를 배포하세요)" >&2; exit 1; }
[[ -d $deploy_root/shared/secrets ]] || { echo "서버 공유 디렉터리가 없습니다: $deploy_root/shared/secrets" >&2; exit 1; }
if [[ $mode == api-key && -t 0 ]]; then
    echo "API 키는 표준입력으로 전달하세요: $0 --api-key < key.txt" >&2
    exit 2
fi

cd "$app"
compose=(docker compose -f docker-compose.reports.yml -f docker-compose.planner.yml)
codex_file=(--entrypoint codex planner -c 'cli_auth_credentials_store="file"')

umask 077
mkdir -p "$secrets"
chmod 700 "$secrets"

if [[ $mode != status ]]; then
    "${compose[@]}" build planner
    if [[ $mode == api-key ]]; then
        "${compose[@]}" run --rm --no-deps -T "${codex_file[@]}" login --with-api-key
    else
        echo "브라우저에 표시된 주소에서, 방금 이 서버가 요청한 로그인 코드만 입력하세요."
        "${compose[@]}" run --rm --no-deps "${codex_file[@]}" login --device-auth
    fi
    # planner는 배포 사용자 UID로 실행되므로 자기 파일의 권한을 직접 정리할 수 있다.
    "${compose[@]}" run --rm --no-deps -T --entrypoint sh planner \
        -c 'chmod 700 /home/agent/.codex && chmod 600 /home/agent/.codex/auth.json'
fi
"${compose[@]}" run --rm --no-deps -T --entrypoint codex planner login status

if (( enable )); then
    env_file=$deploy_root/shared/deploy.env
    stacks="reports planner"
    if [[ -f $env_file ]] && grep -q '^AI_MANAGER_DEPLOY_STACKS=' "$env_file"; then
        stacks=$(sed -n 's/^AI_MANAGER_DEPLOY_STACKS=//p' "$env_file" | tail -1 | tr -d "\"'")
        [[ " $stacks " == *" reports "* ]] || stacks="$stacks reports"
        [[ " $stacks " == *" planner "* ]] || stacks="$stacks planner"
        sed -i "s/^AI_MANAGER_DEPLOY_STACKS=.*/AI_MANAGER_DEPLOY_STACKS=\"${stacks# }\"/" "$env_file"
    else
        printf 'AI_MANAGER_DEPLOY_STACKS="%s"\n' "$stacks" >> "$env_file"
    fi
    echo "deploy.env: AI_MANAGER_DEPLOY_STACKS=\"${stacks# }\" — 현재 커밋을 다시 배포합니다."
    "$deploy_root/bin/deploy.sh"
fi
