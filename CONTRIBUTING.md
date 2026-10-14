# 개발 안내

이 저장소를 가져다 자신의 계좌·서버로 쓰거나 고쳐서 기여하는 방법입니다.

## 처음 받았을 때

```sh
git clone https://github.com/3759357/ai-manager.git
cd ai-manager
git config core.hooksPath .githooks          # 커밋 메시지·개인정보 검사 훅 켜기
git config commit.template .gitmessage
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e ki -e toss -e 'core[push]'
```

`.githooks/post-commit`은 `server` remote가 있으면 `main` 커밋마다 서버로 push합니다.
서버를 쓰지 않거나 원하지 않으면 `git config ai-manager.autoDeploy false`로 끕니다.

## 개인 값은 로컬 파일에만

서버 주소, 번들 ID, 개발 팀, API 키, 계좌 식별값은 코드·문서에 쓰지 않습니다.
저장소의 `.example` 파일을 복사해 실제 값을 넣고, 실제 파일은 Git이 무시합니다.

| 예시 파일 | 실제 파일 | 담는 값 |
| --- | --- | --- |
| `core/.env.example` | `core/.env` | 브로커 키·계좌, 알림, APNs |
| `deploy/local.env.example` | `deploy/local.env` | 서버 SSH 별칭 |
| `ios/Config/Local.xcconfig.example` | `ios/Config/Local.xcconfig` | Apple 개발 팀, 앱 번들 ID |

개인 메모·기획·운용 기록은 저장소 안의 `private/` 폴더에 두면 됩니다. `.gitignore`가 폴더 전체를 제외합니다.

커밋할 때 `pre-commit` 훅이 `scripts/check_private.py --staged`를 실행합니다.
`private/`·실제 설정 파일·키 파일이 들어가거나, 키·웹훅·사설 IP 형식, 또는 **내 로컬 설정 파일의 실제 값**이
커밋 내용에 보이면 커밋을 막습니다. 결과에는 파일 이름과 종류만 나오고 값은 출력하지 않습니다.
CI도 같은 검사를 형식 기준으로 실행합니다. 오탐인 줄에는 `check-private: allow`를 적습니다.

## 테스트

```sh
python -m unittest discover -s core/tests -q
python -m unittest discover -s ki/tests -q
python -m unittest discover -s toss/tests -q
python -m unittest discover -s deploy -p 'test_*.py' -q   # Linux
python -m unittest discover -s scripts -p 'test_*.py' -q
```

테스트는 API 키 없이 가짜 브로커·알림·docker로 실행합니다. 실계좌 주문을 보내는 테스트는 없습니다.

## 커밋 메시지

`type: subject` 형식이며 `commit-msg` 훅이 검사합니다.

| type | 쓰는 때 |
| --- | --- |
| feat | 기능 추가 |
| fix | 버그 수정 |
| docs | 문서 |
| refactor | 동작 변화 없는 구조 개선 |
| test | 테스트 |
| chore | 설정·도구·잡무 |
| perf | 성능 |
| ci | CI |
| init / style / revert | 초기 구성 / 포맷 / 되돌리기 |

## 주문 경로를 바꿀 때

- 테스트가 통과하는지
- 실주문 허용 기본값이 꺼져 있는지(`*_ALLOW_ORDERS=false`)
- 리스크관리부(`core/guard.py`)·Kill Switch·브로커 Gateway를 우회하는 경로가 없는지
- 결과를 모르는 주문을 다시 보내지 않는지(조회로 복구)
- LLM 출력이나 외부 텍스트가 셸 명령·주문으로 실행되지 않는지
