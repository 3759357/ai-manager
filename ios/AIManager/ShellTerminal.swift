import SwiftUI
import SwiftTerm
import Citadel
import NIOCore
import NIOSSH

enum ShellKey: String, CaseIterable {
    case interrupt = "Ctrl+C", tab = "Tab", escape = "Esc", up = "↑", down = "↓", left = "←", right = "→"
    var bytes: [UInt8] {
        switch self {
        case .interrupt: return [3]
        case .tab: return [9]
        case .escape: return [27]
        case .up: return [27, 91, 65]
        case .down: return [27, 91, 66]
        case .right: return [27, 91, 67]
        case .left: return [27, 91, 68]
        }
    }
}

@MainActor
final class ShellSession: ObservableObject {
    @Published private(set) var connected = false
    @Published private(set) var connecting = false
    @Published private(set) var status = "연결 안 됨"
    let terminal = SwiftTerm.TerminalView(frame: CGRect(x: 0, y: 0, width: 390, height: 600))
    private var client: SSHClient?
    private var task: Task<Void, Never>?
    private var generation = UUID()
    private var cols = 80
    private var rows = 24
    private enum Input { case bytes([UInt8]); case size(Int, Int) }
    private var input: AsyncStream<Input>.Continuation?

    init() {
        terminal.font = .monospacedSystemFont(ofSize: 13, weight: .regular)
        terminal.nativeBackgroundColor = .black
        terminal.nativeForegroundColor = .white
        // Use the iOS text keyboard; our toolbar supplies terminal control keys.
        // SwiftTerm's default accessory can switch inputView to a symbols-only pad.
        terminal.inputAccessoryView = nil
        terminal.feed(text: "AI Manager SSH 터미널\r\n상단의 연결 버튼으로 서버 셸에 접속하세요.\r\n")
    }

    func connect(_ profile: ServerProfile) {
        guard task == nil else { return }
        let token = UUID(); generation = token
        connecting = true; status = "SSH 연결 중…"
        terminal.feed(text: "\r\n── 새 SSH 세션 ──\r\n")
        task = Task { [weak self] in
            guard let self else { return }
            var connection: SSHClient?
            do {
                let ssh = try await SSHService.connect(profile: profile)
                connection = ssh
                guard !Task.isCancelled, self.generation == token else { try? await ssh.close(); return }
                self.client = ssh
                let stream = AsyncStream<Input>(bufferingPolicy: .bufferingOldest(256)) { self.input = $0 }
                let continuation = self.input
                try await ssh.withPTY(.init(wantReply: true, term: "xterm-256color",
                    terminalCharacterWidth: self.cols, terminalRowHeight: self.rows,
                    terminalPixelWidth: 0, terminalPixelHeight: 0, terminalModes: .init([:]))) { output, writer in
                    guard self.generation == token, !Task.isCancelled else { return }
                    self.connecting = false; self.connected = true; self.status = "\(profile.username)@\(profile.host)"
                    self.showKeyboard()
                    let sender = Task {
                        do {
                            for await event in stream {
                                try Task.checkCancellation()
                                switch event {
                                case .bytes(let bytes): try await writer.write(ByteBuffer(bytes: bytes))
                                case .size(let cols, let rows): try await writer.changeSize(cols: cols, rows: rows, pixelWidth: 0, pixelHeight: 0)
                                }
                            }
                        } catch { try? await ssh.close() }
                    }
                    defer { continuation?.finish(); sender.cancel() }
                    for try await output in output {
                        guard self.generation == token, !Task.isCancelled else { break }
                        switch output {
                        case .stdout(let data), .stderr(let data): self.terminal.feed(byteArray: Array(data.readableBytesView)[...])
                        }
                    }
                }
                if self.generation == token { self.status = "셸 연결 종료" }
            } catch {
                if self.generation == token {
                    self.status = (error as? AIManagerError)?.localizedDescription ?? "SSH 셸 연결이 종료되었습니다. 네트워크 또는 서버 셸 설정을 확인하세요."
                }
            }
            if let connection { try? await connection.close() }
            guard self.generation == token else { return }
            self.input?.finish(); self.input = nil; self.client = nil; self.task = nil
            self.connected = false; self.connecting = false
            self.terminal.resignFirstResponder()
            self.terminal.feed(text: "\r\n── 연결 종료 · 재연결 시 새 셸이 열립니다 ──\r\n")
        }
    }

    func send(_ bytes: [UInt8]) {
        guard connected, let input else { return }
        if case .dropped = input.yield(.bytes(bytes)) { disconnect("입력 대기열 초과 · 연결을 종료했습니다.") }
    }

    func showKeyboard() {
        terminal.inputView = nil
        terminal.inputAccessoryView = nil
        terminal.becomeFirstResponder()
        terminal.reloadInputViews()
    }

    func resize(cols: Int, rows: Int) {
        self.cols = max(1, cols); self.rows = max(1, rows)
        guard connected, let input else { return }
        if case .dropped = input.yield(.size(self.cols, self.rows)) { disconnect("입력 대기열 초과 · 연결을 종료했습니다.") }
    }

    func disconnect(_ reason: String = "연결 해제됨") {
        generation = UUID()
        task?.cancel(); task = nil
        input?.finish(); input = nil
        let old = client; client = nil
        connected = false; connecting = false; status = reason
        terminal.resignFirstResponder()
        if let old { Task { try? await old.close() } }
    }
}

struct ShellScreen: View {
    @ObservedObject var session: ShellSession
    @EnvironmentObject private var model: AppModel
    @State private var settings = false
    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                HStack {
                    Circle().fill(session.connected ? .mint : .secondary).frame(width: 7, height: 7)
                    Text(session.status).font(.caption).lineLimit(2)
                    Spacer()
                    if session.connecting { ProgressView() }
                }.padding(10)
                ShellCanvas(session: session)
                HStack(spacing: 0) {
                    ForEach(ShellKey.allCases, id: \.self) { key in
                        Button(key.rawValue) { session.send(key.bytes) }.font(.system(size: 13, weight: .semibold, design: .monospaced))
                            .frame(maxWidth: .infinity).padding(.vertical, 12).disabled(!session.connected)
                    }
                    Button { session.terminal.resignFirstResponder() } label: { Image(systemName: "keyboard.chevron.compact.down") }
                        .padding(8).accessibilityLabel("키보드 숨기기")
                }.background(.bar)
                Text("우분투 셸에 직접 연결합니다 · 앱이 백그라운드로 가면 연결을 종료합니다")
                    .font(.caption2).foregroundStyle(.secondary).padding(6)
            }
            .navigationTitle("터미널").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { settings = true } label: { Image(systemName: "gearshape") }
                        .disabled(session.connected || session.connecting).accessibilityLabel("SSH 연결 설정")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("키보드", systemImage: "keyboard") { session.showKeyboard() }
                        .disabled(!session.connected).accessibilityLabel("글자 키보드 열기")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    if session.connected || session.connecting {
                        Button("연결 해제") { session.disconnect() }
                    } else {
                        Button("연결") { if model.profile.isConfigured { session.connect(model.profile) } else { settings = true } }
                    }
                }
            }
            .sheet(isPresented: $settings) { SettingsView() }
            .onDisappear { session.terminal.resignFirstResponder() }
        }
    }
}

struct ShellCanvas: UIViewRepresentable {
    @ObservedObject var session: ShellSession
    func makeCoordinator() -> Coordinator { Coordinator(session) }
    func makeUIView(context: Context) -> SwiftTerm.TerminalView {
        session.terminal.terminalDelegate = context.coordinator
        return session.terminal
    }
    func updateUIView(_ uiView: SwiftTerm.TerminalView, context: Context) {}

    final class Coordinator: NSObject, TerminalViewDelegate {
        let session: ShellSession
        init(_ session: ShellSession) { self.session = session }
        func send(source: SwiftTerm.TerminalView, data: ArraySlice<UInt8>) {
            MainActor.assumeIsolated { session.send(Array(data)) }
        }
        func sizeChanged(source: SwiftTerm.TerminalView, newCols: Int, newRows: Int) {
            MainActor.assumeIsolated { session.resize(cols: newCols, rows: newRows) }
        }
        func setTerminalTitle(source: SwiftTerm.TerminalView, title: String) {}
        func hostCurrentDirectoryUpdate(source: SwiftTerm.TerminalView, directory: String?) {}
        func scrolled(source: SwiftTerm.TerminalView, position: Double) {}
        func rangeChanged(source: SwiftTerm.TerminalView, startY: Int, endY: Int) {}
        // Remote OSC clipboard/link requests cannot read or overwrite the phone clipboard or open URLs.
        func clipboardCopy(source: SwiftTerm.TerminalView, content: Data) {}
        func clipboardRead(source: SwiftTerm.TerminalView) -> Data? { nil }
        func requestOpenLink(source: SwiftTerm.TerminalView, link: String, params: [String: String]) {}
    }
}
