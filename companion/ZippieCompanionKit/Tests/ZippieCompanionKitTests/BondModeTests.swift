import XCTest
@testable import ZippieCompanionKit

/// The mode decision, which is the one piece of logic that can put the phone in
/// the wrong job entirely.
final class BondModeTests: XCTestCase {

    /// On the router's LAN, lend it cellular.
    func testLocalMeansContribute() {
        XCTAssertEqual(ModeDecision(proximity: .local).mode, .contribute)
    }

    /// THE ONE THAT MATTERS. The console answers over the tailnet from anywhere
    /// on earth. Treating "the router replied" as "the router is here" would
    /// put a phone in a hotel in another state into contribute mode, holding a
    /// cellular socket open for a bond that cannot hear it.
    func testReachableOverTailnetIsNotTheSameAsBeingOnItsNetwork() {
        XCTAssertEqual(ModeDecision(proximity: .remote).mode, .client,
                       "a router reachable over the tailnet was treated as nearby")
    }

    func testUnreachableFallsBackToClient() {
        XCTAssertEqual(ModeDecision(proximity: .unreachable).mode, .client)
    }

    /// Client is the safe default: being wrong that way bonds this phone's own
    /// links home, which is useful nearly everywhere. Being wrong the other way
    /// spends metered data on nothing.
    func testOnlyPositiveLocalEvidenceEverSelectsContribute() {
        for p in [RouterProximity.remote, .unreachable] {
            XCTAssertEqual(ModeDecision(proximity: p).mode, .client)
        }
    }

    /// Before the first probe the phone has no evidence, and a confident mode
    /// label would be a claim without one.
    func testUndeterminedSaysSoRatherThanGuessing() {
        let d = ModeDecision(proximity: .unreachable, undetermined: true)
        XCTAssertEqual(d.label, "Checking")
        XCTAssertTrue(d.summary().contains("Working out"))
    }

    /// Every state explains itself. "Why am I in client mode" is the hardest
    /// question this app has to answer and the enum alone cannot answer it.
    func testEveryProximityExplainsItself() {
        for p in [RouterProximity.local, .remote, .unreachable] {
            let s = ModeDecision(proximity: p).summary()
            XCTAssertFalse(s.isEmpty)
            XCTAssertGreaterThan(s.count, 20, "\(p) has no real explanation")
        }
    }

    // MARK: - naming the router when unreachable (#44 operator follow-up, 2026-08-08)

    /// "The router is not answering" next to a working wifi router the phone
    /// IS joined to reads as a claim about that router. Without a name, the
    /// sentence must still say it means the zippie router, not the wifi one.
    func testUnreachableWithNoNameSaysYourZippieRouterRatherThanTheRouter() {
        let s = ModeDecision(proximity: .unreachable).summary()
        XCTAssertTrue(s.hasPrefix("Your zippie router"), s)
        XCTAssertFalse(s.contains("The router"), s)
    }

    /// Given a name, the sentence names it - "suzu is not answering" tells
    /// the reader which box to go and look at.
    func testUnreachableWithANameUsesIt() {
        let s = ModeDecision(proximity: .unreachable).summary(router: "suzu")
        XCTAssertTrue(s.hasPrefix("suzu is not answering"), s)
    }

    /// `.local` and `.remote` already describe a relationship this phone just
    /// proved by reaching an address - naming must not perturb those.
    func testNamingOnlyAffectsTheUnreachableSentence() {
        for p in [RouterProximity.local, .remote] {
            let d = ModeDecision(proximity: p)
            XCTAssertEqual(d.summary(router: "suzu"), d.summary())
        }
    }
}
