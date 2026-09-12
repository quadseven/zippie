import Network
import XCTest
@testable import ZippieCompanionKit

/// `.waiting` must say WHY (#67 follow-up).
///
/// The relay reported one fixed sentence for every waiting cellular
/// connection - "cellular unavailable (interface not usable)" - and the
/// comment beside it guessed at aeroplane mode, no data plan, and Low Data
/// Mode. Measured 2026-09-11 on a phone that showed 5G in the status bar,
/// had cellular granted to this app (9.69 GB attributed to it) and Local
/// Network granted: all three guesses were false, the relay sat in this
/// state indefinitely, and the real reason was in the NWError that
/// `.waiting` hands over and the code discarded.
///
/// These pin the mapping, which is the only part testable off-device:
/// delivering a real NWConnection state needs a device and a live radio.
final class CellularWaitingReasonTests: XCTestCase {

    func testAnInterfaceThatIsDownSaysSo() {
        XCTAssertEqual(CellularRelay.describe(.posix(.ENETDOWN)),
                       "the cellular interface is down")
    }

    func testNoRouteIsDistinctFromNoInterface() {
        XCTAssertNotEqual(CellularRelay.describe(.posix(.ENETUNREACH)),
                          CellularRelay.describe(.posix(.ENETDOWN)),
                          "a radio with no route is a different fault from a "
                        + "radio that is off, and they need different remedies")
    }

    /// The failure this design invites, and the one the old fixed string hid
    /// most effectively. With requiredInterfaceType = .cellular the HOME
    /// HOSTNAME must resolve over cellular as well - a name that only answers
    /// through the router's own resolver never resolves here, and reads to an
    /// operator as "no signal" on a phone with full bars.
    func testAResolutionFailureIsNamedAsOneRatherThanAsNoSignal() {
        let text = CellularRelay.describe(.dns(-65554))
        XCTAssertTrue(text.contains("resolve"), "got: \(text)")
        XCTAssertTrue(text.contains("cellular"), "got: \(text)")
        XCTAssertFalse(text.contains("interface not usable"),
                       "a DNS failure must never be reported as a dead interface")
    }

    /// A permissions fault points at the two settings that cause it, because
    /// that is the whole remedy and an operator should not have to guess.
    func testAPermissionFaultNamesTheSettingToCheck() {
        let text = CellularRelay.describe(.posix(.EPERM))
        XCTAssertTrue(text.contains("Settings"), "got: \(text)")
        XCTAssertTrue(text.contains("Low Data Mode"), "got: \(text)")
    }

    /// No cause may be reported as the old catch-all sentence, which is what
    /// made a real fault indistinguishable from three imagined ones.
    func testNoReasonFallsBackToTheOldGuess() {
        for e: NWError in [.posix(.ENETDOWN), .posix(.ENETUNREACH),
                           .posix(.EHOSTUNREACH), .posix(.ETIMEDOUT),
                           .posix(.EPERM), .dns(-65554)] {
            XCTAssertFalse(CellularRelay.describe(e).contains("interface not usable"),
                           "\(e) still reports the old catch-all")
        }
    }
}
