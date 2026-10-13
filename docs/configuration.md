# 환경변수·키 관리

설정의 출발점은 [`core/.env.example`](../core/.env.example)입니다. 계좌 없는 테스트에는 키가 필요하지 않습니다.
계좌 조회에는 사용할 브로커의 인증정보가 필요하며 LLM·Discord·APNs는 선택 기능입니다.
KIS의 `demo`는 증권사가 제공하는 별도 API 환경입니다. 제거된 내장 가상계좌와는 별개로 유지합니다.

## 설정 파일 위치

| 환경 | 실제 설정 | 데이터·인증 |
| --- | --- | --- |
| 로컬 개발 | `core/.env` | 모듈의 `data/`, `secrets/` |
| Ubuntu 릴리스 배포 | `~/ai-manager/shared/core/.env` | `~/ai-manager/shared/` |
| 서버 배포 도구 | `~/ai-manager/shared/deploy.env` | 배포할 서비스 이름만 지정 |
| iPhone 서명 | `ios/Config/Local.xcconfig` | 본인의 개발 팀·번들 설정 |

`current/core/.env`와 데이터 경로는 배포 도구가 `shared`로 연결합니다.
릴리스 안에 키를 따로 복사하거나 `.env`를 매 배포마다 새로 만들지 않습니다.
`deploy.env`는 Bash에서 읽는 운영자 설정이므로 신뢰하는 운영자만 편집합니다.

로컬에서는 브로커별 `.env`를 중복 생성하지 않고 중앙 파일 하나를 사용합니다.
Core 로더는 이미 설정된 프로세스 환경변수를 유지합니다. 기본 파일 탐색은 현재 디렉터리의 `.env`,
`AI_MANAGER_ENV_PATH`, Core 패키지의 `.env` 순서입니다. 따라서 저장소 루트에 별도 `.env`를 만들지 않고
아래처럼 경로를 명시하는 구성을 권장합니다.

```sh
export AI_MANAGER_ENV_PATH="$PWD/core/.env"
```

Docker Compose는 `core/.env`를 `env_file`로 읽습니다. Compose의 `environment`에 지정한 값은
이 파일보다 우선합니다. `.env` 변경 뒤 단순 `restart`는 컨테이너 환경을 갱신하지 않으므로
로컬은 `up -d`, 릴리스 서버는 `~/ai-manager/bin/deploy.sh`로 다시 적용합니다.

## 주요 환경변수

### 브로커 조회와 주문 경계

| 변수 | 용도·기본값 |
| --- | --- |
| `CORE_DEFAULT_BROKER` | Core 기본 브로커, `toss`; 보고 요청의 `--broker` 선택과는 별개 |
| `KIS_ENV` | `demo` 또는 `real`; 키를 발급받은 환경과 일치해야 함 |
| `KIS_APP_KEY`, `KIS_APP_SECRET` | 해당 환경의 KIS 앱 인증정보 |
| `KIS_ACCOUNT_NO`, `KIS_ACCOUNT_PRODUCT_CODE` | 계좌번호와 상품 코드; 예제 상품 코드 `01` |
| `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET` | Toss API 인증정보 |
| `TOSS_ACCOUNT_SEQ` | Toss 계좌 식별값 |
| `KIS_ALLOW_ORDERS`, `TOSS_ALLOW_ORDERS` | 개별 브로커 CLI 주문 허용; 기본 `false` 유지 |
| `KIS_REAL_TRADING_ACK`, `TOSS_REAL_TRADING_ACK` | 개별 CLI의 추가 실주문 확인값; 예제는 비어 있음 |
| `KIS_MAX_ORDER_AMOUNT`, `KIS_MAX_OVERSEAS_ORDER_AMOUNT_USD` | KIS 개별 주문 상한 |
| `TOSS_MAX_ORDER_AMOUNT_USD`, `TOSS_MAX_ORDER_AMOUNT_KRW`, `TOSS_MAX_SLIPPAGE_PCT` | Toss 개별 주문·가격 제한 |

필요한 브로커만 설정합니다. 매니저 보고 요청은 `--broker kis`, `--broker toss` 또는 `--broker both`로 선택합니다.
reports Compose는 주문 허용 두 값을 `false`로 고정하며, 매니저에는 자동 매매 실행기가 없습니다.
이 설정은 개별 브로커 CLI를 직접 실행하는 다른 경로 전체를 통제하는 중앙 스위치는 아닙니다.

### 데이터와 정책

| 변수 | 용도 |
| --- | --- |
| `AI_MANAGER_DATA_DIR` | 매니저 요청·보고·투자논리·판단 원장; reports 컨테이너는 `/app/runtime/manager`로 고정 |
| `CORE_DATABASE_PATH`, `CORE_KILL_SWITCH_PATH` | Core 원장과 킬 스위치 파일 |
| `KI_DB_PATH`, `TOSS_DB_PATH` | 브로커별 DB 경로 |
| `CORE_MIN_CASH_PCT`, `CORE_MAX_SINGLE_POSITION_PCT` | Core 정책의 최소 현금·종목 비중 |
| `CORE_MAX_LEVERAGED_EXPOSURE_PCT`, `CORE_LEVERAGED_SYMBOLS` | 레버리지 노출 정책과 해당 종목 목록 |
| `CORE_MAX_DAILY_NEW_BUY_USD`, `CORE_MAX_QUOTE_DIVERGENCE_PCT` | Core 일일 신규 매수·브로커 시세 차이 제한 |
| `CORE_PANIC_BUY_BLOCK_MINUTES` | Core 급변 시 매수 차단 시간 |

Core 설정의 상대 DB 경로는 읽은 `.env` 디렉터리를 기준으로 해석합니다.
외부 설정 파일을 사용할 때는 본인의 절대 경로를 명시하면 혼동을 줄일 수 있습니다.
컨테이너에는 호스트 경로가 아닌 컨테이너 내부 경로를 설정합니다.
reports의 매니저 데이터는 호스트 `core/data/manager`를 마운트하며, 별도 브로커 DB 경로가 모두 마운트되는 것은 아닙니다.
Core 정책과 브로커 개별 CLI의 적용 경로는 다르므로 한 값을 바꾸면 모든 주문 경로에 적용된다고 가정하지 않습니다.

### 알림과 LLM

| 변수·파일 | 용도 |
| --- | --- |
| `CORE_DISCORD_WEBHOOK_URL`, `CORE_DISCORD_USERNAME` | Discord 전송; webhook URL 자체가 인증정보 |
| `CORE_NOTIFY_CHANNELS` | Core 알림 worker 채널; 기본 `console` |
| `AI_MANAGER_APNS_TEAM_ID`, `AI_MANAGER_APNS_KEY_ID` | APNs 키의 팀·키 식별값 |
| `AI_MANAGER_APNS_KEY_PATH` | reports 컨테이너 기준 `/run/ai-manager-secrets/apns.p8` |
| `AI_MANAGER_APNS_TOPIC` | 본인의 iOS 앱 번들 ID |
| `AI_MANAGER_PLANNER_ENABLED` | 투자논리 AI 초안·정기 판단 요청; 단독 reports 기본 꺼짐, planner Compose 추가 시 `true` |
| `secrets/codex-planner/` | planner의 전용 `CODEX_HOME`; 로그인 정보·갱신 토큰 저장 |
| `CORE_STRATEGY_*` | 별도 전략 reviewer의 CLI·모델·프롬프트·호출 예산; 매니저 투자논리·판단 요청과 별개 |

Discord 없이 확인할 때는 보고 요청에 `--channel local`을 사용합니다. 매니저 보고 요청은 기본 채널이 Discord이므로
`CORE_NOTIFY_CHANNELS=console`만으로 해당 요청의 Discord 전송이 꺼지지는 않습니다.
APNs는 키·서명·기기 등록까지 별도 준비해야 하며 실제 기기 수신 검증은 별도입니다.

## 인증정보의 보관과 교체

| 종류 | 보관 위치 | 운영 원칙 |
| --- | --- | --- |
| 브로커 API 키·계좌 식별값 | 로컬 `core/.env` / 서버 `shared/core/.env` | 파일 권한 `600`, 계좌·환경별로 관리 |
| SSH 개인키 | 개발 PC의 기존 키 / iPhone Keychain | Git·LLM 컨테이너에 전달하지 않음 |
| 서버 호스트 공개키 | iPhone 서버 연결 설정 | 신뢰하는 SSH 경로로 확인; 변경 시 원인 확인 후 갱신 |
| Codex 인증 캐시 | 서버 `shared/secrets/codex-planner/` | 전용 디렉터리, planner에서 쓰기 가능하게 유지 |
| APNs `.p8` | 서버 `shared/secrets/apns.p8` | 디렉터리 `700`, 파일 `600`; reports에는 읽기 전용 마운트 |
| iOS 개인 서명 설정 | 로컬 `ios/Config/Local.xcconfig` | Xcode에서 본인의 기존 개발 팀 사용 |

기존 키를 재사용하며 설치 도구가 브로커·SSH·Apple 키를 새로 발급하지 않습니다.
서버의 Docker 권한을 가진 운영자는 컨테이너 인증정보에도 접근할 수 있으므로 계정 접근을 제한합니다.
키 값은 명령 인자·커밋·문서·스크린샷에 적지 않고 권한을 제한한 파일이나 인증 도구에서 입력합니다.

키를 교체할 때는 제공자의 절차에 따라 새 값을 서버 설정에 반영하고, 재배포 후 실제 조회·전송을 확인한 다음
이전 키를 폐기합니다. 유출이 의심되면 먼저 해당 키를 폐기하고 재인증합니다.
이미 커밋된 키는 파일 삭제나 `.gitignore` 추가만으로 보호되지 않습니다. 키 폐기 후 공개 Git 이력도 점검합니다.

### LLM 계획기 인증

첫 reports 배포 뒤 **서버**에서 진행합니다. 이 예시는 기존 ChatGPT 계정의 기기 코드 로그인을 사용합니다.
기기 코드 로그인은 계정의 보안 설정 또는 워크스페이스 권한에서 허용되어 있어야 합니다.
본인이 요청한 로그인 코드만 브라우저에 입력합니다. [공식 Codex 인증 안내](https://learn.chatgpt.com/docs/auth)를 참고합니다.

```bash
# 서버에서 실행
~/ai-manager/current/deploy/planner-login.sh             # 기기 코드 로그인
~/ai-manager/current/deploy/planner-login.sh --enable    # 로그인 후 planner를 켜고 다시 배포
~/ai-manager/current/deploy/planner-login.sh --status    # 상태만 확인
```

스크립트는 `shared/secrets/codex-planner`를 권한 700으로 만들고, planner 이미지를 빌드한 뒤
전용 컨테이너 안에서 `codex login --device-auth`를 실행합니다. 생성된 `auth.json`은 같은 컨테이너에서
권한 600으로 바꾸므로 sudo가 필요 없습니다. `--enable`은 로그인이 성공했을 때만
`shared/deploy.env`의 `AI_MANAGER_DEPLOY_STACKS`에 `planner`를 추가하고 재배포합니다.

<details>
<summary>스크립트가 실행하는 명령</summary>

```bash
cd "$HOME/ai-manager/current"
docker compose -f docker-compose.reports.yml -f docker-compose.planner.yml build planner
docker compose -f docker-compose.reports.yml -f docker-compose.planner.yml \
    run --rm --no-deps --entrypoint codex planner \
    -c 'cli_auth_credentials_store="file"' login --device-auth
docker compose -f docker-compose.reports.yml -f docker-compose.planner.yml \
    run --rm --no-deps -T --entrypoint codex planner login status
```

</details>

`file` 저장은 전용 `CODEX_HOME`의 `auth.json`을 사용합니다. 토큰 갱신 때문에 이 디렉터리는 읽기·쓰기로 마운트합니다.
개인 `~/.codex` 전체를 복사하지 않습니다. 캐시도 비밀번호처럼 취급합니다.

무인 자동화의 인증에는 공식 안내가 API 키 사용을 권장합니다. API 키는 명령 인자가 아니라 표준입력으로 넘깁니다.

```bash
~/ai-manager/current/deploy/planner-login.sh --api-key < ~/openai-key.txt && shred -u ~/openai-key.txt
```

키는 셸 기록과 프로세스 목록에 남지 않도록 인자·환경변수로 받지 않습니다.
루트 `.env`에 키를 추가하는 것만으로 planner에 전달되지는 않습니다.
로그인 상태 확인은 실제 모델 응답 성공까지 보장하지 않습니다.
인증 후 [planner 배포 단계](deployment.md#llm-계획기)로 돌아갑니다.

## 공개 전 확인

저장소 루트에서 파일 내용 대신 추적 여부를 확인합니다.

```sh
git ls-files -- core/.env secrets ios/Config/Local.xcconfig
git check-ignore core/.env secrets/codex-planner/auth.json secrets/apns.p8 ios/Config/Local.xcconfig
git diff --cached --stat
```

첫 명령은 출력이 없어야 하고 두 번째는 제외 대상 경로를 표시해야 합니다.
`.env.example`처럼 비어 있는 설정 예제만 공개합니다. Git ignore는 이미 추적된 파일에는 적용되지 않으므로
새로운 인증 도구나 파일 경로를 추가할 때도 실제 추적 상태를 확인합니다.
DB·스풀·로그·토큰 캐시·개인 운용 문서·암호화하지 않은 백업은 공개 저장소에 포함하지 않습니다.
