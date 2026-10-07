import XCTest
import UIKit
@testable import AIManager

final class ShellTests: XCTestCase {
    @MainActor
    func testShowKeyboardResetsSymbolsOnlyInputView() {
        let session = ShellSession()
        session.terminal.inputView = UIView()
        session.terminal.inputAccessoryView = UIView()
        session.showKeyboard()
        XCTAssertNil(session.terminal.inputView)
        XCTAssertNil(session.terminal.inputAccessoryView)
    }
    func testDuplicateHostKeyIsRejectedBeforeSaving() {
        // Synthetic Ed25519 public key, not an account credential.
        let key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        XCTAssertNoThrow(try SSHService.validateHostKey(key))
        XCTAssertThrowsError(try SSHService.validateHostKey(key + key))
    }
    func testControlKeysAreRawTerminalBytes() {
        XCTAssertEqual(ShellKey.interrupt.bytes, [3])
        XCTAssertEqual(ShellKey.tab.bytes, [9])
        XCTAssertEqual(ShellKey.up.bytes, [27, 91, 65])
    }

    @MainActor
    func testTerminalProcessesANSIAndSplitUTF8() {
        let session = ShellSession()
        session.terminal.resize(cols: 80, rows: 24)
        let text = "\u{1b}[2J\u{1b}[H\u{1b}[32m우분투\u{1b}[0m\r\nhello"
        for byte in text.utf8 { session.terminal.feed(byteArray: [byte][...]) }
        // Wide glyphs occupy two terminal cells; the trailing cell exports as NUL.
        let output = String(decoding: session.terminal.getTerminal().getBufferAsData(), as: UTF8.self)
            .replacingOccurrences(of: "\0", with: "")
        XCTAssertTrue(output.contains("우분투"), output.debugDescription)
        XCTAssertTrue(output.contains("hello"))
        XCTAssertFalse(output.contains("[32m"))
    }

    @MainActor
    func testDisconnectResetsConnectionStateWithoutReplayingInput() {
        let session = ShellSession()
        session.send(Array("echo should-not-queue\r".utf8))
        session.disconnect("background")
        XCTAssertFalse(session.connected)
        XCTAssertFalse(session.connecting)
        XCTAssertEqual(session.status, "background")
        let output = String(decoding: session.terminal.getTerminal().getBufferAsData(), as: UTF8.self)
        XCTAssertFalse(output.contains("should-not-queue"))
    }
}
