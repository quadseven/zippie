import XCTest
@testable import ZippieCompanionKit

/// Announcing replaces a static config entry, and the tests are about the ways
/// that can silently not happen.
final class LegAnnouncerTests: XCTestCase {

    private func config(host: String = "10.99.0.1:8787",
                        token: String = "tok",
                        port: UInt16 = 51999) -> LegAnnouncer.Config {
        .init(consoleHost: host, token: token, name: "operator-iphone",
              label: "iPhone (Verizon)", listenPort: port)
    }

    private func announcer(_ handler: @escaping (URLRequest) -> (Int, Data)) -> LegAnnouncer {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.protocolClasses = [StubProtocol.self]
        StubProtocol.handler = handler
        return LegAnnouncer(session: URLSession(configuration: cfg))
    }

    /// NO ADDRESS IS NOT AN ERROR, and it must not announce a wrong one. Off a
    /// local network there is nothing the router could dial.
    func testWithoutALocalAddressNothingIsAnnounced() async {
        var called = false
        let a = announcer { _ in called = true; return (200, Data("{}".utf8)) }
        let outcome = await a.announce(config(), address: nil)
        XCTAssertFalse(called, "it announced with no address to announce")
        if case .refused = outcome {} else { XCTFail("expected refused, got \(outcome)") }
    }

    func testAnAnnouncementCarriesEverythingTheRouterNeeds() async throws {
        var seen: [String: Any] = [:]
        let a = announcer { req in
            if let b = req.httpBodyData,
               let o = try? JSONSerialization.jsonObject(with: b) as? [String: Any] { seen = o }
            return (200, Data(#"{"lease_s":45}"#.utf8))
        }
        _ = await a.announce(config(), address: "10.99.0.151")

        XCTAssertEqual(seen["name"] as? String, "operator-iphone")
        XCTAssertEqual(seen["host"] as? String, "10.99.0.151")
        XCTAssertEqual(seen["port"] as? Int, 51999)
        XCTAssertEqual(seen["label"] as? String, "iPhone (Verizon)")
        XCTAssertNotNil(seen["lease_s"], "no lease requested, so the router would use its default")
    }

    func testTheBearerTokenIsSent() async {
        var auth: String?
        let a = announcer { req in
            auth = req.value(forHTTPHeaderField: "Authorization")
            return (200, Data("{}".utf8))
        }
        _ = await a.announce(config(token: "s3cret"), address: "10.99.0.151")
        XCTAssertEqual(auth, "Bearer s3cret")
    }

    /// The router says exactly which field it refused. Replacing that with a
    /// friendlier sentence loses the only actionable part.
    func testTheRoutersOwnRefusalSurvives() async {
        let a = announcer { _ in
            (400, Data(#"{"error":"host must be a private IPv4 address on this LAN"}"#.utf8))
        }
        let outcome = await a.announce(config(), address: "8.8.8.8")
        guard case let .refused(msg) = outcome else { return XCTFail("expected refused") }
        XCTAssertTrue(msg.contains("private IPv4"), "lost the router's reason: \(msg)")
    }

    func testABadTokenIsReportedAsRefusedNotUnreachable() async {
        let a = announcer { _ in (401, Data(#"{"error":"bad or missing bearer token"}"#.utf8)) }
        let outcome = await a.announce(config(), address: "10.99.0.151")
        guard case let .refused(msg) = outcome else { return XCTFail("expected refused") }
        XCTAssertTrue(msg.contains("token"))
    }

    /// A router that is not there is a different state from one that said no,
    /// and conflating them sends someone to check the wrong thing.
    func testAnUnreachableRouterIsDistinctFromARefusal() async {
        let a = announcer { _ in (0, Data()) }
        let outcome = await a.announce(config(), address: "10.99.0.151")
        if case .unreachable = outcome {} else { XCTFail("expected unreachable, got \(outcome)") }
    }

    func testWithdrawNamesTheLeg() async {
        var seen: [String: Any] = [:]
        var path = ""
        let a = announcer { req in
            path = req.url?.path ?? ""
            if let b = req.httpBodyData,
               let o = try? JSONSerialization.jsonObject(with: b) as? [String: Any] { seen = o }
            return (200, Data(#"{"withdrawn":true}"#.utf8))
        }
        await a.withdraw(config())
        XCTAssertEqual(path, "/api/legs/withdraw")
        XCTAssertEqual(seen["name"] as? String, "operator-iphone")
    }

    /// The renewal interval must be comfortably inside the lease, or two
    /// missed announcements drop the leg.
    func testRenewalIsWellInsideTheLease() {
        XCTAssertLessThan(LegAnnouncer.renewInterval * 2, LegAnnouncer.leaseSeconds,
                          "two missed renewals would expire the lease")
    }

    func testAnUnconfiguredAnnouncerRefusesRatherThanCrashing() async {
        let a = announcer { _ in (200, Data("{}".utf8)) }
        for c in [config(host: ""), config(token: ""), config(port: 0)] {
            let outcome = await a.announce(c, address: "10.99.0.151")
            if case .refused = outcome {} else { XCTFail("expected refused for \(c)") }
        }
    }
}

/// Captures the body, which URLProtocol otherwise strips into a stream.
final class StubProtocol: URLProtocol {
    nonisolated(unsafe) static var handler: ((URLRequest) -> (Int, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let (code, data) = Self.handler?(request) ?? (200, Data())
        if code == 0 {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost))
            return
        }
        let resp = HTTPURLResponse(url: request.url!, statusCode: code,
                                   httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: resp, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

extension URLRequest {
    /// httpBody is nil once URLSession converts it to a stream.
    var httpBodyData: Data? {
        if let b = httpBody { return b }
        guard let s = httpBodyStream else { return nil }
        s.open(); defer { s.close() }
        var out = Data(); var buf = [UInt8](repeating: 0, count: 4096)
        while s.hasBytesAvailable {
            let n = s.read(&buf, maxLength: buf.count)
            if n <= 0 { break }
            out.append(buf, count: n)
        }
        return out
    }
}
