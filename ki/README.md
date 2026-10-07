# KIS 어댑터

한국투자증권 REST 조회, 웹소켓 호가·체결 수집, 주문 후보·원장·대사를 제공합니다.
`core/.env`의 `KIS_*` 값과 `AI_MANAGER_ENV_PATH`를 사용합니다. 기본 예제는 모의 환경과 주문 금지입니다.

```sh
ki --help
python -m unittest discover -s ki/tests -q
```

실제 조회에는 본인의 KIS API 인증과 계좌 설정이 필요합니다.
웹소켓 수집은 동일 인증키의 호가·체결 구독을 하나의 연결로 공유합니다.
결과 불명 주문은 `SUBMIT_UNKNOWN`으로 보존하고 재제출보다 조회·대사를 우선합니다.
[전체 구조](../docs/architecture.md) · [설정](../docs/deployment.md)
