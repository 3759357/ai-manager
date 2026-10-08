# AI Manager

**SwiftUI iPhone 앱 · Python 서버 · 멀티브로커 · 격리된 LLM · 모의 투자 운용**

서로 다른 증권사의 계좌 데이터를 하나의 보고로 정규화하고, 자연어 지시의 접수부터
계획 검증·모의 체결·결과 보고까지 서버에서 이어가는 개인 투자 관리 프로젝트입니다.
아이폰은 SSH로 서버에 직접 연결하며 서버의 작업 상태는 앱 연결과 독립적으로 보존합니다.

## 핵심 기능

| 기능 | 구현 |
| --- | --- |
| 멀티브로커 | KIS REST·웹소켓, Toss REST, 공통 시세·포지션·주문 모델 |
| 보고함 | 같은 JSON으로 앱·CLI·HTML·Markdown 보고, 일부 조회 실패와 기준 시각 표시 |
| 지시함 | 요청 ID 중복 방지, 접수·처리·취소·결과 이력, 연결 단절 후 재확인 |
| 모의 운용 | 가상 USD 계좌, 실제 시세 기반 pilot, 계획 만료·리스크 검증·체결 원장 |
| 판단 기록 | 버전별 투자논리 승인, 추가 전용 LLM 판단 원장, 목표 비중 미리보기 |
| iPhone 앱 | SwiftUI 보고함·지시함·위험 계기판·SSH PTY 터미널·예시 보고 모드 |
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
ai-manager --data-dir ./data/demo engine status
```

테스트와 데모는 API 키 없이 실행합니다. iOS 시뮬레이터에서는 **예시 보고서 둘러보기**로
합성 계좌 화면을 볼 수 있습니다. [데모 안내](docs/demo.md)에서 비중 계산과 보고 스키마도 확인할 수 있습니다.

## 자신의 환경으로 사용

```sh
cp core/.env.example core/.env
# core/.env에 본인의 브로커 인증정보와 선택 기능 설정 입력
export AI_MANAGER_ENV_PATH="$PWD/core/.env"
docker compose -f docker-compose.reports.yml up -d --build
```

설정 예제는 인증값이 비어 있고 실주문 허용이 꺼져 있습니다. 설정·키·DB·스풀은 Git에서 제외됩니다.
브로커 조회에는 API 접근 권한이, LLM 연동에는 별도 인증이 필요합니다.
[환경과 배포](docs/deployment.md)에 최초 설정, CLI 사용, iPhone 연결 방법을 정리했습니다.

## 문서와 코드

- [제품 기획](docs/product-plan.md): 목표, 조직·권한, 투자북·트레이딩북, 개발 순서
- [아키텍처](docs/architecture.md): 데이터 흐름과 실행 경계
- [비중 규칙](docs/allocation.md): 계산식, 승인된 카드와 판단의 결합, 연구 재현
- [데모](docs/demo.md) · [설정·배포](docs/deployment.md) · [검증 기록](docs/verification.md)
- [Core](core/README.md) · [KIS](ki/README.md) · [Toss](toss/README.md) · [LLM](mind/README.md) · [iOS](ios/README.md) · [배포 도구](deploy/README.md)

## 현재 범위

매니저의 자동 실행 어댑터는 `paper`만 지원합니다. 브로커 개별 CLI의 실주문 기능은 별도 경로이며,
전체 경로의 중앙 통제와 실계좌 NAV는 남은 과제입니다. 모의 체결은 호가·부분체결·체결 비용을
완전히 재현하지 않습니다. APNs는 코드 기반을 제공하며 실제 기기 수신 검증은 별도입니다.
구현·테스트 결과와 투자 전략의 성과 검증을 구분합니다.
