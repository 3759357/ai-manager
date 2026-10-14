#!/usr/bin/env bash
# 처음 한 번만 Mac에서 실행: 서버에 새 배포 실행기·훅·ai-manager 명령·배포 설정을 설치한다.
# 이후에는 배포가 성공할 때마다 deploy.sh가 스스로 최신 버전으로 갱신된다.
#   ./deploy/install-server.sh                         # deploy/local.env의 AI_MANAGER_SERVER 사용
#   AI_MANAGER_SERVER=myhost ./deploy/install-server.sh  # 일회성 지정
set -euo pipefail
cd "$(dirname "$0")"
if [[ -z ${AI_MANAGER_SERVER:-} && -f local.env ]]; then
    AI_MANAGER_SERVER=$(sed -n 's/^AI_MANAGER_SERVER=//p' local.env | tail -1 | tr -d "\"'")
fi
host=${AI_MANAGER_SERVER:?"서버를 지정하세요: cp deploy/local.env.example deploy/local.env 후 AI_MANAGER_SERVER 입력"}
put() { # 로컬파일 원격경로 : 임시 파일로 보낸 뒤 이름을 바꿔 실행 중인 파일을 건드리지 않는다
    ssh "$host" "mkdir -p \"\$(dirname $2)\" && cat > $2.next && chmod 0755 $2.next && mv -f $2.next $2" < "$1"
}
put deploy.sh '~/ai-manager/bin/deploy.sh'
put post-receive '~/repos/ai-manager.git/hooks/post-receive'
put ai-manager '~/.local/bin/ai-manager'
ssh "$host" 'f=~/ai-manager/shared/deploy.env; if [ ! -f "$f" ]; then
  mkdir -p ~/ai-manager/shared
  printf "%s\n" "# 배포할 서비스 묶음: reports, planner, trading(시세·대사·알림)" "AI_MANAGER_DEPLOY_STACKS=\"reports planner\"" > "$f"
  echo "created $f"; else echo "kept $f"; fi; cat "$f"'
echo "설치 완료. server remote의 main에 push하면 서버 배포가 시작됩니다. 로컬 커밋 자동 push는 별도 훅 설정이 필요합니다."
