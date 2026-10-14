# AI Manager for iPhone

iOS 17 이상 SwiftUI 앱입니다. 보고함·지시함·푸시 알림·SSH PTY 터미널을 제공합니다.

```sh
xcodebuild -project ios/AIManager.xcodeproj -scheme AIManager \
  -destination 'generic/platform=iOS Simulator' build CODE_SIGNING_ALLOWED=NO
```

Xcode에서 프로젝트를 열고 시뮬레이터로 실행합니다. 첫 화면의 **예시 보고서 둘러보기**는 서버 없이 동작합니다.
공개본 번들 ID는 `com.example.ai-manager`입니다. 실기기는 본인의 고유 번들 ID와 Apple 서명을 사용합니다.
`Config/Local.xcconfig.example`을 `Config/Local.xcconfig`로 복사해 팀 ID와 `AI_MANAGER_BUNDLE_ID`를 입력합니다. 이 파일은 Git에서 제외됩니다.

실제 서버 연결에는 주소·포트·사용자·SSH 개인키·이미 신뢰하는 경로에서 얻은 서버 호스트 공개키가 필요합니다.
개인키는 Keychain에 보관하고 등록한 서버 키만 신뢰합니다. 터미널은 SSH 계정의 셸 권한으로 동작합니다.
보고 생성에는 서버의 reports worker가 필요합니다. 투자 지시는 접수·이력 관리하며 자동 매매하지 않습니다.
APNs는 선택 기능이며 별도 팀·서버 키·프로파일·기기 등록이 필요합니다.
[데모](../docs/demo.md) · [환경과 배포](../docs/deployment.md)
