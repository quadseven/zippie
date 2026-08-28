import XCTest
@testable import ZippieCompanionKit

/// The router's rule, copied from `dynamic.py`, so these tests fail if the two
/// ever drift rather than only failing on a device at announce time.
private let routerRule = try! NSRegularExpression(pattern: "^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$")

private func routerAccepts(_ name: String) -> Bool {
    let r = NSRange(name.startIndex..., in: name)
    return routerRule.firstMatch(in: name, range: r) != nil
}

final class LegNameTests: XCTestCase {

    // MARK: - what the router will take

    func testComposedNamesAlwaysSatisfyTheRouterRegex() {
        // The awkward cases, not the easy one: an apostrophe, a double space,
        // a trailing separator, emoji, a non-Latin script, and a name far over
        // the limit.
        let devices = [
            "Operator's iPhone",
            "Co-operator's  iPhone 15 Pro Max",
            "iPhone -- ",
            "iPhone",
            "12345",
            "x",
            "",
            "телефон",
            String(repeating: "Extremely Long Device Name ", count: 4),
        ]
        for d in devices {
            let name = LegName.compose(base: d, suffix: "3f9a")
            XCTAssertTrue(routerAccepts(name), "router would refuse \(name) from \(d)")
            XCTAssertTrue(LegName.isValid(name), "our own validator disagrees about \(name)")
        }
    }

    /// The truncation has to eat the BASE. Trimming the composed string would
    /// shorten the suffix, which is the one part that must not be shortened.
    func testOverlongNameKeepsTheWholeSuffix() {
        let name = LegName.compose(base: String(repeating: "abcdefghij", count: 6), suffix: "b204")
        XCTAssertLessThanOrEqual(name.count, LegName.maxLength)
        XCTAssertTrue(name.hasSuffix("-b204"), "suffix was cut: \(name)")
        XCTAssertTrue(routerAccepts(name))
    }

    /// If the base is trimmed to exactly a hyphen boundary, the naive result is
    /// "abc--b204" or "-b204". Both are wrong in different ways.
    func testTrimmingOntoASeparatorDoesNotLeaveADoubleHyphen() {
        // 27 characters of base, so the 27-char room boundary lands on a space.
        let name = LegName.compose(base: "aaaaaaaaaaaaaaaaaaaaaaaaaaa bbbb", suffix: "0001")
        XCTAssertFalse(name.contains("--"), name)
        XCTAssertTrue(routerAccepts(name), name)
    }

    /// A name made only of characters that sanitise away must not become "-abcd"
    /// or "" - it falls back to something announceable.
    func testUnusableDeviceNameFallsBackRatherThanFailing() {
        for d in ["", "  ", "!!!", "телефон"] {
            let name = LegName.compose(base: d, suffix: "beef")
            XCTAssertEqual(name, "phone-beef", "from \(d.debugDescription)")
            XCTAssertTrue(routerAccepts(name))
        }
    }

    /// iOS names devices "<Name>'s iPhone" using U+2019, not the ASCII quote.
    /// Handling only the typed apostrophe would leave every real device with a
    /// name reading "operator-s-iphone".
    func testApostrophesDisappearRatherThanSplittingTheWord() {
        XCTAssertEqual(LegName.sanitise("Operator\u{2019}s iPhone"), "operators-iphone")
        XCTAssertEqual(LegName.sanitise("Operator's iPhone"), "operators-iphone")
        XCTAssertEqual(LegName.sanitise("Co-operator\u{2019}s iPhone 15 Pro"), "co-operators-iphone-15-pro")
    }

    // MARK: - the collision this whole type exists to prevent

    func testTwoPhonesWithTheSameDeviceNameGetDifferentLegNames() {
        // Deterministic stand-ins for the random source, because a test that
        // relies on real randomness to prove distinctness proves nothing.
        let mine = LegName.compose(base: "iPhone", suffix: LegName.newSuffix { 0x3f9a })
        let theirs = LegName.compose(base: "iPhone", suffix: LegName.newSuffix { 0xb204 })
        XCTAssertNotEqual(mine, theirs)
        XCTAssertEqual(mine, "iphone-3f9a")
        XCTAssertEqual(theirs, "iphone-b204")
    }

    func testSuffixIsAlwaysFourHexCharacters() {
        // Including the values that would produce a short string under a naive
        // String(_, radix: 16).
        for value: UInt32 in [0, 1, 0xF, 0xFF, 0xFFF, 0xFFFF] {
            let s = LegName.newSuffix { value }
            XCTAssertEqual(s.count, 4, "suffix for \(value) was \(s)")
        }
    }

    // MARK: - stability

    func testResolveMintsOnceAndThenReturnsTheSameName() {
        let d = UserDefaults(suiteName: "LegNameTests-\(UUID().uuidString)")!
        let first = LegName.resolve(in: d, deviceName: "Operator's iPhone")
        let second = LegName.resolve(in: d, deviceName: "Operator's iPhone")
        XCTAssertEqual(first, second, "the name must not change between launches")
        XCTAssertTrue(first.hasPrefix("operators-iphone-"), first)
    }

    /// Renaming the phone in iOS Settings must NOT rename the leg. A new name
    /// is a new leg to the router, so the old one lingers until its lease runs
    /// out and the leg list shows the same phone twice.
    func testRenamingTheDeviceDoesNotRenameTheLeg() {
        let d = UserDefaults(suiteName: "LegNameTests-\(UUID().uuidString)")!
        let before = LegName.resolve(in: d, deviceName: "Operator's iPhone")
        let after = LegName.resolve(in: d, deviceName: "Operator's New iPhone")
        XCTAssertEqual(before, after)
    }

    /// A stored value that the router would refuse - written by an older build,
    /// or corrupted - must be replaced rather than announced and rejected.
    func testAnInvalidStoredNameIsReplaced() {
        let d = UserDefaults(suiteName: "LegNameTests-\(UUID().uuidString)")!
        d.set("Operator's iPhone", forKey: "legName")  // never valid: capitals, space, apostrophe
        let resolved = LegName.resolve(in: d, deviceName: "Operator's iPhone")
        XCTAssertTrue(routerAccepts(resolved), resolved)
    }

    // MARK: - validator agrees with the router in both directions

    func testValidatorRejectsExactlyWhatTheRouterRejects() {
        let cases = ["a", "", "-abc", "abc-", "ABC", "ab_c", "a b", "ab",
                     String(repeating: "a", count: 32), String(repeating: "a", count: 33),
                     "1", "12", "a-b", "телефон"]
        for c in cases {
            XCTAssertEqual(LegName.isValid(c), routerAccepts(c),
                           "disagreement on \(c.debugDescription): "
                         + "ours=\(LegName.isValid(c)) router=\(routerAccepts(c))")
        }
    }
}
