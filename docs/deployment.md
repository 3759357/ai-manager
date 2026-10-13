# 서버 설정·배포·자동화

로컬 실행은 Python 3.11 이상, 서버는 Ubuntu·Bash·Git·Docker Engine·Compose v2를 기준으로 합니다.
iPhone 빌드에는 macOS·Xcode가 필요합니다. 실제 인증값은 [환경변수·키 관리](configuration.md)를 따라 별도로 준비합니다.
명령 블록의 **로컬**은 개발 PC, **서버**는 SSH로 접속한 Ubuntu 셸입니다.

## 로컬에서 먼저 실행

**로컬 / 저장소 루트**:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ki -e toss -e 'core[push]'
test -f core/.env || cp core/.env.example core/.env
chmod 600 core/.env
export AI_MANAGER_ENV_PATH="$PWD/core/.env"
```

`core/.env`를 편집하고 보고 worker를 시작합니다. API 키가 없으면 [계좌 없는 데모](demo.md)를 먼저 사용합니다.

```sh
mkdir -p secrets
docker compose -f docker-compose.reports.yml config --quiet
docker compose -f docker-compose.reports.yml up -d --build
docker compose -f docker-compose.reports.yml exec -T report-worker ai-manager reports --json
```

처음에는 reports만 실행합니다. Compose는 브로커 실주문 허용을 `false`로 고정합니다.
매니저는 가상계좌나 자동 매매를 실행하지 않습니다.
계좌 조회 성공은 Docker 기동 성공과 별도로 확인해야 합니다.

## Ubuntu 서버 최초 설치

### 1. 기존 SSH 연결 확인

sudo 권한이 있는 서버 계정과 기존 SSH 인증을 사용합니다. 아래의 주소·사용자·키 경로는 본인의 값으로 바꿉니다.
이 가이드는 SSH 키를 새로 생성하지 않습니다. 로컬 `~/.ssh/config` 예시:

```sshconfig
Host ai-manager-server
    HostName YOUR_SERVER_ADDRESS
    User ubuntu
    IdentityFile ~/.ssh/YOUR_EXISTING_KEY
    IdentitiesOnly yes
```

**로컬**:

```sh
ssh ai-manager-server
```

서버에는 SSH 접속과 외부 HTTPS 통신이 필요합니다. 기본 Compose는 호스트에 HTTP 포트를 공개하지 않습니다.
SSH는 본인의 접속 경로에 맞게 접근을 제한하고, 서버 호스트 공개키를 확인합니다.

### 2. Docker 설치

**서버 / 새 설치 환경**:

```sh
sudo apt-get update
sudo apt-get install -y git python3
git clone https://github.com/3759357/ai-manager.git "$HOME/ai-manager-setup"
sudo bash "$HOME/ai-manager-setup/deploy/bootstrap-ubuntu.sh" "$USER"
```

설치 도구는 Ubuntu용 Docker 공식 저장소를 등록하고 Docker·Compose 플러그인을 설치한 뒤
서비스를 활성화합니다. 기존 Docker 환경에서는 설치 도구를 재실행하기 전에 현재 구성을 확인합니다.
`ai-manager-setup`은 최초 설치용 소스이며 실제 실행 경로는 이후의 `ai-manager/current`입니다.

docker 그룹 변경을 반영하려면 SSH에서 나갔다가 다시 접속합니다. **재접속한 서버 셸**에서 확인합니다.

```sh
docker info >/dev/null
docker compose version
```

docker 그룹에는 호스트의 높은 권한이 부여되므로 신뢰하는 운영 계정만 사용합니다.
설치 기준은 [Docker Ubuntu 설치](https://docs.docker.com/engine/install/ubuntu/)와
[Linux 설치 후 설정](https://docs.docker.com/engine/install/linux-postinstall/)을 참고합니다.

### 3. 영속 디렉터리와 설정 준비

기본 서버 레이아웃:

```text
~/repos/ai-manager.git/          # 서버 push를 받는 bare 저장소
~/ai-manager/
  bin/deploy.sh                 # 배포 실행기
  current -> releases/<revision>.<suffix>/
  releases/                    # 버전별 코드; 직접 수정하지 않음
  logs/<revision>.log          # 배포 로그
  shared/
    deploy.env                 # 배포할 서비스 묶음
    core/.env                  # 브로커·알림 설정
    core/data/manager/          # 보고·지시·판단 원장·LLM 스풀
    ki/data/                   # 브로커별 영속 경로
    toss/data/
    spool/                     # 별도 전략 게이트웨이 스풀
    secrets/                   # LLM 로그인·APNs 키
```

**서버 / 최초 준비**:

```bash
umask 077
mkdir -p "$HOME/repos" "$HOME/ai-manager/shared"/{core/data/manager,ki/data,toss/data,spool,secrets}
if [ ! -d "$HOME/repos/ai-manager.git" ]; then
    git init --bare "$HOME/repos/ai-manager.git"
fi
if [ ! -f "$HOME/ai-manager/shared/core/.env" ]; then
    cp "$HOME/ai-manager-setup/core/.env.example" "$HOME/ai-manager/shared/core/.env"
fi
if [ ! -f "$HOME/ai-manager/shared/deploy.env" ]; then
    printf '%s\n' 'AI_MANAGER_DEPLOY_STACKS="reports"' > "$HOME/ai-manager/shared/deploy.env"
fi
chmod 700 "$HOME/ai-manager/shared" "$HOME/ai-manager/shared/secrets"
chmod 600 "$HOME/ai-manager/shared/core/.env" "$HOME/ai-manager/shared/deploy.env"
nano "$HOME/ai-manager/shared/core/.env"
```

기존 파일은 유지합니다. 예제에서 사용할 브로커의 키와 계좌 식별값을 입력하고 실주문 허용은 꺼둡니다.
초기 `deploy.env`는 LLM 인증 없이도 설치할 수 있도록 **reports만** 선택합니다.
설치 도구 자체의 기본값은 `reports planner`지만, 먼저 만든 `deploy.env`가 있으면 그 설정을 유지합니다.

### 4. 서버 훅 설치와 첫 배포

**로컬 / 저장소 루트**:

```sh
AI_MANAGER_SERVER=ai-manager-server ./deploy/install-server.sh
git remote -v
# server remote가 없을 때 한 번만 등록
git remote add server ai-manager-server:~/repos/ai-manager.git
git push origin main
git push server main
```

기존 `server` remote가 있으면 주소를 확인하고 그대로 사용합니다. 설치 명령은 서버에 실행기·훅·CLI wrapper를 씁니다.
배포는 `server`의 `main`이 갱신될 때 시작합니다. 첫 배포는 이미지 다운로드·빌드 때문에 시간이 걸립니다.

서버는 순서대로 다음을 수행합니다.

1. `main`의 커밋을 새 릴리스 디렉터리에 풀고 이미지를 빌드합니다.
2. 인증정보를 연결하기 전에 네트워크 없는 컨테이너에서 KIS·Toss·Core 테스트를 실행합니다.
3. `shared`의 설정·영속 데이터·인증 경로를 연결하고 Compose 구성을 검증합니다.
4. 선택한 서비스를 기동하고 논리 카드 조회 명령으로 DB 접근을 확인합니다.
5. 성공하면 `current`를 전환하고 서버 배포 도구도 갱신합니다.

`DEPLOY OK`는 배포 절차의 성공입니다. 실제 브로커 인증·LLM 응답·푸시 수신까지 검증하는 단계는 아닙니다.

### 5. 계좌 조회 확인

**서버**:

```sh
cd "$HOME/ai-manager/current"
docker compose -f docker-compose.reports.yml ps
"$HOME/.local/bin/ai-manager" request '현재 계좌 보고' --broker toss --channel local
"$HOME/.local/bin/ai-manager" reports --json
"$HOME/.local/bin/ai-manager" report latest --format json
```

`--broker`는 준비한 계좌에 따라 `toss`, `kis`, `both`를 선택합니다. 요청은 비동기로 처리되므로
완료 전에는 잠시 뒤 다시 조회합니다. 특정 요청의 상태는 `ai-manager status <요청 ID>`로 확인합니다.
wrapper를 짧은 이름으로 쓰려면 SSH 셸의 PATH에 `~/.local/bin`을 추가합니다.

## 선택 기능 추가

### LLM 계획기

첫 reports 배포가 끝난 뒤 **서버**에서 한 줄로 인증과 활성화를 끝낼 수 있습니다.

```sh
~/ai-manager/current/deploy/planner-login.sh --enable
```

자세한 내용은 [planner 인증 절차](configuration.md#llm-계획기-인증)를 참고합니다.
수동으로 하려면 인증 후 **서버**의 `~/ai-manager/shared/deploy.env`를 편집합니다.

```dotenv
AI_MANAGER_DEPLOY_STACKS="reports planner"
```

**서버**에서 현재 커밋을 다시 배포합니다.

```sh
"$HOME/ai-manager/bin/deploy.sh"
cd "$HOME/ai-manager/current"
docker compose -f docker-compose.reports.yml -f docker-compose.planner.yml ps
```

planner Compose가 worker의 `AI_MANAGER_PLANNER_ENABLED`를 `true`로 덮어씁니다.
계획기는 브로커 키·계좌 DB·Docker 소켓을 마운트하지 않고, 전용 인증 디렉터리와 요청·응답 스풀을 사용합니다.
투자논리 AI 초안과 정기 판단 요청을 처리합니다. 기동 후에도 카드·판단 원장과 로그에서 실제 응답을 확인합니다.

### 매니저의 동작 범위

매니저는 계좌 보고, 지시 접수·이력, 투자논리 점검과 판단 기록을 관리합니다.
접수한 투자 지시는 자동 실행하지 않습니다. 처리 결과는 `record-instruction`으로 별도 기록할 수 있습니다.
가상계좌·가상 잔고·모의 체결 및 `engine`·`pilot` 명령은 제거했습니다.
기존 보고서·지시 이력과 과거 DB는 삭제하거나 실계좌 잔고로 전환하지 않습니다.
기존 DB에 남은 가상계좌 테이블은 사용하지 않으며 대기 중인 모의 체결 알림도 발송하지 않습니다.

`trading` 묶음은 별도의 시세·대사·알림 서비스를 뜻합니다. 해당 Compose에는 KIS 실전 환경 지정이 있으므로
브로커 모듈의 설정을 확인한 뒤 별도로 선택합니다. 기본 설치에 추가하지 않습니다.

## 재배포와 자동화

### 기본: 검토한 코드를 직접 배포

**로컬**에서 변경을 커밋한 뒤 GitHub와 서버에 각각 push합니다.

```sh
git push origin main
git push server main
```

`origin`은 공개 소스, `server`는 실행 환경의 배포 접수처입니다. GitHub 저장소에 push하는 것만으로
서버의 `post-receive`가 실행되지는 않습니다. GitHub Actions 기반 배포 워크플로는 현재 포함하지 않습니다.
기존 SSH 인증을 사용하며 GitHub에 서버 개인키를 올릴 필요가 없습니다.

### 선택: 로컬 main 커밋마다 서버로 자동 push

서버 remote와 수동 배포가 정상 동작하는 것을 확인한 뒤 **로컬**에서 활성화합니다.

```sh
git config core.hooksPath .githooks
git config ai-manager.autoDeploy true
```

이 설정은 해당 로컬 저장소에만 적용됩니다. 기존 훅 설정이 있다면 먼저 확인합니다.
제공된 `commit-msg` 훅도 적용되어 커밋 제목에 `docs:`, `feat:`, `fix:` 등의 형식을 요구합니다.
`post-commit`은 `main` 커밋 후 서버 push를 백그라운드로 실행합니다.
**GitHub push는 수행하지 않으며, GitHub 게시 전에 서버가 배포될 수 있습니다.**
GitHub에 반영된 코드만 배포하려면 위의 수동 순서를 사용합니다.

```sh
# 로컬 자동 배포 결과
tail -n 100 .git/ai-manager-deploy.log
# 한 번만 자동 배포 생략
AI_MANAGER_AUTO_DEPLOY=0 git commit -m "docs: update deployment guide"
# 자동 배포 계속 끄기; 서버에 직접 push하는 배포는 계속 가능
git config ai-manager.autoDeploy false
```

### 서버 재부팅 후 서비스 복구

설치 도구가 Docker 서비스를 활성화하고 reports·planner는 `restart: unless-stopped`를 사용합니다.
서버 재부팅이나 컨테이너 종료 후 자동 복구되지만, 운영자가 명시적으로 중지한 컨테이너는 다시 켜야 합니다.
이는 코드 자동 배포와 별개입니다. [Docker 재시작 정책](https://docs.docker.com/engine/containers/start-containers-automatically/)을 참고합니다.

## 운영 확인과 복구

**서버**에서 현재 버전과 worker 상태를 확인합니다.

```sh
readlink -f "$HOME/ai-manager/current"
git --git-dir="$HOME/repos/ai-manager.git" rev-parse main
cd "$HOME/ai-manager/current"
docker compose -f docker-compose.reports.yml ps
docker compose -f docker-compose.reports.yml logs --tail 100 report-worker
"$HOME/.local/bin/ai-manager" reports --json
```

planner 사용 시 로그 명령에 `-f docker-compose.planner.yml`을 추가하고 서비스 이름을 `planner`로 바꿉니다.
로그·보고서에는 개인 운용 정보가 있을 수 있으므로 공개 이슈에는 필요한 부분만 가려서 공유합니다.

| 증상 | 확인·조치 |
| --- | --- |
| Docker 권한 오류 | 그룹 변경 뒤 SSH 재접속, `docker info` 확인 |
| `Missing server configuration` | `shared/core/.env` 존재와 읽기 권한 확인 |
| 컨테이너는 실행 중인데 보고 실패 | 요청 상태·브로커 환경·계좌 식별값·API 권한 확인 |
| planner 인증 실패 | 전용 인증 경로, 로그인 상태, 인증 파일 쓰기 권한 확인 후 재로그인 |
| push 후 `DEPLOY FAILED` | 서버 `logs/<revision>.log`의 실패 단계 확인 후 배포 실행기 재실행 |
| 서버 Git 커밋과 `current` 버전이 다름 | 커밋 접수 후 배포 실패 가능; 실행 버전과 배포 로그 확인 |

서버 Git은 배포 실패 전에 이미 커밋을 접수할 수 있습니다. 같은 커밋을 다시 push하면
`Everything up-to-date`로 끝날 수 있으므로 설정·인증 문제를 고친 뒤 **서버**에서 재시도합니다.

```sh
"$HOME/ai-manager/bin/deploy.sh"
```

새 서비스 기동·조회 확인에 실패하면 이전 릴리스가 있는 경우 서비스 복구를 시도합니다.
첫 배포에는 복구할 이전 버전이 없으므로 남은 컨테이너와 로그를 직접 확인합니다.
배포 후 발견한 코드 문제는 로컬에서 문제 커밋을 `git revert`하여 새 커밋을 만들고 GitHub·서버에 push합니다.
`current`나 릴리스 내부 파일을 수동 수정하면 실행 이미지·서버 main과 어긋날 수 있습니다.

코드 복구는 DB·브로커에서 처리된 주문·전송된 알림을 되돌리지 않습니다. DB 변경 전에는
관련 writer를 중지한 일관된 스냅샷 또는 SQLite backup API로 백업하고 복원 가능 여부를 확인합니다.
`shared/`의 데이터·설정·키와 `~/.cache/ki`의 인증 캐시는 소스와 분리되어 있습니다.
백업에는 인증정보와 계좌 데이터가 포함될 수 있으므로 접근을 제한하고 저장소 밖에 암호화해 보관합니다.
GitHub는 소스 백업이며 이 데이터의 백업을 대신하지 않습니다.
오래된 릴리스·이미지·배포 로그의 자동 정리와 DB 백업 스케줄은 현재 제공하지 않습니다.

## 서버 이전

새 서버는 [최초 설치](#ubuntu-서버-최초-설치)대로 구성하고, 기존 서버의 `shared/`에서 운영 상태만 옮깁니다.
코드(`releases/`)와 서버 Git 저장소는 옮기지 않고 새로 push합니다.

| 옮기는 것 | 위치 | 비고 |
| --- | --- | --- |
| 보고·지시·투자논리·판단 원장 | `shared/core/data/manager/` | SQLite. 쓰기 중인 worker를 멈춘 뒤 복사 |
| 브로커 원장 | `shared/ki/data/`, `shared/toss/data/` | 주문·체결·대사 기록 |
| 설정 | `shared/core/.env`, `shared/deploy.env` | 권한 600 유지 |
| APNs 키 | `shared/secrets/apns.p8` | 권한 600 유지 |

| 옮기지 않는 것 | 이유 |
| --- | --- |
| `shared/secrets/codex-planner/` | 서버에 묶인 로그인 정보. 새 서버에서 `planner-login.sh`로 다시 로그인 |
| `~/.cache/ki` 토큰 캐시 | 새 서버에서 자동으로 다시 발급 |
| `releases/`, `current`, `logs/` | 새 서버에서 배포할 때 다시 생성 |

순서:

1. **기존 서버**에서 컨테이너를 멈춥니다. 복사 중 DB가 바뀌지 않게 하기 위해서입니다.
   ```sh
   cd "$HOME/ai-manager/current"
   docker compose -f docker-compose.reports.yml -f docker-compose.planner.yml stop
   ```
   `trading` 묶음도 쓰고 있다면 `docker compose -f docker-compose.yml stop`도 실행합니다.
2. 위 표의 파일을 새 서버의 같은 경로로 복사합니다. 계좌 데이터와 키가 들어 있으므로 SSH로만 옮기고 권한을 유지합니다.
3. 내 PC에서 `server` remote 주소를 새 서버로 바꾸고 `git push server main`으로 배포합니다.
4. LLM을 쓰면 **새 서버**에서 `~/ai-manager/current/deploy/planner-login.sh --enable`을 실행합니다.
5. `ai-manager reports --json`, `ai-manager thesis list`로 이전 기록이 보이는지 확인합니다.
6. iPhone 앱에 새 서버 주소와 [호스트 공개키](#iphone-연결)를 등록하고, 증권사 API에 허용 IP 조건이 있으면 새 IP로 바꿉니다.
7. 확인이 끝나면 기존 서버의 `shared/`를 안전하게 삭제하거나 암호화해 보관합니다. 두 서버를 동시에 켜 두면
   같은 계좌에 대한 작업과 알림이 중복될 수 있습니다.

## iPhone 연결

1. [iOS 프로젝트](../ios/README.md)를 시뮬레이터 또는 본인의 서명으로 빌드합니다.
2. 앱에 서버 주소·포트·사용자와 기존 SSH 개인키를 등록합니다.
3. 이미 신뢰하는 SSH 경로에서 서버 호스트 공개키를 읽고 앱에 등록합니다.
4. 서버 프로젝트 경로를 절대 경로로 설정합니다. 예: `/home/ubuntu/ai-manager/current`. 사용자 홈이 다르면 고급 설정에서 바꿉니다.

**로컬**에서 호스트 공개키를 확인합니다.

```sh
ssh ai-manager-server 'cat /etc/ssh/ssh_host_ed25519_key.pub'
```

앱 경로에는 `~`를 사용할 수 없습니다. 서버에서 `readlink -f "$HOME/ai-manager/current"`로 확인하되,
앱에는 버전별 `releases/...` 경로 대신 홈 아래의 `ai-manager/current` 절대 경로를 입력합니다.
앱 보고 화면은 SSH로 reports Compose를 호출합니다. 서버 계정에는 프로젝트 경로 접근과 Docker 권한이 필요합니다.
터미널은 해당 계정의 셸에 연결합니다. 실기기 서명 설정은 Git에서 제외된 `ios/Config/Local.xcconfig`에 둡니다.
APNs 사용 시 서버의 topic과 본인의 앱 번들 ID를 맞춥니다. 자세한 보관 위치는 [키 관리](configuration.md#인증정보의-보관과-교체)를 참고합니다.
