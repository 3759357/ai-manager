# 환경과 배포

명령은 저장소 루트를 기준으로 합니다. Python 3.11 이상과 Docker Compose v2가 필요합니다.
iPhone 빌드는 macOS·Xcode, 서버 배포 스크립트는 Ubuntu/Linux를 대상으로 합니다.

## 로컬 설치와 설정

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ki -e toss -e 'core[push]'
cp core/.env.example core/.env
export AI_MANAGER_ENV_PATH="$PWD/core/.env"
```

브로커별 `.env`를 중복 생성하지 않고 중앙 파일 하나에 본인의 값을 입력합니다.
환경변수는 `.env`보다 우선하며 외부 파일을 사용할 때는 DB 경로도 명시합니다.

| 기능 | 설정 |
| --- | --- |
| KIS 조회 | `KIS_ENV`, `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO`, 상품 코드 |
| Toss 조회 | `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`, `TOSS_ACCOUNT_SEQ` |
| 로컬 상태 | `AI_MANAGER_DATA_DIR`, `CORE_DATABASE_PATH`, `KI_DB_PATH`, `TOSS_DB_PATH` |
| Discord 선택 전송 | `CORE_DISCORD_WEBHOOK_URL`, `CORE_NOTIFY_CHANNELS` |
| APNs 선택 전송 | `AI_MANAGER_APNS_TEAM_ID`, `KEY_ID`, `KEY_PATH`, `TOPIC` (모두 동일 접두사) |
| LLM 계획기 | `AI_MANAGER_PLANNER_ENABLED`, planner 서비스의 별도 CLI 인증 |

API 키 없이 [테스트와 데모](demo.md)를 실행할 수 있습니다. API 인증 없이 실제 계좌 조회는 동작하지 않습니다.
`KIS_ALLOW_ORDERS=false`, `TOSS_ALLOW_ORDERS=false`의 기본값을 유지하면 개별 CLI 실주문을 허용하지 않습니다.
보고 Compose는 이 값을 false로 고정하고 실행 모드는 paper로 고정합니다.

## 보고·지시 서버

```sh
mkdir -p secrets
docker compose -f docker-compose.reports.yml up -d --build
docker compose -f docker-compose.reports.yml exec -T report-worker ai-manager reports --json
```

계좌 보고를 요청하면 worker가 처리해 서버에 원본을 저장합니다. 기본 모의 운용은 명시적으로 시작해야 합니다.
기존 시작·중지 상태는 재시작 뒤에도 보존합니다. Docker 기동 성공과 브로커 인증 성공은 따로 확인합니다.

LLM을 추가하려면 해당 CLI의 인증을 별도로 준비하고 설정 후 실행합니다.

```sh
docker compose -f docker-compose.reports.yml -f docker-compose.planner.yml up -d --build report-worker planner
```

계획기의 브로커 키·DB 접근은 분리되어 있습니다. `.env`만으로 LLM 로그인이나 Apple 서명이 대체되지 않습니다.

## iPhone 연결

1. [iOS 프로젝트](../ios/README.md)를 시뮬레이터 또는 자신의 서명으로 빌드합니다.
2. 앱에 서버 주소·포트·사용자·개인키를 등록합니다.
3. 이미 신뢰하는 SSH 경로에서 서버 호스트 공개키를 읽고 앱에 등록합니다.
4. 저장소가 기본 경로와 다르면 고급 설정에서 서버 프로젝트 경로를 바꿉니다.

```sh
ssh ai-manager-server 'cat /etc/ssh/ssh_host_ed25519_key.pub'
```

보고 화면은 SSH로 reports Compose를 호출합니다. SSH 계정에는 해당 경로 접근과 Docker 실행 권한이 필요합니다.
터미널은 별도 보고 worker 없이도 SSH 계정의 셸에 연결합니다.
시뮬레이터는 팀 ID가 필요 없고, 실기기 서명 설정은 `ios/Config/Local.xcconfig`에 둡니다.
공개본의 APNs topic도 본인의 번들 ID로 설정합니다.

## 선택: 서버 릴리스 배포

서버에 Docker, SSH 계정, `~/repos/ai-manager.git` bare 저장소와 프로젝트용 경로를 준비합니다.
`~/ai-manager/shared/core/.env`와 필요한 데이터·인증 경로를 운영자가 구성합니다.

```sh
AI_MANAGER_SERVER=ai-manager-server ./deploy/install-server.sh
git remote add server ai-manager-server:~/repos/ai-manager.git
git push server main
```

서버 post-receive가 테스트 후 릴리스 배포를 실행합니다. 공개본은 로컬 `core.hooksPath`를 자동 설정하지 않습니다.
평소 커밋을 서버로 자동 push하려는 운영자는 제공된 훅을 직접 검토하고 설정합니다.
기본 배포 묶음은 reports·planner이므로 planner 인증이 없으면 서버의 `shared/deploy.env`에서
`AI_MANAGER_DEPLOY_STACKS="reports"`를 선택합니다.
실패 시 실행 버전을 복구하며 영속 DB를 이전 시점으로 되돌리지는 않습니다.

## Git에 포함하지 않는 파일

`.env`, LLM 로그인 파일, APNs·SSH 개인키, 개인 서명 설정, DB·로그·스풀은 제외합니다.
설정 예제와 합성 fixture는 포함합니다. 이 공개본은 운영 저장소의 개인 기록과 과거 Git 이력을 가져오지 않습니다.
