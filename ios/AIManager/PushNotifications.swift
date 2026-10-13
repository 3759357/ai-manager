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

struct PushRegistration: Decodable { let registered: Bool }
struct PushStatus: Decodable { let configured: Bool; let activeDevices: Int }

struct PushNotificationControls: View {
    @EnvironmentObject private var model: AppModel
    @ObservedObject private var push = PushCenter.shared
    var body: some View {
        Section("푸시 알림") {
            Text(push.status).font(.caption).foregroundStyle(.secondary)
            Button("알림 허용 및 서버 등록") {
                Task { await push.request(); await model.registerPush() }
            }.disabled(model.demo || !model.profile.isConfigured)
            Button("서버 알림 등록 해제") { Task { await model.unregisterPush() } }
                .disabled(model.demo || !model.profile.isConfigured)
        }
    }
}
