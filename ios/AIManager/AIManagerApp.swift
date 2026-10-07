import SwiftUI

@main
struct AIManagerApp: App {
    @UIApplicationDelegateAdaptor(PushAppDelegate.self) private var pushDelegate
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase
    var body: some Scene {
        WindowGroup {
            MainTabs().environmentObject(model)
                .tint(.mint)
                .preferredColorScheme(.dark)
                .overlay {
                    if scenePhase != .active {
                        ZStack { Color(.systemBackground).ignoresSafeArea(); Label("AI Manager", systemImage: "lock.shield").font(.largeTitle.bold()) }
                    }
                }
        }
    }
}

struct MainTabs: View {
    @State private var selectedTab = 0
    @ObservedObject private var push = PushCenter.shared
    @StateObject private var terminal = ShellSession()
    @EnvironmentObject private var model: AppModel
    @Environment(\.scenePhase) private var phase
    var body: some View {
        TabView(selection: $selectedTab) {
            RootView().tabItem { Label("보고함", systemImage: "tray") }.tag(0)
            InstructionsView().tabItem { Label("투자 지시", systemImage: "text.bubble") }.tag(1)
            ShellScreen(session: terminal).tabItem { Label("터미널", systemImage: "terminal") }.tag(2)
        }
        .onChange(of: push.token) { _, _ in Task { await model.registerPush() } }
        .onChange(of: push.openedInstruction) { _, value in if value != nil { selectedTab = 1 } }
        .sheet(isPresented: Binding(get: { push.openedInstruction != nil }, set: { if !$0 { push.openedInstruction = nil } })) {
            if let id = push.openedInstruction { NavigationStack { InstructionDetail(id: id) } }
        }
        .onChange(of: phase) { _, value in
            if value == .background { terminal.disconnect("백그라운드 전환 · 다시 연결하세요.") }
        }
        .onChange(of: model.profile) { _, _ in terminal.disconnect("설정 변경 · 다시 연결하세요.") }
    }
}

struct RootView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.scenePhase) private var phase
    @State private var settings = false
    @State private var discord = false
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    HStack {
                        Label(model.demo ? "DEMO · 예시 계좌" : "PERSONAL INVESTMENT MANAGER", systemImage: "circle.hexagongrid.fill")
                            .font(.caption.weight(.semibold)).foregroundStyle(.mint)
                        Spacer()
                    }
                    Text("내 투자,\n한곳에서 확인.").font(.system(size: 36, weight: .bold)).tracking(-1)
                    Text("서버가 만든 보고를 읽고, 필요할 때 새 계좌 보고를 요청하세요.")
                        .foregroundStyle(.secondary)

                    if model.demo {
                        HStack { Label("실제 계좌에 연결되지 않은 미리보기", systemImage: "sparkles").font(.caption); Spacer(); Button("종료") { model.leaveDemo() } }
                            .padding().background(.mint.opacity(0.1), in: RoundedRectangle(cornerRadius: 16))
                    }

                    if !model.profile.isConfigured && !model.demo {
                        VStack(alignment: .leading, spacing: 16) {
                            Label("서버 연결부터 시작하세요", systemImage: "server.rack").font(.headline)
                            Text("SSH 주소와 서버 공개키를 등록하면 아이폰에서 보고함을 열 수 있습니다.").font(.subheadline).foregroundStyle(.secondary)
                            Button("SSH 연결 설정") { settings = true }.buttonStyle(.borderedProminent)
                            Button("예시 보고서 둘러보기") { model.showDemo() }.font(.subheadline)
                        }.card()
                    } else {
                        VStack(alignment: .leading, spacing: 14) {
                            HStack { Label("새 계좌 보고", systemImage: "waveform.path.ecg").font(.headline); Spacer(); Text("조회 전용").font(.caption).foregroundStyle(.mint) }
                            Toggle("디스코드에도 보내기", isOn: $discord).font(.subheadline)
                            Text("앱과 SSH CLI에서는 디스코드 전송 여부와 관계없이 같은 보고서를 읽을 수 있습니다.").font(.caption).foregroundStyle(.secondary)
                            Button { Task { await model.request(discord: discord) } } label: {
                                HStack { Spacer(); Label("현재 계좌 보고 요청", systemImage: "arrow.up.message.fill"); Spacer() }.padding(.vertical, 5)
                            }.buttonStyle(.borderedProminent).disabled(model.busy || model.demo)
                        }.card()
                    }

                    if let notice = model.notice {
                        Text(notice).font(.subheadline).foregroundStyle(.secondary).padding()
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 14)).textSelection(.enabled)
                    }

                    HStack {
                        Text("보고함").font(.title2.bold()); Spacer()
                        if model.busy { ProgressView() }
                        else { Text("\(model.receipts.count)개의 요청").font(.caption).foregroundStyle(.secondary) }
                    }
                    if model.receipts.isEmpty {
                        ContentUnavailableView("아직 불러온 보고가 없습니다", systemImage: "tray", description: Text("연결 후 새로고침하거나 첫 계좌 보고를 요청하세요."))
                    }
                    ForEach(model.receipts) { receipt in
                        Button { Task { await model.open(receipt) } } label: {
                            VStack(alignment: .leading, spacing: 12) {
                                HStack {
                                    Label(receipt.stateLabel, systemImage: receipt.reportSummary == nil ? "clock" : "doc.text")
                                        .font(.subheadline.weight(.semibold)).foregroundStyle(receipt.reportSummary == nil ? .orange : .mint)
                                    Spacer(); Image(systemName: "chevron.right").foregroundStyle(.secondary)
                                }
                                Text(receipt.reportSummary ?? "서버 워커의 처리를 기다리고 있습니다. 새로고침하면 상태를 다시 확인합니다.")
                                    .font(.subheadline).foregroundStyle(.primary).lineLimit(5)
                                Text(receipt.createdAt).font(.caption.monospaced()).foregroundStyle(.secondary)
                                if receipt.error != nil { Text("처리 또는 전송 재시도 중 · 보고서는 생성 후 바로 열 수 있습니다.").font(.caption).foregroundStyle(.orange) }
                            }.frame(maxWidth: .infinity, alignment: .leading).card()
                        }.buttonStyle(.plain).disabled(model.busy || receipt.reportSummary == nil)
                    }
                    Text("AI Manager · SSH로 연결된 나의 투자 보고함").font(.caption2).foregroundStyle(.tertiary).frame(maxWidth: .infinity).padding(.vertical)
                }.padding(22)
            }
            .background(Color(red: 0.035, green: 0.055, blue: 0.075))
            .navigationTitle("AI Manager").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { Image(systemName: "chart.xyaxis.line").foregroundStyle(.mint) }
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Button { Task { await model.refresh() } } label: { Image(systemName: "arrow.clockwise") }
                        .accessibilityLabel("보고함 새로고침").disabled(model.busy || !model.profile.isConfigured || model.demo)
                    Button { settings = true } label: { Image(systemName: "slider.horizontal.3") }
                        .accessibilityLabel("SSH 연결 설정").disabled(model.busy || model.instructionBusy)
                }
            }
            .refreshable { await model.refresh() }
            .task(id: phase) {
                guard phase == .active, model.profile.isConfigured, !model.demo else { return }
                while !Task.isCancelled {
                    await model.refresh()
                    try? await Task.sleep(for: .seconds(5))
                }
            }
            .sheet(isPresented: $settings) { SettingsView() }
            .sheet(isPresented: Binding(get: { model.selectedReport != nil }, set: { if !$0 { model.selectedReport = nil } })) {
                if let report = model.selectedReport { ReportView(report: report) }
            }
        }
    }
}

extension View {
    func card() -> some View {
        self.padding(18).background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 20))
            .overlay(RoundedRectangle(cornerRadius: 20).strokeBorder(Color.white.opacity(0.07)))
    }
}

struct ReportView: View {
    let report: AccountReport
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text(report.status == "ok" ? "조회 범위 내 보고 완료" : "일부 데이터를 확인하지 못했습니다")
                        .foregroundStyle(report.status == "ok" ? .mint : .orange)
                    LabeledContent("보고 ID", value: report.reportId).font(.caption)
                    LabeledContent("수집 시작", value: report.collectedFrom).font(.caption)
                    LabeledContent("수집 완료", value: report.collectedTo).font(.caption)
                }
                Section("조회된 주식 평가액") {
                    ForEach(report.marketValueByCurrency.keys.sorted(), id: \.self) { currency in
                        LabeledContent(currency) { Text(report.marketValueByCurrency[currency] ?? "미확인").font(.title2.monospacedDigit().bold()) }
                    }
                    Text(report.scopeNote).font(.caption).foregroundStyle(.secondary)
                }
                Section("보유 종목") {
                    ForEach(Array(report.positions.enumerated()), id: \.offset) { _, holding in
                        VStack(alignment: .leading, spacing: 8) {
                            HStack { Text(holding.symbol).font(.headline); Spacer(); Text(holding.broker.uppercased()).font(.caption).foregroundStyle(.secondary) }
                            LabeledContent("수량", value: holding.quantity)
                            LabeledContent("평가액", value: "\(holding.currency) \(holding.marketValue ?? "미확인")")
                            LabeledContent("평가손익", value: "\(holding.currency) \(holding.unrealizedPnl ?? "미확인")")
                        }.font(.subheadline).padding(.vertical, 6)
                    }
                }
                if let orders = report.recentOrders {
                    Section("최근 실계좌 주문") {
                        if orders.isEmpty { Text("조회 범위에 주문 내역이 없습니다.").foregroundStyle(.secondary) }
                        ForEach(Array(orders.enumerated()), id: \.offset) { _, order in
                            VStack(alignment: .leading, spacing: 6) {
                                HStack {
                                    Text("\(order.symbol) · \(order.side == "BUY" ? "매수" : "매도")")
                                        .font(.headline)
                                    Spacer()
                                    Text(order.broker.uppercased()).font(.caption).foregroundStyle(.secondary)
                                }
                                Text("\(order.status) · 주문 \(order.quantity)주 · 체결 \(order.filledQuantity)주")
                                if let price = order.averageFilledPrice {
                                    Text("평균 체결가 \(order.currency) \(price) · 체결금액 \(order.filledAmount ?? "미확인")")
                                }
                                if let commission = order.commission {
                                    Text("수수료 \(order.currency) \(commission)")
                                }
                                Text(order.orderedAt).font(.caption).foregroundStyle(.secondary)
                            }.font(.subheadline).padding(.vertical, 6)
                        }
                        if let note = report.orderScopeNote {
                            Text(note).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                Section("주문가능금액 · 현금잔고와 다름") {
                    ForEach(Array(report.buyingPower.enumerated()), id: \.offset) { _, power in
                        LabeledContent(power.broker, value: "\(power.currency) \(power.amount)")
                    }
                }
                Section("조회 범위") {
                    ForEach(Array(report.sources.enumerated()), id: \.offset) { _, source in LabeledContent(source.source, value: source.status) }
                    ForEach(Array(report.warnings.enumerated()), id: \.offset) { _, warning in Text(warning).foregroundStyle(.orange) }
                }
            }.textSelection(.enabled)
                .navigationTitle("계좌 보고").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("닫기") { dismiss() } } }
        }
    }
}
