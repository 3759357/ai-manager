# 계좌 없이 데모와 검증

[루트 설치](../README.md)를 마친 가상환경에서 실행합니다. 테스트는 가짜 응답·임시 DB를 사용하며
실계좌 환경변수나 `.env`가 필요하지 않습니다.

## 테스트

```sh
python -m unittest discover -s ki/tests -q
python -m unittest discover -s toss/tests -q
python -m unittest discover -s core/tests -q
```

요청 중복 방지·재확인, 리스크 차단, 브로커 주문 결과 불명 복구,
브로커 응답 정규화, 승인 버전과 판단 만료를 검증합니다.

## 로컬 CLI 데모

```sh
core brokers
core portfolio-risk --cash 5000 --position AAPL:5:100 --pending-buy 0
ai-manager --data-dir ./data/demo reports --json
ai-manager --data-dir ./data/demo thesis import core/seed/theses-2026-09-25.json
ai-manager --data-dir ./data/demo thesis list
ai-manager --data-dir ./data/demo allocation --input core/tests/fixtures/allocation_golden.json
```

합성 seed는 draft 상태로만 저장되며 자동 승인·주문을 하지 않습니다.
마지막 명령은 가격·NAV fixture로 비중 상한을 계산하는 미리보기입니다.
실제 계좌·AI 호출·브로커 API에 접근하지 않습니다.

## iPhone 화면

`ios/AIManager.xcodeproj`를 열고 시뮬레이터로 실행해 **예시 보고서 둘러보기**를 누릅니다.
합성 계좌 보고와 수량·통화·손익 표시를 확인할 수 있습니다. 터미널 시연에는 별도 SSH 서버가 필요합니다.

```sh
xcodebuild -project ios/AIManager.xcodeproj -scheme AIManager \
  -destination 'generic/platform=iOS Simulator' build CODE_SIGNING_ALLOWED=NO
```

`ios/AIManagerTests/report-fixture.json`은 Python 보고와 Swift 디코딩의 계약용 fixture입니다.
설치된 시뮬레이터를 확인하고 해당 기기의 destination을 지정하면 iOS 테스트를 실행할 수 있습니다.

## 배포 검증

Linux의 테스트는 임시 Git 저장소·가짜 Docker 명령으로 성공·실패·복구를 확인합니다.
운영 서버에 연결하지 않습니다.

```sh
python -m unittest discover -s deploy -p 'test_*.py' -v
```
