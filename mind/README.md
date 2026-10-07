# LLM 게이트웨이

파일 스풀의 요청·응답 JSON으로 서버 worker와 격리된 LLM CLI를 연결합니다.
`planner`, `reviewer`, `thesis`, `judge` 역할별 프롬프트·스키마를 포함합니다.
LLM 컨테이너에는 브로커 키·계좌 DB·Docker 소켓을 마운트하지 않습니다.

```sh
docker compose -f docker-compose.reports.yml -f docker-compose.planner.yml config --quiet
```

기동에는 별도 LLM CLI 인증과 스풀·환경 설정이 필요합니다.
인증파일은 `secrets/` 아래에 준비하며 개인 인증 디렉터리 전체를 복사하지 않습니다.
`AI_MANAGER_PLANNER_ENABLED=true` 설정 전에는 계획기를 사용하는 모의 흐름을 켜지 않습니다.
[전체 구조](../docs/architecture.md) · [설정](../docs/deployment.md)
