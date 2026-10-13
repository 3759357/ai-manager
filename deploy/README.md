# 배포 도구

Ubuntu 서버에서 이미지 빌드·네트워크 없는 테스트·서비스 상태 확인 후 릴리스를 전환합니다.
영속 DB·설정·스풀은 `shared`에 보관하고 코드 버전만 복구합니다. DB 마이그레이션은 별도로 관리합니다.

`deploy.sh`, `post-receive`, `ai-manager` 서버 CLI wrapper와 설치 도구를 포함합니다.
원격 서버와 로컬 훅은 자동으로 설정되지 않습니다. 본인의 기존 SSH 인증과 bare 저장소를 준비한 뒤 설정합니다.

```sh
AI_MANAGER_SERVER=ai-manager-server ./deploy/install-server.sh
```

설치 명령은 지정 서버에 파일을 씁니다. 설치만으로 배포하지 않으며 `server` remote의 `main` push가 배포를 시작합니다.
GitHub `origin` push와 서버 push는 별개입니다. 운영 서버 인증을 GitHub에 저장하지 않습니다.

| 도구 | 역할 |
| --- | --- |
| `bootstrap-ubuntu.sh` | Ubuntu Docker·Compose 설치와 운영 계정의 Docker 그룹 등록 |
| `install-server.sh` | 서버 배포 실행기·post-receive 훅·CLI wrapper 설치 |
| `post-receive` | 서버 bare 저장소의 main 갱신 후 배포 실행 |
| `deploy.sh` | 빌드·격리 테스트·shared 연결·기동 확인·릴리스 전환 |
| `ai-manager` | current의 보고 worker에 명령 전달 |
| `../.githooks/post-commit` | 선택적으로 로컬 main 커밋을 서버에 자동 push |

처음 설치할 때는 reports만 선택합니다. `deploy.env`가 없을 때 설치 도구가 만드는 기본값은
`reports planner`이므로 [최초 서버 설정](../docs/deployment.md#ubuntu-서버-최초-설치)을 먼저 따라갑니다.
[환경변수·키 관리](../docs/configuration.md) · [재배포·자동화](../docs/deployment.md#재배포와-자동화) · [실패 복구](../docs/deployment.md#운영-확인과-복구)
