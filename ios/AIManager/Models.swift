import Foundation

struct ServerProfile: Codable, Equatable {
    var host = ""
    var port = "22"
    var username = "ubuntu"
    var directory = "/home/ubuntu/ai-manager/current"
    var hostKey = ""

    var isConfigured: Bool { !host.isEmpty && !hostKey.isEmpty }
    var identity: String { "\(host):\(port):\(username):\(directory)" }

    func validate() throws {
        guard !host.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !host.contains(where: { $0.isWhitespace }),
              let port = Int(port), (1...65535).contains(port),
              !username.isEmpty, directory.hasPrefix("/"), !directory.contains("\n"),
              ![host, port.description, username, directory, hostKey].contains(where: { $0.contains("\0") }),
              !hostKey.isEmpty else { throw AIManagerError.settings }
    }
}

enum AIManagerError: LocalizedError {
    case settings, key, keychain, hostKey, authentication, connection, response, timeout
    var errorDescription: String? {
        switch self {
        case .settings: return "서버 주소·포트·사용자·절대 경로·호스트 공개키를 확인하세요."
        case .key: return "OpenSSH Ed25519 또는 RSA 개인키와 암호를 확인하세요."
        case .keychain: return "개인키를 Keychain에 저장하거나 읽지 못했습니다."
        case .hostKey: return "서버 호스트 공개키를 확인하세요. ssh-ed25519로 시작하는 한 줄만 입력하며, 등록된 서버 키와 일치해야 합니다."
        case .authentication: return "서버가 SSH 인증을 거절했습니다. 사용자와 개인키의 서버 등록 여부를 확인하세요."
        case .connection: return "SSH 연결을 완료하지 못했습니다. 네트워크·서버 호스트키·인증 정보와 보고 워커 실행 상태를 확인하세요."
        case .response: return "서버 응답을 읽지 못했습니다. 최신 AI Manager CLI가 설치되어 있는지 확인하세요."
        case .timeout: return "서버 응답 시간이 초과되었습니다. 동일 요청 ID로 다시 확인할 수 있습니다."
        }
    }
}

struct ReportReceipt: Decodable, Identifiable {
    let id: String
    let state: String
    let createdAt: String
    let channel: String
    let reportSummary: String?
    let error: String?
    var stateLabel: String {
        switch state {
        case "pending": return "계좌 조회 대기"
        case "ready": return "보고서 생성 · 전송 대기"
        case "completed": return channel == "local" ? "보고서 생성 완료" : "디스코드 전송 완료"
        default: return state
        }
    }
}

struct ReportList: Decodable { let schemaVersion: Int; let requests: [ReportReceipt] }
struct AccountReport: Decodable {
    let schemaVersion: Int
    let reportId: String
    let collectedFrom: String
    let collectedTo: String
    let status: String
    let positions: [Holding]
    let buyingPower: [BuyingPower]
    let marketValueByCurrency: [String: String]
    let warnings: [String]
    let scopeNote: String
    let sources: [ReportSource]
    let recentOrders: [BrokerOrder]?
    let orderScopeNote: String?
}
struct Holding: Decodable {
    let broker: String
    let symbol: String
    let currency: String
    let quantity: String
    let marketValue: String?
    let unrealizedPnl: String?
}
struct BrokerOrder: Decodable {
    let broker: String
    let orderId: String
    let symbol: String
    let side: String
    let status: String
    let currency: String
    let quantity: String
    let filledQuantity: String
    let averageFilledPrice: String?
    let filledAmount: String?
    let commission: String?
    let orderedAt: String
}
struct BuyingPower: Decodable { let broker: String; let currency: String; let amount: String }
struct ReportSource: Decodable { let source: String; let status: String }

enum RemoteCommand {
    static func quote(_ value: String) -> String { "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'" }
    static func make(profile: ServerProfile, arguments: [String]) -> String {
        "cd " + quote(profile.directory) + " && timeout 25s docker compose -f docker-compose.reports.yml exec -T report-worker ai-manager " + arguments.map(quote).joined(separator: " ")
    }
}
