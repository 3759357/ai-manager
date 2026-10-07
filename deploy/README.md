# 배포 도구

Ubuntu 서버에서 이미지 빌드·네트워크 없는 테스트·서비스 상태 확인 후 릴리스를 전환합니다.
영속 DB·설정·스풀은 `shared`에 보관하고 코드 버전만 복구합니다. DB 마이그레이션은 별도로 관리합니다.

`deploy.sh`, `post-receive`, `ai-manager` 서버 CLI wrapper와 설치 도구를 포함합니다.
원격 서버와 훅은 자동으로 설정되지 않습니다. 본인의 SSH 별칭과 bare 저장소를 준비한 뒤 설정합니다.

```sh
AI_MANAGER_SERVER=ai-manager-server ./deploy/install-server.sh
```

설치 명령은 지정 서버에 파일을 씁니다. [배포 안내](../docs/deployment.md)의 선행 조건을 확인한 환경에서 사용합니다.
GitHub는 소스 공유용이며 운영 서버 인증을 GitHub에 저장하지 않습니다.
