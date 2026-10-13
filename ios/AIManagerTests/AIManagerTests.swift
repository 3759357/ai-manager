import XCTest
@testable import AIManager

final class AIManagerTests: XCTestCase {
    func testInstructionContractIncludesResultAndHistory() throws {
        let data = Data(#"{"schema_version":1,"instructions":[{"id":"phone-1","message":"비중 검토","state":"waiting","created_at":"now","updated_at":"now","result":null,"events":[{"state":"waiting","at":"now","result":null}]}]}"#.utf8)
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let list = try decoder.decode(InstructionList.self, from: data)
        XCTAssertEqual(list.instructions.first?.stateLabel, "검토 대기")
        XCTAssertNil(list.instructions.first?.result)
        XCTAssertEqual(list.instructions.first?.events.count, 1)
    }
    func testCommandsQuoteEveryArgument() {
        var profile = ServerProfile()
        profile.directory = "/srv/ai-manager's project; touch /tmp/should-not-run"
        let command = RemoteCommand.make(profile: profile, arguments: ["request", "$(id)", "--request-id", "one"])
        XCTAssertTrue(command.hasPrefix("cd '/srv/ai-manager'\\''s project; touch /tmp/should-not-run' && "))
        XCTAssertTrue(command.hasSuffix("'request' '$(id)' '--request-id' 'one'"))
        XCTAssertFalse(command.contains("sudo"))
    }

    func testProfileValidation() throws {
        var profile = ServerProfile()
        XCTAssertThrowsError(try profile.validate())
        profile.host = "example.invalid"; profile.hostKey = "ssh-ed25519 AAAA"
        XCTAssertNoThrow(try profile.validate())
        profile.port = "65536"
        XCTAssertThrowsError(try profile.validate())
        profile.port = "22"; profile.directory = "relative"
        XCTAssertThrowsError(try profile.validate())
    }

    func testPythonReportContractKeepsFractionalValues() throws {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "report-fixture", withExtension: "json"))
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let report = try decoder.decode(AccountReport.self, from: Data(contentsOf: url))
        XCTAssertEqual(report.schemaVersion, 1)
        XCTAssertEqual(report.positions.first?.quantity, "0.01313")
        XCTAssertEqual(report.marketValueByCurrency["KRW"], "2000")
        XCTAssertEqual(report.marketValueByCurrency["USD"], "0.9429966")
        XCTAssertEqual(report.recentOrders?.first?.filledQuantity, "0.01313")
        XCTAssertEqual(report.recentOrders?.first?.commission, "0")
    }

    func testPendingReceiptHasNoReportAndLocalDoesNotClaimDiscordDelivery() throws {
        let data = Data(#"{"schema_version":1,"requests":[{"id":"id","state":"pending","created_at":"now","channel":"local","report_summary":null,"error":null}]}"#.utf8)
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let list = try decoder.decode(ReportList.self, from: data)
        XCTAssertNil(list.requests.first?.reportSummary)
        XCTAssertEqual(list.requests.first?.stateLabel, "계좌 조회 대기")
        XCTAssertEqual(DemoData.receipt.stateLabel, "보고서 생성 완료")
    }
}
