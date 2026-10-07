# Core

브로커 어댑터·공통 보고·리스크·모의 실행·투자논리·판단 원장을 관리합니다.
설치는 [루트 안내](../README.md), 서버 설정은 [환경과 배포](../docs/deployment.md)를 참고합니다.

```sh
core brokers
core portfolio-risk --cash 5000 --position AAPL:5:100 --pending-buy 0
ai-manager --data-dir ./data/demo engine status
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
모의 엔진과 pilot은 명시적으로 시작하며 전체 실전 자동 주문 어댑터는 없습니다.
상세 하위 명령은 `core --help`, `ai-manager --help`에서 확인합니다.
