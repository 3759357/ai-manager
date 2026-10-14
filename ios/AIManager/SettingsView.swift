import SwiftUI
import UniformTypeIdentifiers

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var draft = ServerProfile()
    @State private var privateKey: String?
    @State private var passphrase = ""
    @State private var importing = false
    @State private var importKind = KeyImportKind.privateKey
    @State private var error: String?
    @State private var confirmForget = false

    private enum KeyImportKind { case hostKey, privateKey }

    var body: some View {
        NavigationStack {
            Form {
                Section("SSH 서버") {
                    TextField("주소 · IP 또는 도메인", text: $draft.host).keyboardType(.URL)
                    TextField("포트", text: $draft.port).keyboardType(.numberPad)
                    TextField("사용자", text: $draft.username)
                }
                Section {
                    TextField("ssh-ed25519 AAAA…", text: $draft.hostKey, axis: .vertical).font(.caption.monospaced()).lineLimit(3...6)
                    Button { importKind = .hostKey; importing = true } label: {
                        Label("공개키 파일 가져오기", systemImage: "doc.badge.plus")
                    }
                } header: { Text("서버 호스트 공개키") } footer: {
                    Text("서버에서 확인한 호스트 공개키 파일(.pub)을 가져오거나 한 줄을 직접 입력하세요. 로그인용 공개키와 다릅니다. 등록한 키와 다르면 접속을 거절합니다.")
                }
                Section {
                    Button { importKind = .privateKey; importing = true } label: { Label(privateKey == nil ? "개인키 파일 가져오기" : "개인키 가져옴 · 저장 대기", systemImage: "key.fill") }
                    SecureField("개인키 암호 (있을 때만)", text: $passphrase)
                } header: { Text("SSH 인증") } footer: {
                    Text("OpenSSH 형식의 Ed25519 / RSA 키를 지원합니다. 개인키와 암호는 이 아이폰의 Keychain에 저장됩니다. 기존 키를 유지하려면 새로 가져오지 마세요.")
                }
                Section {
                    DisclosureGroup("고급 설정") {
                        TextField("서버 저장소 경로", text: $draft.directory)
                        Text("기본 경로를 자동으로 사용합니다. 서버 설치 위치를 바꾼 경우에만 수정하세요.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                Section {
                    Text("Mac에 설정한 SSH 별칭은 아이폰에 자동으로 복사되지 않습니다. 실제 주소와 포트를 입력하세요.")
                    Text("터미널은 SSH 접속만으로 사용할 수 있습니다. 보고함은 서버의 보고 워커가 실행되어 있어야 합니다.")
                }.font(.caption).foregroundStyle(.secondary)
                if let error { Section { Text(error).foregroundStyle(.orange) } }
                Section { Button("저장된 연결과 개인키 삭제", role: .destructive) { confirmForget = true } }
            }
            .textInputAutocapitalization(.never).autocorrectionDisabled()
            .navigationTitle("서버 연결").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("취소") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("저장") {
                        do {
                            draft.host = draft.host.trimmingCharacters(in: .whitespacesAndNewlines)
                            draft.hostKey = draft.hostKey.trimmingCharacters(in: .whitespacesAndNewlines)
                            draft.directory = draft.directory.trimmingCharacters(in: .whitespacesAndNewlines)
                            if draft.directory.isEmpty { draft.directory = ServerProfile().directory }
                            try model.save(draft, secret: privateKey.map { SSHSecret(privateKey: $0, passphrase: passphrase) })
                            privateKey = nil; passphrase = ""; dismiss()
                        } catch { self.error = error.localizedDescription }
                    }
                }
            }
            .onAppear { draft = model.profile }
            .fileImporter(isPresented: $importing, allowedContentTypes: [.item]) { result in
                do {
                    let url = try result.get()
                    let access = url.startAccessingSecurityScopedResource()
                    defer { if access { url.stopAccessingSecurityScopedResource() } }
                    guard let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize, size < 65536 else { throw AIManagerError.key }
                    let contents = try String(contentsOf: url, encoding: .utf8)
                    switch importKind {
                    case .hostKey:
                        let value = contents.trimmingCharacters(in: .whitespacesAndNewlines)
                        guard !value.contains("\n"), !value.contains("\r") else { throw AIManagerError.hostKey }
                        try SSHService.validateHostKey(value)
                        draft.hostKey = value
                    case .privateKey:
                        privateKey = contents
                    }
                    error = nil
                } catch {
                    self.error = importKind == .hostKey
                        ? "공개키 파일을 읽지 못했습니다. 서버 호스트 공개키 한 줄이 담긴 .pub 파일을 선택하세요."
                        : "개인키 파일을 읽지 못했습니다. 파일 형식과 접근 권한을 확인하세요."
                }
            }
            .confirmationDialog("아이폰에 저장된 연결 설정과 개인키를 삭제할까요?", isPresented: $confirmForget, titleVisibility: .visible) {
                Button("삭제", role: .destructive) { model.forget(); dismiss() }
            }
        }
    }
}
