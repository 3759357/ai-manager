#!/usr/bin/env bash
# 서버 배포 실행기. post-receive 훅이 main push마다 호출한다.
#
# 배포할 서비스 묶음은 서버 설정 $deploy_root/shared/deploy.env 의 AI_MANAGER_DEPLOY_STACKS로 고른다.
#   reports  : 보고·지시·판단 기록 워커 (docker-compose.reports.yml, 프로젝트 ai-manager-reports)
#   planner  : reports + Codex 계획기 (docker-compose.planner.yml)
#   trading  : 시세 수집·대사·알림 (docker-compose.yml, 프로젝트 ai-manager)
# 기본값은 "reports planner". trading은 명시해야만 기동한다.
set -euo pipefail
umask 077
unset COMPOSE_PROFILES COMPOSE_FILE
# 컨테이너는 root가 아니라 배포 사용자 UID로 실행한다(Compose의 user 값).
AI_MANAGER_UID=$(id -u)
AI_MANAGER_GID=$(id -g)
export AI_MANAGER_UID AI_MANAGER_GID
deploy_root=${AI_MANAGER_DEPLOY_ROOT:-$HOME/ai-manager}
deploy_repo=${AI_MANAGER_DEPLOY_REPO:-$HOME/repos/ai-manager.git}
mkdir -p "$deploy_root/releases" "$deploy_root/logs"
exec 9>"$deploy_root/deploy.lock"
flock 9
# Resolve after acquiring the lock so queued pushes cannot deploy stale commits.
revision=$(git --git-dir="$deploy_repo" rev-parse --verify refs/heads/main^{commit})
subject=$(git --git-dir="$deploy_repo" log -1 --format=%s "$revision")
log_file="$deploy_root/logs/$revision.log"
exec > >(tee -a "$log_file") 2>&1

stage=start
notify() {
    # 선택: shared/core/.env 의 CORE_DISCORD_WEBHOOK_URL 로 결과를 보낸다. 계좌 정보는 보내지 않는다.
    [[ ${AI_MANAGER_DEPLOY_NOTIFY:-1} == 1 ]] || return 0
    local url
    url=$(sed -n 's/^CORE_DISCORD_WEBHOOK_URL=//p' "$deploy_root/shared/core/.env" 2>/dev/null | tail -1 | tr -d "\"'")
    [[ $url == https://discord.com/api/webhooks/* || $url == https://discordapp.com/api/webhooks/* ]] || return 0
    local body
    body=$(python3 -c 'import json,sys; print(json.dumps({"username": "ai-manager-deploy", "content": sys.argv[1][:1900]}, ensure_ascii=False))' "$1") || return 0
    curl -fsS -m 10 -H 'Content-Type: application/json' -d "$body" "$url" >/dev/null || true
}
on_exit() {
    local code=$?
    if [[ $code -eq 0 ]]; then
        notify "✅ AI Manager 배포 성공 ${revision:0:7} · ${subject} · 묶음: ${stacks[*]:-?}"
    else
        notify "❌ AI Manager 배포 실패 ${revision:0:7} · 단계: ${stage} · ${subject}
로그: ${log_file}"
    fi
}
trap on_exit EXIT

echo "Deploying $revision ($subject)"
[[ -s $deploy_root/shared/core/.env ]] || {
    echo "Missing server configuration: $deploy_root/shared/core/.env" >&2
    exit 1
}
AI_MANAGER_DEPLOY_STACKS="reports planner"
if [[ -f $deploy_root/shared/deploy.env ]]; then
    # shellcheck disable=SC1091
    source "$deploy_root/shared/deploy.env"
fi
read -r -a stacks <<<"$AI_MANAGER_DEPLOY_STACKS"
want() { local s; for s in "${stacks[@]}"; do [[ $s == "$1" ]] && return 0; done; return 1; }
for s in "${stacks[@]}"; do
    [[ $s == reports || $s == planner || $s == trading ]] || { echo "Unknown stack: $s" >&2; exit 1; }
done
if want planner && ! want reports; then stacks+=(reports); fi
echo "Stacks: ${stacks[*]}"

stage=prepare
docker info >/dev/null
docker compose version
release=$(mktemp -d "$deploy_root/releases/$revision.XXXXXX")
git --git-dir="$deploy_repo" archive "$revision" | tar -x -C "$release"
cd "$release"

stage=build
# Test images contain source only. Server credentials are linked after builds.
docker build -f core/Dockerfile -t "ai-manager-core:$revision" .
stage=test
docker run --rm --network none --entrypoint sh "ai-manager-core:$revision" -ec '
    for package in ki toss core; do
        python -m unittest discover -s "$package/tests" -q
    done
'
stage=build
if want trading; then docker build -t "ai-manager-ki:$revision" ki; fi
if want planner; then docker build -f mind/Dockerfile.codex -t "ai-manager-mind-codex:$revision" mind; fi

stage=link
for package in core ki toss; do
    [[ ! -e $package/data && ! -L $package/data ]] || {
        echo "Runtime data must not be tracked: $package/data" >&2; exit 1;
    }
    ln -s "$deploy_root/shared/$package/data" "$package/data"
done
[[ ! -e core/.env && ! -L core/.env ]] || {
    echo 'core/.env must not be tracked.' >&2; exit 1;
}
ln -s "$deploy_root/shared/core/.env" core/.env
ln -s "$deploy_root/shared/spool" spool
ln -s "$deploy_root/shared/secrets" secrets
cat > deploy-images.yml <<EOF
services:
  notification:
    image: ai-manager-core:$revision
  ki-reconcile:
    image: ai-manager-ki:$revision
  ki-market-data:
    image: ai-manager-ki:$revision
EOF
{
    echo "services:"
    echo "  report-worker:"
    echo "    image: ai-manager-core:$revision"
    if want planner; then
        echo "  planner:"
        echo "    image: ai-manager-mind-codex:$revision"
    fi
} > deploy-reports-images.yml
printf '%s\n' "${stacks[@]}" > deploy-stacks.txt

stage=permissions
# 예전 root 컨테이너가 만든 파일까지 배포 사용자 소유로 맞춘다. 같은 이미지를 한 번만 root로,
# 네트워크 없이 실행한다. APNs 키 디렉터리와 KIS 토큰 캐시도 미리 만들어 둔다.
mkdir -p "$HOME/.cache/ki"
docker run --rm --network none --user 0 --entrypoint sh \
    -v "$deploy_root/shared:/shared" -v "$HOME/.cache/ki:/kicache" \
    "ai-manager-core:$revision" -ec '
        mkdir -p /shared/secrets/apns /shared/spool /shared/core/data/manager
        for path in /shared/core/data /shared/ki/data /shared/toss/data /shared/spool /shared/secrets /kicache; do
            if [ -e "$path" ]; then chown -R "$1:$2" "$path"; fi
        done
    ' sh "$AI_MANAGER_UID" "$AI_MANAGER_GID"
if compgen -G "$deploy_root/shared/secrets/*.p8" >/dev/null; then
    echo "주의: APNs 키(.p8)는 shared/secrets/apns/로 옮기고 AI_MANAGER_APNS_KEY_PATH를 /run/ai-manager-secrets/apns/<파일>로 바꾸세요." >&2
fi

compose_trading() {
    docker compose --project-name ai-manager --project-directory "$1" \
        -f "$1/docker-compose.yml" -f "$1/deploy-images.yml" "${@:2}"
}
compose_reports() {
    local files=(-f "$1/docker-compose.reports.yml")
    if [[ -f $1/deploy-stacks.txt ]] && grep -qx planner "$1/deploy-stacks.txt"; then
        files+=(-f "$1/docker-compose.planner.yml")
    fi
    docker compose --project-name ai-manager-reports --project-directory "$1" \
        "${files[@]}" -f "$1/deploy-reports-images.yml" "${@:2}"
}

stage=config
if want trading; then compose_trading "$release" config --quiet; fi
compose_reports "$release" config --quiet
previous=$(readlink -f "$deploy_root/current" 2>/dev/null || true)

restore() {
    echo 'Startup failed.' >&2
    if [[ -n $previous && -f $previous/deploy-reports-images.yml ]]; then
        echo "Restoring reports stack from $previous"
        compose_reports "$previous" up -d --no-build --remove-orphans --wait --wait-timeout 180 || true
    fi
    if want trading; then
        if [[ -n $previous && -f $previous/deploy-images.yml ]]; then
            echo "Restoring trading stack from $previous"
            compose_trading "$previous" up -d --no-build --remove-orphans --wait --wait-timeout 180 || true
        else
            compose_trading "$release" down || true
        fi
    fi
    exit 1
}

stage=startup
if want trading; then
    compose_trading "$release" up -d --no-build --remove-orphans --wait --wait-timeout 180 || restore
fi
compose_reports "$release" up -d --no-build --remove-orphans --wait --wait-timeout 180 || restore
stage=smoke
# 새 코드가 영속 DB를 열고 명령을 처리하는지 확인한다(조회 전용).
compose_reports "$release" exec -T report-worker ai-manager thesis list --json >/dev/null || restore

stage=switch
ln -sfn "$release" "$deploy_root/current.next"
mv -Tf "$deploy_root/current.next" "$deploy_root/current"
# 배포 스크립트와 훅도 릴리스 버전으로 갱신한다. mv는 실행 중인 파일을 덮어쓰지 않는다.
install_tool() {
    local src=$1 dst=$2
    [[ -f $src ]] || return 0
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst.next" && chmod 0755 "$dst.next" && mv -f "$dst.next" "$dst"
}
install_tool "$release/deploy/deploy.sh" "$deploy_root/bin/deploy.sh"
install_tool "$release/deploy/post-receive" "$deploy_repo/hooks/post-receive"
install_tool "$release/deploy/ai-manager" "$HOME/.local/bin/ai-manager"
stage=done
echo "DEPLOY OK: $revision"
if want trading; then compose_trading "$release" ps; fi
compose_reports "$release" ps
