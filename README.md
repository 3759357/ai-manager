# AI Manager

**SwiftUI iPhone 앱 · Python 서버 · 멀티브로커 · 격리된 LLM · 투자 판단·보고**

서로 다른 증권사의 계좌 데이터를 하나의 보고로 정규화하고, 자연어 지시의 접수부터
투자논리 점검·판단 기록·결과 보고까지 서버에서 관리하는 개인 투자 관리 프로젝트입니다.
아이폰은 SSH로 서버에 직접 연결하며 서버의 작업 상태는 앱 연결과 독립적으로 보존합니다.

## 핵심 기능

| 기능 | 구현 |
| --- | --- |
| 멀티브로커 | KIS REST·웹소켓, Toss REST, 공통 시세·포지션·주문 모델 |
| 보고함 | 같은 JSON으로 앱·CLI·HTML·Markdown 보고, 일부 조회 실패와 기준 시각 표시 |
| 지시함 | 요청 ID 중복 방지, 접수·처리·취소·결과 이력, 연결 단절 후 재확인 |
| 판단 기록 | 버전별 투자논리 승인, 추가 전용 LLM 판단 원장, 목표 비중 미리보기 |
| iPhone 앱 | SwiftUI 보고함·지시함·SSH PTY 터미널·예시 보고 모드 |
| 알림 | 영속 아웃박스, 중복 억제·재시도, 선택 Discord·APNs 어댑터 |
| 배포 | 테스트 후 릴리스 전환, 코드와 영속 데이터 분리, 실패 시 이전 실행 버전 복구 |

## 설계에서 해결한 문제

| 문제 | 선택한 방식 |
| --- | --- |
| 브로커마다 다른 응답과 주문 상태 | 어댑터·공통 모델과 브로커별 원장 분리 |
| 요청 직후 네트워크 단절 | 영속 요청 ID로 동일 요청 재확인 |
| 주문 전송 후 응답 유실 | 전송 전 상태 기록, 결과 불명 상태의 재제출 차단, 조회·대사 복구 |
| 비결정적 AI 출력 | JSON 계약·정책 검증과 실행 계층 분리 |
| LLM에 실행 권한이 전달되는 문제 | 브로커 인증·계좌 DB·Docker 소켓을 제공하지 않는 별도 컨테이너 |
| 원격 서버 신뢰 | 호스트 공개키 고정, 기기 전용 Keychain에 SSH 개인키 보관 |

## 계좌 없이 실행

Python 3.11 이상에서 저장소 루트를 기준으로 실행합니다.

```sh
git clone https://github.com/3759357/ai-manager.git
cd ai-manager
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ki -e toss -e 'core[push]'
python -m unittest discover -s core/tests -q
python -m unittest discover -s ki/tests -q
python -m unittest discover -s toss/tests -q
core brokers
ai-manager --data-dir ./data/demo reports --json
```

테스트와 데모는 API 키 없이 실행합니다. iOS 시뮬레이터에서는 **예시 보고서 둘러보기**로
합성 계좌 화면을 볼 수 있습니다. [데모 안내](docs/demo.md)에서 비중 계산과 보고 스키마도 확인할 수 있습니다.

## 자신의 환경으로 사용

처음 사용하는 경우 **환경변수 준비 → 보고 서버 실행 → 계좌 조회 확인 → 선택 기능 추가** 순서로 진행합니다.
Ubuntu 서버에 상주시키는 경우에는 아래 서버 배포 가이드를 따릅니다.

```sh
test -f core/.env || cp core/.env.example core/.env
chmod 600 core/.env
# core/.env에 본인의 브로커 인증정보와 선택 기능 설정 입력
export AI_MANAGER_ENV_PATH="$PWD/core/.env"
docker compose -f docker-compose.reports.yml up -d --build
```

설정 예제는 인증값이 비어 있고 실주문 허용이 꺼져 있습니다. 설정·키·DB·스풀은 Git에서 제외됩니다.
브로커 조회에는 API 접근 권한이, LLM 연동에는 별도 인증이 필요합니다.

| 준비할 항목 | 가이드 |
| --- | --- |
| 환경변수·브로커 API 키 | [필수·선택 설정과 파일 위치](docs/configuration.md) |
| SSH·LLM·APNs·앱 서명 | [키 보관·교체와 유출 대응](docs/configuration.md#인증정보의-보관과-교체) |
| Ubuntu 서버 최초 설치 | [Docker·디렉터리·설정·첫 배포](docs/deployment.md#ubuntu-서버-최초-설치) |
| 코드 갱신과 자동 배포 | [서버 push와 선택적 로컬 훅](docs/deployment.md#재배포와-자동화) |
| 배포 확인·실패·백업 | [운영 확인과 복구](docs/deployment.md#운영-확인과-복구) |
| iPhone에서 서버 연결 | [SSH와 서버 호스트 공개키 등록](docs/deployment.md#iphone-연결) |

서버는 `releases/`의 코드와 `shared/`의 설정·데이터를 분리합니다. 서버 준비가 끝나면 다음 흐름으로 갱신합니다.

```sh
git push origin main  # GitHub에 코드 공개
git push server main  # 서버에서 빌드 → 테스트 → 기동 확인 → 릴리스 전환
```

`server` remote는 최초 설치 때 별도로 등록합니다. GitHub push에 연동되는 Actions 워크플로는 아직 제공하지 않습니다.
로컬 `main` 커밋 시 서버로 자동 push하는 훅은 직접 활성화할 수 있습니다.
컨테이너는 보고 처리·투자논리 점검·판단 기록을 담당합니다. 투자 지시의 접수만으로 매매를 실행하지 않습니다.

## 문서와 코드

- [제품 기획](docs/product-plan.md): 목표, 조직·권한, 투자북·트레이딩북, 개발 순서
- [아키텍처](docs/architecture.md): 데이터 흐름과 실행 경계
- [비중 규칙](docs/allocation.md): 계산식, 승인된 카드와 판단의 결합, 연구 재현
- [데모](docs/demo.md) · [환경변수·키 관리](docs/configuration.md) · [서버 설정·배포·자동화](docs/deployment.md) · [검증 기록](docs/verification.md)
- [Core](core/README.md) · [KIS](ki/README.md) · [Toss](toss/README.md) · [LLM](mind/README.md) · [iOS](ios/README.md) · [배포 도구](deploy/README.md)

## 현재 범위

내장 가상계좌·가상 잔고·모의 체결 기능은 제공하지 않습니다. 매니저는 지시 접수·처리 이력을 저장하며
자동 매매는 실행하지 않습니다. 브로커 개별 CLI의 실주문 기능은 별도 경로이고,
전체 주문 경로의 중앙 통제와 실계좌 NAV는 남은 과제입니다. APNs의 실제 기기 수신 검증은 별도입니다.
구현·테스트 결과와 투자 전략의 성과 검증을 구분합니다.
