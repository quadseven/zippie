import XCTest
@testable import ZippieCompanionKit

final class NextDNSProfileTests: XCTestCase {

    func testTheDohUrlCarriesProfileAndDeviceName() {
        let p = NextDNSProfile(profileID: "abc123", deviceName: "operator-iphone")
        XCTAssertEqual(p.dohURL?.absoluteString,
                       "https://dns.nextdns.io/abc123/operator-iphone")
    }

    /// Attribution still works without a device name; NextDNS just cannot tell
    /// this phone from anything else on the same profile.
    func testAProfileWithNoDeviceNameStillResolves() {
        let p = NextDNSProfile(profileID: "abc123")
        XCTAssertEqual(p.dohURL?.absoluteString, "https://dns.nextdns.io/abc123")
    }

    /// THE POINT OF THE WHOLE SLICE: two people, two profiles, two names.
    func testTwoUsersGetGenuinelyDifferentEndpoints() {
        let operatorProfile = NextDNSProfile(profileID: "aaa111", deviceName: "operator-iphone")
        let coOperatorProfile = NextDNSProfile(profileID: "bbb222", deviceName: "co-operator-iphone")
        XCTAssertNotEqual(operatorProfile.dohURL, coOperatorProfile.dohURL)
    }

    /// A typo must be refused while the user is still looking at the field. DNS
    /// silently resolving nothing is indistinguishable from a broken network.
    func testAnInvalidProfileIdProducesNoUrlAtAll() {
        for bad in ["", "ab", "has spaces", "WAY-too-long-to-be-a-profile-id", "a/b"] {
            let p = NextDNSProfile(profileID: bad, deviceName: "x")
            XCTAssertFalse(p.isValid, "\(bad.debugDescription) was accepted")
            XCTAssertNil(p.dohURL, "\(bad.debugDescription) produced a URL")
        }
    }

    /// Device names ride in a URL path segment. Anything needing escaping is
    /// replaced, because a percent-escaped name in the NextDNS dashboard reads
    /// as a bug rather than as a name.
    func testDeviceNamesAreSanitisedNotEscaped() {
        let p = NextDNSProfile(profileID: "abc123", deviceName: "Operator's iPhone 17/Pro")
        let url = p.dohURL?.absoluteString ?? ""
        XCTAssertFalse(url.contains("%"), "name was percent-escaped: \(url)")
        XCTAssertFalse(url.contains("'"), url)
        XCTAssertTrue(url.hasPrefix("https://dns.nextdns.io/abc123/"), url)
    }

    func testTrailingSeparatorsAreTrimmed() {
        let p = NextDNSProfile(profileID: "abc123", deviceName: "!!!")
        XCTAssertEqual(p.dohURL?.absoluteString, "https://dns.nextdns.io/abc123")
    }

    /// Android takes a hostname, not a URL.
    func testDotHostnameForAndroidPrivateDns() {
        XCTAssertEqual(NextDNSProfile(profileID: "abc123").dotHostname,
                       "abc123.dns.nextdns.io")
        XCTAssertNil(NextDNSProfile(profileID: "!!").dotHostname)
    }

    func testDoHURLRoundTripsTheInstalledIdentity() throws {
        let profile = NextDNSProfile(profileID: "abc123", deviceName: "operator-iphone")
        XCTAssertEqual(NextDNSProfile(dohURL: try XCTUnwrap(profile.dohURL)), profile)
    }

    func testAnotherDoHProviderIsNotMislabelledAsNextDNS() throws {
        let url = try XCTUnwrap(URL(string: "https://dns.example.test/dns-query"))
        XCTAssertNil(NextDNSProfile(dohURL: url))
    }
}
