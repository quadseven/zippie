import XCTest
@testable import ZippieCompanionKit

/// A repeater leg names itself from the SSID it is associated to (#153), and an
/// SSID is arbitrary bytes chosen by whoever runs the access point - spaces,
/// accents, emoji, CJK. The agent already proves the label survives its own
/// JSON round trip; this is the other half of that acceptance criterion, which
/// requires the label to survive "round-trip to BOTH apps".
///
/// Worth testing rather than assuming. `label` is the only leg field derived
/// from a string this project does not control, so it is the only place where
/// an access point called `Café ☕` can produce a row reading `Caf?` on a phone
/// in a car, with nobody able to explain why. That failure is silent and looks
/// cosmetic, which is the kind that survives for months.
final class LegLabelRoundTripTests: XCTestCase {

    private func label(fromSSIDLabel raw: String) throws -> String? {
        // Built through JSONSerialization rather than string interpolation so
        // the escaping under test is the DECODER's, not this test's.
        let payload: [String: Any] = [
            "mode": "aggregate",
            "datapath": "packet",
            "paths": [["name": "hotspot", "label": raw, "state": "up"]],
        ]
        let data = try JSONSerialization.data(withJSONObject: payload)
        let status = try JSONDecoder().decode(BondStatus.self, from: data)
        return try XCTUnwrap(status.paths?.first).label
    }

    func testSpacesSurvive() throws {
        XCTAssertEqual(try label(fromSSIDLabel: "Wi-Fi Repeater - Guest Network 5G"),
                       "Wi-Fi Repeater - Guest Network 5G")
    }

    func testAccentedCharactersSurvive() throws {
        XCTAssertEqual(try label(fromSSIDLabel: "Wi-Fi Repeater - Café"),
                       "Wi-Fi Repeater - Café")
    }

    func testNonLatinScriptsSurvive() throws {
        XCTAssertEqual(try label(fromSSIDLabel: "Wi-Fi Repeater - 東京カフェ"),
                       "Wi-Fi Repeater - 東京カフェ")
    }

    /// Surrogate pairs. A decoder counting bytes rather than characters
    /// truncates here, and the damage reads as a font problem.
    func testEmojiOutsideTheBasicPlaneSurvive() throws {
        XCTAssertEqual(try label(fromSSIDLabel: "Wi-Fi Repeater - ☕ Coffee 🚀"),
                       "Wi-Fi Repeater - ☕ Coffee 🚀")
    }

    /// iwinfo does not escape a quote embedded in an SSID, which the agent's
    /// parser already recovers from; this pins that the app does not re-break
    /// it on the way back out.
    func testEmbeddedQuoteSurvives() throws {
        XCTAssertEqual(try label(fromSSIDLabel: #"Wi-Fi Repeater - Bob"s AP"#),
                       #"Wi-Fi Repeater - Bob"s AP"#)
    }

    /// The auto label is deliberately ABSENT for an unassociated station rather
    /// than holding a stale SSID or the literal word "unknown", so the app has
    /// to cope with the field simply not being there.
    func testAMissingLabelDecodesAsNilRatherThanFailing() throws {
        let data = try JSONSerialization.data(withJSONObject: [
            "mode": "aggregate",
            "paths": [["name": "hotspot", "state": "up"]],
        ])
        let status = try JSONDecoder().decode(BondStatus.self, from: data)
        XCTAssertNil(try XCTUnwrap(status.paths?.first).label)
    }
}
