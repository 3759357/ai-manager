import SwiftUI

@MainActor
final class AppModel: ObservableObject {
    @Published var profile: ServerProfile
    @Published var receipts: [ReportReceipt] = []
    @Published var busy = false
    @Published var notice: String?
    @Published var selectedReport: AccountReport?
    @Published var demo = false
    @Published var instructions: [InvestmentInstruction] = []
    @Published var instructionDraft = ""
    @Published var instructionPending = false
    @Published var instructionBusy = false
    @Published var instructionNotice: String?
    private var instructionKey: String { "pendingInstruction:" + profile.identity }

    private func restoreInstruction() {
        let pending = UserDefaults.standard.data(forKey: instructionKey).flatMap { try? JSONDecoder().decode(PendingInstruction.self, from: $0) }
        instructionDraft = pending?.message ?? ""
        instructionPending = pending != nil
        instructions = []; instructionNotice = nil
    }
    private let decoder: JSONDecoder = {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase; return decoder
    }()

    init() {
        profile = UserDefaults.standard.data(forKey: "serverProfile").flatMap { try? JSONDecoder().decode(ServerProfile.self, from: $0) } ?? ServerProfile()
        restoreInstruction()
        if ProcessInfo.processInfo.arguments.contains("--demo") { showDemo() }
    }

    func save(_ profile: ServerProfile, secret: SSHSecret?) throws {
        try profile.validate()
        try SSHService.validateHostKey(profile.hostKey)
        // Validate credentials before replacing the saved profile.
        if let secret {
            _ = try SSHService.authentication(username: profile.username, secret: secret)
            try KeyStore.save(secret)
        } else { _ = try KeyStore.load() }
        self.profile = profile
        UserDefaults.standard.set(try JSONEncoder().encode(profile), forKey: "serverProfile")
        receipts = []; selectedReport = nil; demo = false
        restoreInstruction()
    }

    private func fetch<T: Decodable>(_ arguments: [String]) async throws -> T {
        let data = try await SSHService.execute(profile: profile, arguments: arguments)
        do { return try decoder.decode(T.self, from: data) }
        catch { throw AIManagerError.response }
    }

    func refresh() async {
        guard !busy, !demo else { return }
        busy = true; notice = nil
        defer { busy = false }
        do {
            let list: ReportList = try await fetch(["reports", "--json"])
            guard list.schemaVersion == 1 else { throw AIManagerError.response }
            receipts = list.requests
        } catch { notice = error.localizedDescription }
    }

    func request(discord: Bool) async {
        guard !busy, !demo else { return }
        busy = true; notice = nil
        defer { busy = false }
        // Persist the receipt ID before network I/O; a retry cannot create another job.
        let pendingKey = "pendingRequest:" + profile.identity + (discord ? ":discord" : ":local")
        let id = UserDefaults.standard.string(forKey: pendingKey) ?? UUID().uuidString.lowercased()
        UserDefaults.standard.set(id, forKey: pendingKey)
        do {
            let receipt: ReportReceipt = try await fetch(["request", "현재 계좌", "--request-id", id, "--channel", discord ? "discord" : "local"])
            UserDefaults.standard.removeObject(forKey: pendingKey)
            receipts.removeAll { $0.id == receipt.id }; receipts.insert(receipt, at: 0)
            notice = "보고 요청을 접수했습니다. 서버 워커가 처리하며, 새로고침으로 결과를 확인할 수 있습니다."
        } catch { notice = "\(error.localizedDescription) 재시도 시 동일 요청 ID를 사용합니다." }
    }

    func open(_ receipt: ReportReceipt) async {
        guard !busy else { return }
        if demo { selectedReport = DemoData.report; return }
        busy = true; notice = nil
        defer { busy = false }
        do {
            let report: AccountReport = try await fetch(["report", receipt.id, "--format", "json"])
            guard report.schemaVersion == 1, report.reportId == receipt.id else { throw AIManagerError.response }
            selectedReport = report
        } catch { notice = error.localizedDescription }
    }

    func refreshInstructions() async {
        guard !instructionBusy, !demo else { return }
        instructionBusy = true; instructionNotice = nil
        defer { instructionBusy = false }
        do {
            let list: InstructionList = try await fetch(["instructions", "--json"])
            guard list.schemaVersion == 1 else { throw AIManagerError.response }
            instructions = list.instructions
        } catch { instructionNotice = error.localizedDescription }
    }

    private func updateInstruction(_ item: InvestmentInstruction) {
        instructions.removeAll { $0.id == item.id }; instructions.insert(item, at: 0)
    }

    func submitInstruction() async {
        guard !instructionBusy, !demo else { return }
        let text = instructionDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text.unicodeScalars.count <= 4000 else {
            instructionNotice = "지시는 1~4000자로 입력하세요."; return
        }
        instructionBusy = true; instructionNotice = nil
        defer { instructionBusy = false }
        let key = instructionKey
        do {
            let stored = UserDefaults.standard.data(forKey: key).flatMap { try? JSONDecoder().decode(PendingInstruction.self, from: $0) }
            if let stored, stored.message != text {
                // 전송 결과를 모르는 이전 지시를 고쳐 쓴 경우: 이전 지시가 서버에 닿았는지 먼저 확인한다.
                // 확인하지 못하면 아무것도 보내지 않는다(이전 지시가 조용히 사라지거나 둘 다 나가는 것을 막음).
                let list: InstructionList = try await fetch(["instructions", "--json", "--limit", "100"])
                guard list.schemaVersion == 1 else { throw AIManagerError.response }
                if let sent = list.instructions.first(where: { $0.id == stored.id }) { updateInstruction(sent) }
                UserDefaults.standard.removeObject(forKey: key)
            }
            let pending = PendingInstruction.next(stored: stored, draft: text)
            UserDefaults.standard.set(try JSONEncoder().encode(pending), forKey: key)
            instructionPending = true
            let item: InvestmentInstruction = try await fetch(["instruct", pending.message, "--request-id", pending.id])
            guard item.id == pending.id, item.message == pending.message else { throw AIManagerError.response }
            UserDefaults.standard.removeObject(forKey: key)
            instructionPending = false; instructionDraft = ""
            updateInstruction(item)
            instructionNotice = "지시를 저장했습니다. 처리 상태와 결과는 지시함에서 확인할 수 있습니다. 자동 매매는 실행하지 않습니다."
        } catch { instructionNotice = "\(error.localizedDescription) 같은 지시 ID로 재확인할 수 있습니다." }
    }

    func refreshInstruction(_ id: String) async {
        guard !instructionBusy, !demo else { return }
        instructionBusy = true
        defer { instructionBusy = false }
        do {
            let item: InvestmentInstruction = try await fetch(["instruction", id])
            guard item.id == id else { throw AIManagerError.response }
            updateInstruction(item)
        } catch { instructionNotice = error.localizedDescription }
    }

    func cancelInstruction(_ id: String) async {
        guard !instructionBusy, !demo else { return }
        instructionBusy = true; instructionNotice = nil
        defer { instructionBusy = false }
        do {
            let item: InvestmentInstruction = try await fetch(["cancel-instruction", id])
            guard item.id == id else { throw AIManagerError.response }
            updateInstruction(item)
        } catch { instructionNotice = error.localizedDescription }
    }

    func showDemo() { demo = true; receipts = [DemoData.receipt]; notice = nil }

    func registerPush() async {
        guard profile.isConfigured, !demo, PushCenter.shared.enabled, let token = PushCenter.shared.token else { return }
        do {
            let result: PushRegistration = try await fetch(["register-device", PushCenter.shared.deviceID, token, "--environment", PushCenter.shared.environment])
            let status: PushStatus = try await fetch(["push-status"])
            PushCenter.shared.status = result.registered && status.configured ? "푸시 연결 완료" : "기기 등록 완료 · 서버 APNs 키 설정 필요"
        } catch { PushCenter.shared.status = "서버 기기 등록 실패 · 알림 등록 버튼으로 재시도하세요." }
    }

    func unregisterPush() async {
        guard profile.isConfigured, !demo else { return }
        do {
            let _: PushRegistration = try await fetch(["unregister-device", PushCenter.shared.deviceID])
            PushCenter.shared.enabled = false
            PushCenter.shared.status = "서버 알림 등록을 해제했습니다."
        } catch { PushCenter.shared.status = error.localizedDescription }
    }
    func leaveDemo() { demo = false; receipts = []; selectedReport = nil; notice = nil }
    func forget() {
        KeyStore.delete()
        UserDefaults.standard.removeObject(forKey: "serverProfile")
        profile = ServerProfile(); receipts = []; selectedReport = nil; demo = false
        restoreInstruction()
    }
}

enum DemoData {
    static let receipt = ReportReceipt(id: "demo-report", state: "completed", createdAt: "2026-09-19T09:30:00+09:00", channel: "local",
        reportSummary: "예시 계좌 보고\nKRW 240,000 · USD 1,280.50\n실제 계좌와 연결되지 않은 데모 데이터입니다.", error: nil)
    static let report = AccountReport(schemaVersion: 1, reportId: "demo-report", collectedFrom: "2026-09-19T09:29:55+09:00",
        collectedTo: "2026-09-19T09:30:00+09:00", status: "ok", positions: [
            Holding(broker: "toss", symbol: "SAMPLE", currency: "USD", quantity: "0.125", marketValue: "30.50", unrealizedPnl: "2.10"),
            Holding(broker: "kis", symbol: "EXAMPLE", currency: "USD", quantity: "5", marketValue: "1250", unrealizedPnl: "35"),
            Holding(broker: "toss", symbol: "예시 종목", currency: "KRW", quantity: "3", marketValue: "240000", unrealizedPnl: "-1500")],
        buyingPower: [BuyingPower(broker: "toss", currency: "USD", amount: "120")],
        marketValueByCurrency: ["USD": "1280.50", "KRW": "240000"], warnings: [],
        scopeNote: "데모 데이터입니다. 주식 평가액은 통화별로 표시하며 현금·미체결·기타 자산은 포함하지 않습니다.",
        sources: [ReportSource(source: "demo", status: "ok")], recentOrders: [],
        orderScopeNote: "데모 주문 내역입니다.")
}
