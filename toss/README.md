# Toss 어댑터

토스증권 REST 시세·계좌 조회, 지정가 주문 후보·제출·정정·취소, SQLite 원장·대사를 제공합니다.
`core/.env`의 `TOSS_*` 값과 `AI_MANAGER_ENV_PATH`를 사용합니다. 예제 설정은 주문을 허용하지 않습니다.

```sh
toss --help
python -m unittest discover -s toss/tests -q
```

실제 조회에는 본인의 API 인증과 계좌 설정이 필요합니다. 전송 직전 `SUBMITTING`을 기록하고,
응답 유실·타임아웃·5xx 등 접수 여부 불명은 `SUBMIT_UNKNOWN`으로 유지합니다.
읽기 요청의 재시도와 결과 불명 주문의 재제출 금지를 구분합니다.
[전체 구조](../docs/architecture.md) · [설정](../docs/deployment.md)
