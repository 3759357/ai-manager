import SwiftUI
import UserNotifications
import UIKit

@MainActor
final class PushCenter: ObservableObject {
    static let shared = PushCenter()
    @Published var status = "푸시 설정을 확인하세요."
    @Published var token: String?
    @Published var openedInstruction: String?
    var enabled: Bool {
        get { UserDefaults.standard.bool(forKey: "pushEnabled") }
        set { UserDefaults.standard.set(newValue, forKey: "pushEnabled") }
    }
    let deviceID: String
    var environment: String {
        Bundle.main.object(forInfoDictionaryKey: "AIManagerAPNSEnvironment") as? String ?? "sandbox"
    }
    private init() {
        let id = UserDefaults.standard.string(forKey: "pushDeviceID") ?? UUID().uuidString.lowercased()
        deviceID = id; UserDefaults.standard.set(id, forKey: "pushDeviceID")
    }
    func request() async {
        #if AI_MANAGER_APNS
        do {
            guard try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) else {
                status = "알림이 허용되지 않았습니다. iPhone 설정에서 변경할 수 있습니다."; return
            }
            status = "Apple 기기 등록 중…"
            enabled = true
            UIApplication.shared.registerForRemoteNotifications()
        } catch { status = "알림 권한을 확인하지 못했습니다." }
        #else
        status = "현재 Personal Team 빌드입니다. 유료 개발자 팀의 Push Notifications 설정과 APNs 서명이 필요합니다."
        #endif
    }
}

final class PushAppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        #if AI_MANAGER_APNS
        if PushCenter.shared.enabled { application.registerForRemoteNotifications() }
        #endif
        return true
    }
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        Task { @MainActor in PushCenter.shared.token = token; PushCenter.shared.status = "기기 등록 완료 · 서버 연결 중…" }
    }
    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        Task { @MainActor in PushCenter.shared.status = "APNs 등록 실패 · 개발자 팀과 푸시 서명을 확인하세요." }
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler completionHandler: @escaping () -> Void) {
        if let id = response.notification.request.content.userInfo["instruction_id"] as? String,
           id.range(of: "^[a-zA-Z0-9_-]{1,64}$", options: .regularExpression) != nil {
            Task { @MainActor in PushCenter.shared.openedInstruction = id }
        }
        completionHandler()
    }
}

struct EngineStatus: Decodable {
    let mode: String
    let enabled: Bool
    let cash: String
    let currency: String
    let initialCash: String
    let fills: [PaperFill]
    let pilot: PilotStatus?
}
struct PilotStatus: Decodable {
    let enabled: Bool
    let note: String
    let scope: String
    let lastMarketAt: String?
    let risk: PilotRisk?
    let plans: [PilotPlan]
}
struct PilotRisk: Decodable {
    let equity: String
    let drawdownPct: String
    let stage: String
    let trackedFrom: String
}
struct PilotPlan: Decodable, Identifiable {
    let id: String
    let state: String
    let note: String
}
struct PaperFill: Decodable, Identifiable {
    let id: String
    let instructionId: String
    let symbol: String
    let side: String
    let quantity: String
    let price: String
    let createdAt: String
}
struct PushRegistration: Decodable { let registered: Bool }
struct PushStatus: Decodable { let configured: Bool; let activeDevices: Int }

struct AutomationControls: View {
    @EnvironmentObject private var model: AppModel
    @ObservedObject private var push = PushCenter.shared
    @State private var engine: EngineStatus?
    @State private var message: String?
    @State private var busy = false
    var body: some View {
        Section("모의 운용") {
            Text(engine?.enabled == true ? "모의 운용 중" : "모의 운용 중지됨")
            Text("실계좌와 분리된 가상 10,000 USD 계좌입니다. 체결은 조회 가격으로 가정하며 실제 주문·수수료·호가 체결을 재현하지 않습니다.")
                .font(.caption).foregroundStyle(.secondary)
            if let engine { LabeledContent("모의 현금", value: "\(engine.cash) USD") }
            if let pilot = engine?.pilot {
                Text(pilot.enabled ? "실전 시세 모의 자동 운용 중" : "실전 시세 자동 운용 중지됨")
                Text(pilot.scope).font(.caption).foregroundStyle(.secondary)
                Text(pilot.note).font(.caption)
                if let risk = pilot.risk {
                    LabeledContent("모의 순자산 USD", value: displayNumber(risk.equity))
                    LabeledContent("추적 고점 대비 낙폭", value: "\(displayNumber(risk.drawdownPct))%")
                    LabeledContent("모의 위험 단계", value: risk.stage)
                    Text("추적 시작: \(risk.trackedFrom)").font(.caption2)
                }
                if let at = pilot.lastMarketAt { Text("시세 수집: \(at)").font(.caption2) }
                ForEach(pilot.plans.prefix(3)) { plan in
                    Text("\(plan.state) · \(plan.note)").font(.caption)
                }
                Button("실전 시세 모의 자동 운용 시작") { Task { await pilotControl("start") } }
                    .disabled(busy || model.demo || !model.profile.isConfigured || pilot.enabled)
            }
            HStack {
                Button("모의 운용 시작") { Task { await control("start") } }.disabled(engine?.enabled == true)
                Spacer()
                Button("운용 중지", role: .destructive) { Task { await control("stop") } }
            }.disabled(busy || model.demo || !model.profile.isConfigured)
            if let message { Text(message).font(.caption).foregroundStyle(.secondary) }
            if let engine {
                ForEach(engine.fills.prefix(5)) { fill in
                    Text("모의 · \(fill.symbol) \(fill.side == "buy" ? "매수" : "매도") \(fill.quantity)주 · \(fill.price) USD")
                        .font(.caption)
                }
            }
        }.task {
            while !Task.isCancelled {
                if model.profile.isConfigured, !model.demo { await control("status") }
                do { try await Task.sleep(nanoseconds: 15_000_000_000) } catch { break }
            }
        }
        Section("푸시 알림") {
            Text(push.status).font(.caption).foregroundStyle(.secondary)
            Button("알림 허용 및 서버 등록") {
                Task { await push.request(); await model.registerPush() }
            }.disabled(model.demo || !model.profile.isConfigured)
            Button("서버 알림 등록 해제") { Task { await model.unregisterPush() } }
                .disabled(model.demo || !model.profile.isConfigured)
        }
    }
    private func control(_ action: String) async {
        guard !busy else { return }; busy = true
        defer { busy = false }
        do { engine = try await model.engineControl(action); message = nil }
        catch { message = error.localizedDescription }
    }
    private func pilotControl(_ action: String) async {
        guard !busy else { return }; busy = true
        defer { busy = false }
        do {
            let _: PilotStatus = try await model.pilotControl(action)
            engine = try await model.engineControl("status"); message = nil
        } catch { message = error.localizedDescription }
    }
    private func displayNumber(_ value: String) -> String {
        guard let number = Double(value), number.isFinite else { return "미확인" }
        return String(format: "%.2f", number)
    }
}
