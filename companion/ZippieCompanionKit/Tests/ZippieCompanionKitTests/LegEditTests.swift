import XCTest
@testable import ZippieCompanionKit

/// The write path, tested without a router.
///
/// EVERY BODY IN THIS FILE IS THE ROUTER'S OWN. The status payload was fetched
/// from the live console on 2026-08-05 (`GET /api/status`, leg
/// companion-iphone) and the 401 body is the verbatim reply that console gave
/// to a PUT with no Authorization header. Writing these from the Swift structs
/// instead would test that this file agrees with itself, which is how the
/// previous decoder shipped matching on a field the router never published.
///
/// The failure this file exists to catch is a WRITE REPORTED AS APPLIED WHEN
/// IT WAS NOT. A 200 is not evidence on its own - the router echoes the entry
/// it stored, and only that echo says which fields survived.
final class LegEditTests: XCTestCase {

    // MARK: - transports

    /// Captures the request so a test can assert about what went on the wire,
    /// and about calls that must NEVER be made.
    private final class Wire: @unchecked Sendable {
        var requests: [URLRequest] = []

        func replying(_ status: Int, _ body: String) -> LegEditClient.Transport {
            { [self] request in
                requests.append(request)
                let response = HTTPURLResponse(url: request.url!, statusCode: status,
                                               httpVersion: "HTTP/1.1", headerFields: nil)!
                return (Data(body.utf8), response)
            }
        }

        /// A transport that fails the test if it is ever reached.
        func refusing() -> LegEditClient.Transport {
            { request in
                XCTFail("the client went to the network when it should not have: \(request)")
                throw URLError(.badURL)
            }
        }
    }

    private let console = URL(string: "http://10.99.0.1:8787/api/status")!

    /// Captured from the live console. Trimmed to one leg; every key and value
    /// is as the router emitted it.
    private let liveStatus = """
    {"mode": "bond", "datapath": "packet", "primary": "hotspot", "paths": [
      {"name": "companion-iphone", "interface": "br-lan", "wg_iface": null,
       "port": 51901, "address": "10.66.0.13/32", "state": "up",
       "rtt_ms": 41.2, "loss_pct": 0.0, "tx_bytes": 0, "rx_bytes": 0,
       "tx_bps": null, "rx_bps": null,
       "effective_weight": 16, "config_weight": 60,
       "label": "iPhone (Verizon)", "tier": 2, "priority": 40,
       "cost_class": "metered", "monthly_cap_gb": 50.0, "max_kbps": 0,
       "usage_gb": 0.0, "over_soft_limit": false, "ssid": null,
       "last_error": null, "peer_endpoint": null,
       "peer_endpoint_private": false, "has_gateway": false,
       "relay_endpoint": "10.99.0.151:51999", "in_bond": false,
       "link_id": 3, "link_tx_bytes": 0, "link_rx_bytes": 0}
    ]}
    """

    // MARK: - what goes on the wire

    /// The router 400s an unknown key. These spellings are the contract.
    func testTheBodyUsesTheRoutersOwnFieldNames() throws {
        var edit = LegEdit()
        edit.set(.tier, .int(2))
        edit.set(.maxKbps, .int(500))
        edit.set(.monthlyCapGB, .number(15.5))
        edit.set(.planName, .text("Unlimited Plus"))

        let json = try JSONSerialization.jsonObject(with: edit.body()) as? [String: Any]
        XCTAssertEqual(Set((json ?? [:]).keys),
                       ["tier", "max_kbps", "monthly_cap_gb", "plan_name"])
    }

    /// A null CLEARS an override. Sending 0 instead would leave the override in
    /// place shadowing zippie.toml, which looks identical on the status screen
    /// and is not the same thing at all.
    func testClearingAFieldSendsJSONNull() throws {
        var edit = LegEdit()
        edit.clear(.maxKbps)
        let body = String(decoding: try edit.body(), as: UTF8.self)
        XCTAssertTrue(body.contains("\"max_kbps\":null"), body)
    }

    func testTheWriteURLIsDerivedFromTheStatusURL() {
        let url = LegEditClient.writeURL(console: console, leg: "companion-iphone")
        XCTAssertEqual(url?.absoluteString,
                       "http://10.99.0.1:8787/api/legs/companion-iphone")
    }

    /// A slash in a leg name must not become a path separator - that would
    /// address a different endpoint instead of 404ing honestly.
    func testALegNameIsEscapedIntoASinglePathSegment() {
        let url = LegEditClient.writeURL(console: console, leg: "co-operator phone/2")
        XCTAssertEqual(url?.absoluteString,
                       "http://10.99.0.1:8787/api/legs/co-operator%20phone%2F2")
    }

    func testAConsoleAddressWithNoHostIsRefusedRatherThanGuessedAt() async {
        let wire = Wire()
        let client = LegEditClient(transport: wire.refusing())
        var edit = LegEdit()
        edit.set(.tier, .int(1))

        let result = await client.apply(edit, to: "hotspot",
                                        console: URL(string: "/api/status")!,
                                        token: "tok")
        guard case .failure(.badConsoleAddress) = result else {
            return XCTFail("expected badConsoleAddress, got \(result)")
        }
    }

    func testTheTokenIsSentAsABearerHeaderOnAPUT() async throws {
        let wire = Wire()
        let client = LegEditClient(transport: wire.replying(200, #"{"leg":"hotspot","applied":{"tier":1}}"#))
        var edit = LegEdit()
        edit.set(.tier, .int(1))

        _ = await client.apply(edit, to: "hotspot", console: console, token: "s3cret")

        let sent = try XCTUnwrap(wire.requests.first)
        XCTAssertEqual(sent.httpMethod, "PUT")
        XCTAssertEqual(sent.value(forHTTPHeaderField: "Authorization"), "Bearer s3cret")
    }

    // MARK: - nothing sent is not a failure to report as one

    func testAnEmptyEditNeverReachesTheNetwork() async {
        let wire = Wire()
        let client = LegEditClient(transport: wire.refusing())

        let result = await client.apply(LegEdit(), to: "hotspot",
                                        console: console, token: "tok")
        guard case .failure(.nothingToSend) = result else {
            return XCTFail("expected nothingToSend, got \(result)")
        }
    }

    /// A blank token would produce a 401 from the router that reads as "your
    /// token is wrong" when the truth is that there is no token at all.
    func testAMissingTokenIsCaughtBeforeTheNetwork() async {
        let wire = Wire()
        let client = LegEditClient(transport: wire.refusing())
        var edit = LegEdit()
        edit.set(.tier, .int(1))

        let result = await client.apply(edit, to: "hotspot", console: console, token: "  \n ")
        guard case .failure(.tokenMissing) = result else {
            return XCTFail("expected tokenMissing, got \(result)")
        }
    }

    // MARK: - the three refusals, told apart

    func testABadTokenIsReportedAsAnAuthorisationFailure() async {
        let wire = Wire()
        // The live console's verbatim reply to a PUT with no token.
        let client = LegEditClient(transport: wire.replying(401, #"{"error": "bad or missing bearer token"}"#))
        var edit = LegEdit()
        edit.set(.tier, .int(1))

        let result = await client.apply(edit, to: "hotspot", console: console, token: "wrong")
        guard case .failure(let error) = result, case .notAuthorised(let said) = error else {
            return XCTFail("expected notAuthorised, got \(result)")
        }
        XCTAssertEqual(said, "bad or missing bearer token")
        XCTAssertTrue(error.message.contains("console_token"),
                      "the message must say where to get the token: \(error.message)")
    }

    func testAnUnknownLegIsReportedByName() async {
        let wire = Wire()
        let client = LegEditClient(transport: wire.replying(404, #"{"error": "no such leg: dongle5g"}"#))
        var edit = LegEdit()
        edit.set(.tier, .int(1))

        let result = await client.apply(edit, to: "dongle5g", console: console, token: "tok")
        guard case .failure(let error) = result, case .noSuchLeg(let leg) = error else {
            return XCTFail("expected noSuchLeg, got \(result)")
        }
        XCTAssertEqual(leg, "dongle5g")
        XCTAssertTrue(error.message.contains("dongle5g"), error.message)
    }

    /// A refusal must carry the router's reason. "It did not work" sends the
    /// reader looking in the wrong place.
    func testARefusedValueCarriesTheRoutersReason() async {
        let wire = Wire()
        let client = LegEditClient(transport: wire.replying(400, #"{"error": "unknown field(s): ['tierr']"}"#))
        var edit = LegEdit()
        edit.set(.tier, .int(1))

        let result = await client.apply(edit, to: "hotspot", console: console, token: "tok")
        guard case .failure(let error) = result, case .refused(let said) = error else {
            return XCTFail("expected refused, got \(result)")
        }
        XCTAssertEqual(said, "unknown field(s): ['tierr']")
        XCTAssertTrue(error.message.contains("applied nothing"), error.message)
    }

    func testAnUnexpectedStatusIsNotFoldedIntoAKnownOne() async {
        let wire = Wire()
        let client = LegEditClient(transport: wire.replying(502, "<html>bad gateway</html>"))
        var edit = LegEdit()
        edit.set(.tier, .int(1))

        let result = await client.apply(edit, to: "hotspot", console: console, token: "tok")
        guard case .failure(.unexpectedStatus(let code, _)) = result else {
            return XCTFail("expected unexpectedStatus, got \(result)")
        }
        XCTAssertEqual(code, 502)
    }

    /// A transport that hands back something which is not an HTTP exchange
    /// must not be read as a success. Assuming 200 here would invent one.
    func testANonHTTPReplyIsNeverTreatedAsSuccess() async {
        let transport: LegEditClient.Transport = { request in
            (Data("ok".utf8), URLResponse(url: request.url!, mimeType: nil,
                                          expectedContentLength: 2, textEncodingName: nil))
        }
        var edit = LegEdit()
        edit.set(.tier, .int(1))

        let result = await LegEditClient(transport: transport)
            .apply(edit, to: "hotspot", console: console, token: "tok")
        guard case .failure(.unreadableReply) = result else {
            return XCTFail("expected unreadableReply, got \(result)")
        }
    }

    func testAnUnreachableConsoleSaysNothingWasSent() async {
        let transport: LegEditClient.Transport = { _ in throw URLError(.cannotConnectToHost) }
        var edit = LegEdit()
        edit.set(.tier, .int(1))

        let result = await LegEditClient(transport: transport)
            .apply(edit, to: "hotspot", console: console, token: "tok")
        guard case .failure(let error) = result, case .unreachable = error else {
            return XCTFail("expected unreachable, got \(result)")
        }
        XCTAssertTrue(error.message.contains("nothing was"), error.message)
    }

    // MARK: - the receipt is the only evidence

    func testASuccessfulWriteReturnsWhatTheRouterStored() async throws {
        let wire = Wire()
        let client = LegEditClient(transport: wire.replying(200, """
        {"leg": "companion-iphone",
         "applied": {"tier": 3, "monthly_cap_gb": 15.0, "carrier": "Verizon"}}
        """))
        var edit = LegEdit()
        edit.set(.tier, .int(3))
        edit.set(.monthlyCapGB, .number(15))
        edit.set(.carrier, .text("Verizon"))

        let result = await client.apply(edit, to: "companion-iphone",
                                        console: console, token: "tok")
        let receipt = try XCTUnwrap(try? result.get())
        XCTAssertEqual(receipt.leg, "companion-iphone")
        XCTAssertEqual(receipt.value(.tier), .int(3))
        XCTAssertEqual(receipt.value(.carrier), .text("Verizon"))
        XCTAssertEqual(receipt.unconfirmed(edit), [],
                       "every field was echoed, so nothing is unconfirmed")
    }

    /// THE POINT OF THE RECEIPT. A 200 whose echo is missing a field means that
    /// field was not stored, and the UI must be able to say so rather than draw
    /// the edit as applied because the status code was green.
    func testAFieldTheRouterDidNotEchoIsReportedAsUnconfirmed() async throws {
        let wire = Wire()
        let client = LegEditClient(transport: wire.replying(200, """
        {"leg": "companion-iphone", "applied": {"tier": 3}}
        """))
        var edit = LegEdit()
        edit.set(.tier, .int(3))
        edit.set(.maxKbps, .int(500))

        let result = await client.apply(edit, to: "companion-iphone",
                                        console: console, token: "tok")
        let receipt = try XCTUnwrap(try? result.get())
        XCTAssertEqual(receipt.unconfirmed(edit), [.maxKbps])
    }

    /// A value that came back DIFFERENT is as unconfirmed as one that did not
    /// come back at all.
    func testAValueTheRouterChangedIsReportedAsUnconfirmed() throws {
        let receipt = try JSONDecoder().decode(LegEditReceipt.self, from: Data("""
        {"leg": "hotspot", "applied": {"tier": 1}}
        """.utf8))
        var edit = LegEdit()
        edit.set(.tier, .int(3))
        XCTAssertEqual(receipt.unconfirmed(edit), [.tier])
    }

    /// JSON does not distinguish 15 from 15.0 and the agent coerces to the
    /// config's own type. A false "not applied" is as damaging as a false
    /// "applied", so the comparison is numeric.
    func testAnIntegerSentAndAFloatEchoedStillCounts() throws {
        let receipt = try JSONDecoder().decode(LegEditReceipt.self, from: Data("""
        {"leg": "hotspot", "applied": {"monthly_cap_gb": 15.0}}
        """.utf8))
        var edit = LegEdit()
        edit.set(.monthlyCapGB, .int(15))
        XCTAssertEqual(receipt.unconfirmed(edit), [])
    }

    /// Clearing means the key is GONE from the stored entry. Still present
    /// means the override survived and the leg is still overridden.
    func testAClearedFieldStillPresentInTheEchoIsUnconfirmed() throws {
        let receipt = try JSONDecoder().decode(LegEditReceipt.self, from: Data("""
        {"leg": "hotspot", "applied": {"max_kbps": 500}}
        """.utf8))
        var edit = LegEdit()
        edit.clear(.maxKbps)
        XCTAssertEqual(receipt.unconfirmed(edit), [.maxKbps])

        let cleared = try JSONDecoder().decode(LegEditReceipt.self, from: Data("""
        {"leg": "hotspot", "applied": {}}
        """.utf8))
        XCTAssertEqual(cleared.unconfirmed(edit), [])
    }

    /// A 200 with a body this app cannot read leaves the outcome UNKNOWN,
    /// which is worse than a clean failure and must not be rounded to success.
    func testAnUnreadableSuccessBodyIsAFailure() async {
        let wire = Wire()
        let client = LegEditClient(transport: wire.replying(200, "not json at all"))
        var edit = LegEdit()
        edit.set(.tier, .int(1))

        let result = await client.apply(edit, to: "hotspot", console: console, token: "tok")
        guard case .failure(let error) = result, case .unreadableReply = error else {
            return XCTFail("expected unreadableReply, got \(result)")
        }
        XCTAssertTrue(error.message.contains("unknown"), error.message)
    }

    // MARK: - reading the leg back

    func testTheSnapshotSeparatesConfiguredWeightFromEffectiveWeight() async throws {
        let wire = Wire()
        let client = LegEditClient(transport: wire.replying(200, liveStatus))

        let result = await client.snapshot(of: "companion-iphone", status: console)
        let leg = try XCTUnwrap(try? result.get())
        XCTAssertEqual(leg.configWeight, 60, "the number the operator set")
        XCTAssertEqual(leg.effectiveWeight, 16, "what the policy is using now")
        XCTAssertEqual(leg.tier, 2)
        XCTAssertEqual(leg.priority, 40)
        XCTAssertEqual(leg.label, "iPhone (Verizon)")
    }

    /// The agent defaults monthly_cap_gb to 0.0 and reads that as "no budget",
    /// so every unconfigured leg publishes a zero. Rendering it as a 0 GB cap
    /// would tell the reader this leg may carry nothing, which is the opposite.
    func testACapOfZeroIsNoCapRatherThanACapOfZero() throws {
        let data = Data(liveStatus.replacingOccurrences(of: "\"monthly_cap_gb\": 50.0",
                                                        with: "\"monthly_cap_gb\": 0.0").utf8)
        let leg = try XCTUnwrap(try? LegEditSnapshot.find("companion-iphone",
                                                          inStatus: data).get())
        XCTAssertNil(leg.capGB)
        XCTAssertNil(leg.usedFraction, "there is nothing to compare usage against")
        XCTAssertNil(leg.remainingGB)
    }

    func testACeilingOfZeroIsUncapped() throws {
        let leg = try XCTUnwrap(try? LegEditSnapshot.find("companion-iphone",
                                                          inStatus: Data(liveStatus.utf8)).get())
        XCTAssertNil(leg.ceilingKbps, "max_kbps 0 means no ceiling")
    }

    /// A measured zero is a measurement. It is only "unknown" when the router
    /// did not publish the field at all.
    func testMeasuredUsageOfZeroIsNotUnknown() throws {
        let leg = try XCTUnwrap(try? LegEditSnapshot.find("companion-iphone",
                                                          inStatus: Data(liveStatus.utf8)).get())
        XCTAssertEqual(leg.usageGB, 0.0)
        XCTAssertEqual(leg.capGB, 50.0)
        XCTAssertEqual(leg.remainingGB, 50.0)
        XCTAssertEqual(leg.usedFraction, 0.0)
    }

    func testUsageIsUnknownWhenTheRouterDoesNotPublishIt() throws {
        let data = Data(liveStatus.replacingOccurrences(of: "\"usage_gb\": 0.0,",
                                                        with: "").utf8)
        let leg = try XCTUnwrap(try? LegEditSnapshot.find("companion-iphone",
                                                          inStatus: data).get())
        XCTAssertNil(leg.usageGB)
        XCTAssertNil(leg.usedFraction, "nothing measured means nothing to draw")
    }

    func testAskingForALegTheRouterDoesNotHaveNamesIt() {
        let result = LegEditSnapshot.find("dongle5g", inStatus: Data(liveStatus.utf8))
        guard case .failure(.noSuchLeg(let leg)) = result else {
            return XCTFail("expected noSuchLeg, got \(result)")
        }
        XCTAssertEqual(leg, "dongle5g")
    }

    // MARK: - the token

    /// `cat console_token` yields a trailing newline and a long-press paste
    /// carries whitespace. Both 401 as "wrong token" and send the reader back
    /// to the router to copy the same string again.
    func testAPastedTokenIsCleanedUpBeforeItIsUsed() {
        XCTAssertEqual(ConsoleWriteToken.normalise("  abc123\n"), "abc123")
        XCTAssertEqual(ConsoleWriteToken.normalise("Bearer abc123"), "abc123")
        XCTAssertNil(ConsoleWriteToken.normalise("   \n\t "))
    }

    func testTheClientNormalisesTheTokenItSends() async throws {
        let wire = Wire()
        let client = LegEditClient(transport: wire.replying(200, #"{"leg":"h","applied":{}}"#))
        var edit = LegEdit()
        edit.set(.tier, .int(1))

        _ = await client.apply(edit, to: "h", console: console, token: "abc123\n")

        let sent = try XCTUnwrap(wire.requests.first)
        XCTAssertEqual(sent.value(forHTTPHeaderField: "Authorization"), "Bearer abc123",
                       "a trailing newline would be sent as part of the token")
    }
}

// MARK: - renaming

extension LegEditTests {

    /// THE CONTROL THAT DID NOTHING. `label` was renderable, carried by the
    /// payload, and accepted by the router, while being absent from the
    /// editable set the save path filters on - so typing a new name changed
    /// nothing and reported no error. Operator asked three times.
    func testLabelIsAnEditableField() {
        XCTAssertTrue(LegEdit.Field.allCases.contains(.label))
        XCTAssertEqual(LegEdit.Field.label.rawValue, "label",
                       "the wire key must be exactly what the router accepts")
    }

    /// A rename must reach the router as `label`, not be dropped or renamed.
    func testARenameSerialisesToTheRoutersKey() throws {
        let payload = LegEdit([.label: .text("Repeated WiFi")])
        let data = try payload.body()
        let json = try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(json["label"] as? String, "Repeated WiFi",
                       "the rename did not reach the wire as `label`")
    }
}
