# LLM 게이트웨이

파일 스풀의 요청·응답 JSON으로 서버 worker와 격리된 LLM CLI를 연결합니다.
`reviewer`, `thesis`, `judge` 역할별 프롬프트·스키마를 포함합니다.
LLM 컨테이너에는 브로커 키·계좌 DB·Docker 소켓을 마운트하지 않습니다.

```sh
docker compose -f docker-compose.reports.yml -f docker-compose.planner.yml config --quiet
```

기동에는 별도 LLM CLI 인증과 스풀·환경 설정이 필요합니다.
인증파일은 `secrets/` 아래에 준비하며 개인 인증 디렉터리 전체를 복사하지 않습니다.
`AI_MANAGER_PLANNER_ENABLED=true`는 투자논리 AI 초안·정기 판단 요청을 활성화합니다.
Compose 서비스 이름 `planner`와 인증 경로는 기존 배포와 호환되도록 유지하며, 기본 역할은 `judge`입니다.
가상계좌 매매 계획과 체결은 처리하지 않습니다.
[전체 구조](../docs/architecture.md) · [전용 CLI 인증](../docs/configuration.md#llm-계획기-인증) · [planner 배포](../docs/deployment.md#llm-계획기)
