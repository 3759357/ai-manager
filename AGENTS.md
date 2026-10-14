# AI 작업 지침

이 저장소에서 작업하는 코딩 에이전트(Claude Code, Codex 등)를 위한 규칙입니다. 사람용 안내는 [CONTRIBUTING.md](CONTRIBUTING.md)입니다.

- `private/`는 사용자의 개인 문서입니다. 읽고 참고할 수 있지만 그 내용(종목·수량·금액·계좌·서버 정보)을
  코드·커밋·공개 문서로 옮기지 않습니다.
- 개인 값은 Git 제외 로컬 파일(`core/.env`, `deploy/local.env`, `ios/Config/Local.xcconfig`)에만 둡니다.
  새 설정이 필요하면 `.example` 파일에 빈 값이나 예시 값을 추가합니다.
- 커밋 전 `python3 scripts/check_private.py --staged`가 통과해야 합니다(`pre-commit` 훅이 자동 실행).
- 실제 주문을 보내는 명령(`--execute`, `submit`)은 사용자가 명시적으로 요청하지 않으면 실행하지 않습니다.
- 주문 경로 변경은 `core/guard.py`와 Kill Switch를 우회하지 않아야 하며 테스트를 함께 추가합니다.
- `server` remote로 push하면 운영 서버에 배포됩니다. 사용자가 요청할 때만 push하고, 커밋할 때 자동 배포를
  원치 않으면 `AI_MANAGER_AUTO_DEPLOY=0`을 붙입니다.
- 커밋 메시지는 `type: subject` 형식입니다([CONTRIBUTING.md](CONTRIBUTING.md)).
