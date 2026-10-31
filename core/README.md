# Core

브로커 어댑터·공통 보고·리스크관리부(guard)·투자논리·판단 원장을 관리합니다.
설치는 [루트 안내](../README.md), 서버 설정은 [환경과 배포](../docs/deployment.md)를 참고합니다.

```sh
core brokers
core portfolio-risk --cash 5000 --position AAPL:5:100 --pending-buy 0
ai-manager --data-dir ./data/demo reports --json
ai-manager --data-dir ./data/demo thesis import core/seed/theses-2026-09-25.json
ai-manager --data-dir ./data/demo thesis list
```

위 예제는 합성 입력과 로컬 상태만 사용합니다. import는 draft를 만들며 자동 승인·주문을 하지 않습니다.
브로커 조회는 본인의 `.env` 설정 후 사용합니다.

```sh
core quote AAPL --broker toss
core compare-quotes AAPL
core orders --broker both
ai-manager request '현재 계좌 보고' --channel local
ai-manager reports --json
ai-manager report latest
```

계좌 보고 요청은 별도 worker가 처리합니다. `ai-manager worker` 또는 reports Compose를 실행합니다.
`ai-manager`에는 가상계좌와 자동 매매 실행기가 없습니다. 투자 지시는 접수·조회·취소·처리 결과 기록을 지원합니다.
`core kill-switch on --reason "..."`은 core·KIS·토스의 신규 후보와 제출·정정을 모두 멈춥니다. 취소는 계속 허용합니다.
worker는 보고 생성, 투자논리 AI 초안·판단 응답 적재, 선택적 알림을 처리합니다.
상세 하위 명령은 `core --help`, `ai-manager --help`에서 확인합니다.

## 리스크관리부 (guard)

`core/src/core/guard.py`는 LLM 없이 주문 의도를 허용·차단하는 결정론적 검증기입니다.
한도는 버전이 있는 [`policy/v1.toml`](src/core/policy/v1.toml)에서만 읽고, 키가 빠지거나 남으면 로드를 거절합니다.
주문을 만들거나 보내지 않습니다.

`core intent`와 `core submit --execute`의 매수 리스크 평가도 같은 정책 파일의 한도(현금 하한, 종목 비중,
레버리지 실효 노출, 주문당·일일 매수 한도)를 씁니다. 새 주문을 반영한 뒤의 비중으로 판단하고, 제출 직전에
다시 평가하며, 우회 옵션은 없습니다. 낙폭·투자북 비중 상한·트레이딩북처럼 계좌 NAV 이력이 필요한 guard
전체 판정의 주문 경로 연결은 남은 과제입니다.

```sh
core guard policy                                         # 적용 중인 정책
core guard size --entry 500 --stop 480 --book-nav 10000   # 트레이딩북 1건 손실 1% 기준 수량
core guard check intent.json                              # {"intent":…, "account":…, "quote":…} 판정
```

- 계좌 → 장부 → 전략 → 종목 → 주문 순으로 모두 검사하고 하나라도 걸리면 차단합니다.
- 낙폭·주식 비중 상한·트레이딩북 NAV를 모르거나 스냅샷이 오래되면 매수를 막습니다.
- 매도(위험 축소)는 호가 신선도·보유 수량·Kill Switch만 확인합니다. 공매도와 다른 장부 포지션 매도는 막습니다.
- `check_alerts`는 시장·계좌 급락, 보유 종목 급변, 실적 예정, heartbeat 끊김을 경보로 만들며,
  아웃박스 dedup 키로 같은 단계는 하루 한 번만 보냅니다. 자동으로 팔거나 막지 않습니다.
- 환경변수 `AI_MANAGER_POLICY_PATH`로 다른 정책 파일을 지정할 수 있습니다.
