import SwiftUI

struct InvestmentInstruction: Codable, Identifiable {
    let id: String
    let message: String
    let state: String
    let createdAt: String
    let updatedAt: String
    let result: String?
    let events: [InstructionEvent]
    var stateLabel: String {
        switch state {
        case "waiting": return "검토 대기"
        case "running": return "처리 중"
        case "completed": return "처리 완료"
        case "failed": return "처리 실패"
        case "cancelled": return "취소됨"
        default: return state
        }
    }
}
struct InstructionEvent: Codable { let state: String; let at: String; let result: String? }
struct InstructionList: Decodable { let schemaVersion: Int; let instructions: [InvestmentInstruction] }
struct PendingInstruction: Codable { let id: String; let message: String }

struct InstructionsView: View {
    @EnvironmentObject private var model: AppModel
    var body: some View {
        NavigationStack {
            List {
                PushNotificationControls()
                Section("새 투자 지시") {
                    TextField("예: 보유 종목의 비중 조정안을 검토해줘", text: $model.instructionDraft, axis: .vertical)
                        .lineLimit(3...8).disabled(model.instructionPending || model.instructionBusy)
                    Text("투자 검토 요청과 처리 이력을 저장합니다. 접수한 지시는 자동 실행되지 않으며, 실제 주문은 전송하지 않습니다.")
                        .font(.caption).foregroundStyle(.secondary)
                    Button(model.instructionPending ? "같은 지시로 접수 재확인" : "지시 보내기") {
                        Task { await model.submitInstruction() }
                    }.disabled(model.instructionBusy || !model.profile.isConfigured || model.demo || model.instructionDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
                if let notice = model.instructionNotice {
                    Section { Text(notice).font(.subheadline).foregroundStyle(.secondary) }
                }
                Section("지시함") {
                    if model.instructions.isEmpty { Text("접수된 지시가 없습니다.").foregroundStyle(.secondary) }
                    ForEach(model.instructions) { item in
                        NavigationLink {
                            InstructionDetail(id: item.id)
                        } label: {
                            VStack(alignment: .leading, spacing: 8) {
                                Text(item.stateLabel).font(.caption).foregroundStyle(.mint)
                                Text(item.message).lineLimit(3)
                                Text(item.createdAt).font(.caption2).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
            .navigationTitle("투자 지시")
            .toolbar { Button { Task { await model.refreshInstructions() } } label: {
                if model.instructionBusy { ProgressView() } else { Image(systemName: "arrow.clockwise") }
            }.disabled(model.instructionBusy || !model.profile.isConfigured || model.demo) }
            .refreshable { await model.refreshInstructions() }
            .task { if model.profile.isConfigured { await model.refreshInstructions() } }
        }
    }
}

struct InstructionDetail: View {
    @EnvironmentObject private var model: AppModel
    let id: String
    var body: some View {
        List {
            if let item = model.instructions.first(where: { $0.id == id }) {
                Section(item.stateLabel) { Text(item.message).textSelection(.enabled) }
                Section("처리 결과") { Text(item.result ?? "아직 처리 결과가 없습니다.").textSelection(.enabled) }
                Section("처리 이력") {
                    ForEach(Array(item.events.enumerated()), id: \.offset) { _, event in
                        VStack(alignment: .leading) {
                            Text(event.state); Text(event.at).font(.caption).foregroundStyle(.secondary)
                            if let result = event.result { Text(result) }
                        }
                    }
                }
                Section { Text(item.id).font(.caption.monospaced()).textSelection(.enabled) }
                if item.state == "waiting" {
                    Button("대기 중인 지시 취소", role: .destructive) { Task { await model.cancelInstruction(id) } }
                        .disabled(model.instructionBusy)
                }
            }
            if let notice = model.instructionNotice { Text(notice).foregroundStyle(.secondary) }
        }.navigationTitle("지시 상세")
            .refreshable { await model.refreshInstruction(id) }
            .task { await model.refreshInstruction(id) }
    }
}
