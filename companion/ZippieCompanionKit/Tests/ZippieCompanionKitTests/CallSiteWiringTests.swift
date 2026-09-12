import XCTest
@testable import ZippieCompanionKit

/// Does anything actually CALL the tested logic.
///
/// WHY A TEST READS SOURCE FILES, WHICH IS NOT NORMAL AND IS NOT PRETENDING TO
/// BE. `ZippieCompanionKit` is the only target `swift test` can compile: the app
/// and the extension need Xcode, the iOS SDK and a gomobile framework, and
/// `app.companion-ios.ci.yml` runs `xcodebuild build` on them - never
/// `xcodebuild test`, because the generated project has no test target. So no
/// executable test in this repository can call `TunnelController.startTunnel`
/// or `PacketTunnelProvider.startTunnel`.
///
/// That gap is not theoretical, it is the entire subject of quadseven/zippie#48.
/// `ClientConfig` shipped with 34 passing tests and had never run once, because
/// the extension READ `providerConfiguration["client"]` and nothing in the tree
/// WROTE it. Every one of those tests would have kept passing forever. The
/// compiler catches a call site that no longer type-checks; nothing catches a
/// call site that quietly stops calling.
///
/// WHAT THIS CAN AND CANNOT PROVE. It proves the call sites still route through
/// the Kit functions that `TunnelProfileTests` exercises, and it fails the
/// moment somebody deletes the call or reintroduces a hand-assembled dictionary
/// beside it. It does NOT prove the app behaves correctly at runtime - only a
/// device does that, and #48 says so. It is a tripwire on one specific
/// regression, priced accordingly.
///
/// Written 2026-08-10 for #48.
final class CallSiteWiringTests: XCTestCase {

    /// `companion/`, found from this file rather than from the working
    /// directory - `swift test` is run from the package directory in CI and
    /// from anywhere at all locally.
    private static let companion: URL = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()   // ZippieCompanionKitTests
        .deletingLastPathComponent()   // Tests
        .deletingLastPathComponent()   // ZippieCompanionKit
        .deletingLastPathComponent()   // companion

    private func source(_ path: String) throws -> String {
        let url = Self.companion.appendingPathComponent(path)
        guard let text = try? String(contentsOf: url, encoding: .utf8) else {
            // Not a skip. A file that moved has to be followed here, or the
            // tripwire silently stops watching the thing it was written for.
            XCTFail("cannot read \(path) - if it moved, move this check with it")
            throw XCTSkip("unreadable")
        }
        return text
    }

    private func assertCalls(_ text: String, _ needle: String, _ why: String,
                             file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertTrue(text.contains(needle), "nothing calls \(needle): \(why)",
                      file: file, line: line)
    }

    private func assertDoesNotContain(_ text: String, _ needle: String, _ why: String,
                                      file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertFalse(text.contains(needle), "\(needle) is back: \(why)",
                       file: file, line: line)
    }

    /// The status screen must count MEMBERSHIP, not the drawing state.
    ///
    /// `LegState` has one slot and spends it on how the row is drawn, so a leg
    /// that is degraded AND carrying is drawn `.degraded`. Counting membership
    /// from that slot made the screen report "Nothing carrying" and "0 of 3
    /// carrying" directly above a row reading "carrying, degraded" with 402 MB
    /// sent - while `BondStatus.carryingCount`, which the telemetry uses, had
    /// the right answer all along.
    ///
    /// Source-text assertions because the app target has no test target (#48).
    /// Crude, and the only tripwire available for this file.
    func testTheStatusScreenCountsCarryingFromTheRouterNotTheDrawingState() throws {
        for path in ["ZippieCompanionApp/Design/BondLegs.swift",
                     "ZippieCompanionApp/Design/BondModel.swift"] {
            let text = try source(path)
            assertDoesNotContain(
                text, "$0.state == .carrying",
                "\(path) is counting membership from LegState again, which "
              + "cannot see a leg that is degraded AND carrying")
        }
        let legs = try source("ZippieCompanionApp/Design/BondLegs.swift")
        assertCalls(legs, "isCarrying: path.isCarrying",
                    "the row is not taking carrying from the router, so the "
                  + "headline and the row can disagree again")
    }

    /// The Kit's own answer, which the screen must not contradict.
    ///
    /// Taken from a real reading: 12% loss, 34 ms, 402 MB sent, 293 MB back.
    func testADegradedLegThatCarriesIsStillCounted() throws {
        let data = try JSONSerialization.data(withJSONObject: [
            "name": "iphone", "state": "degraded", "effective_weight": 100,
            "in_bond": true, "loss_pct": 12.0, "rtt_ms": 34.0,
            "interface": "br-lan", "tier": 1,
        ])
        let leg = try JSONDecoder().decode(BondStatus.Path.self, from: data)

        XCTAssertTrue(leg.isCarrying,
                      "12% loss is a health verdict, not a membership one")
        XCTAssertEqual(leg.stateWord, "carrying, degraded")

        let bond = try JSONDecoder().decode(
            BondStatus.self,
            from: try JSONSerialization.data(withJSONObject: [
                "paths": [["name": "iphone", "state": "degraded",
                           "effective_weight": 100, "in_bond": true,
                           "interface": "br-lan", "tier": 1]],
            ]))
        XCTAssertEqual(bond.carryingCount, 1,
                       "the count the telemetry uses must include it")
    }

    /// The producer. `TunnelController` is the only thing in the app that
    /// installs a VPN profile, and until #48 it assigned the relay's flat
    /// dictionary directly - which is why the client key had no writer.
    func testTheAppInstallsItsProfileThroughTheKit() throws {
        let text = try source("ZippieCompanionApp/TunnelController.swift")
        assertCalls(text, "TunnelPlan.decide(",
                    "the app is choosing its mode somewhere other than the tested decision")
        assertCalls(text, "TunnelProfile(",
                    "the app is building a profile the Kit tests never see")
        assertDoesNotContain(text, ".providerConfiguration =",
                             "the app is assembling a provider dictionary by hand again, "
                           + "which is exactly how the client key came to have no producer")
    }

    /// A stop from the app must disarm on-demand and SAVE before it stops
    /// (#55). `stopTunnel` used to be a bare `stopVPNTunnel()`, and on the
    /// router's wifi the Connect rule answered it by starting the tunnel again
    /// within the second - "Stop relaying" was a restart button. The order is
    /// the point: a save AFTER the stop leaves the same window open.
    func testTheAppDisarmsOnDemandBeforeItStopsTheTunnel() throws {
        let text = try source("ZippieCompanionApp/TunnelController.swift")
        guard let stop = text.range(of: "func stopTunnel()") else {
            return XCTFail("TunnelController.stopTunnel is gone - if it moved, move this check")
        }
        // The body up to the next MARK, which is where `stopTunnel` ends.
        let bodyEnd = text.range(of: "// MARK: - supervision", range: stop.upperBound..<text.endIndex)?.lowerBound
            ?? text.endIndex
        let body = String(text[stop.lowerBound..<bodyEnd])
        guard let disarm = body.range(of: "TunnelProfile.disarmOnDemand(on:") else {
            return XCTFail("stopTunnel no longer disarms on-demand through the Kit: on the "
                         + "router's wifi the tunnel will be started again the moment it stops")
        }
        guard let save = body.range(of: "saveToPreferences()") else {
            return XCTFail("stopTunnel disarms on-demand but never saves it, and an "
                         + "in-memory flag is nothing the system acts on")
        }
        guard let stopCall = body.range(of: "stopVPNTunnel()") else {
            return XCTFail("stopTunnel no longer stops the tunnel at all")
        }
        XCTAssertTrue(disarm.lowerBound < save.lowerBound,
                      "the disarm has to happen before the save, or the save writes the armed rule")
        XCTAssertTrue(save.lowerBound < stopCall.lowerBound,
                      "the save has to land before the stop, or the rule is still armed when "
                    + "the tunnel goes down and the system restarts it")
    }

    /// The consumer. The extension used to read the raw key itself and, when it
    /// would not parse, log "falling through to contributor mode" - a phone in a
    /// hotel spending metered data on a bond that cannot hear it.
    func testTheExtensionChoosesItsModeThroughTheKit() throws {
        let text = try source("ZippieCompanionTunnel/PacketTunnelProvider.swift")
        assertCalls(text, "TunnelPlan.installed(",
                    "the extension is deciding its mode ad hoc again")
        assertDoesNotContain(text, "providerConfiguration?[",
                             "the extension is subscripting the provider dictionary directly, "
                           + "so the key it reads is no longer the key the app writes")
        assertDoesNotContain(text, "RelayConfiguration(providerConfiguration:",
                             "the fallback order between the profile and the app group is "
                           + "back in the extension, where no test can reach it")
    }

    /// A leg pinned to a stale device name is refused by the Go datapath with
    /// no error the app can show, and a bond with no legs at all comes up
    /// green carrying nothing.
    func testClientLegsArePinnedToLiveInterfacesBeforeTheyAreAttached() throws {
        let text = try source("ZippieCompanionTunnel/ClientTunnel.swift")
        assertCalls(text, "repinned(using:",
                    "client legs are attached on the device names typed at pairing time")
        assertCalls(text, "LegAdmission.admit(",
                    "a client with no live leg can still start and report itself as a bond")
    }

    /// The DNS controller and profile model predated their UI. With no screen
    /// constructing the controller, every unit test stayed green while nobody
    /// could enter a profile or enable it (#25).
    func testNextDNSSettingsAreReachableAndDriveTheSystemController() throws {
        let status = try source("ZippieCompanionApp/Design/BondScreen.swift")
        assertCalls(status, "NextDNSSettingsScreen()",
                    "the NextDNS settings exist but have no user-reachable entry point")

        let settings = try source("ZippieCompanionApp/Design/NextDNSSettingsScreen.swift")
        assertCalls(settings, "DNSSettingsController(",
                    "the settings screen never reaches the iOS DNS settings API")
        assertCalls(settings, "if await controller.apply(profile)",
                    "the editor can persist a profile the system refused")
        assertCalls(settings, "Settings.nextDNSProfileID =",
                    "the profile ID is not persisted for the next launch")
        assertCalls(settings, "Settings.nextDNSDeviceName =",
                    "the per-person device name is not persisted")
        assertCalls(settings, "controller.apply(profile)",
                    "saving the fields never enables the configured resolver")
        assertCalls(settings, ".onChange(of: scenePhase)",
                    "status stays stale after the user enables DNS in iOS Settings")

        let entitlements = try source("ZippieCompanionApp/ZippieCompanion.entitlements")
        assertCalls(entitlements, "<string>dns-settings</string>",
                    "NEDNSSettingsManager compiles but the signed app cannot use it")
    }

    /// SUPERVISION IS THE MECHANISM MOST LIKELY TO GO INERT AND LEAST LIKELY TO
    /// BE NOTICED. Its whole subject is a relay that looks fine, so a
    /// supervisor that stopped being called would present exactly as a relay
    /// that was never wedged - and the fault it exists for was live for an
    /// unknown number of days before anybody read the router's `rtt_ms: null`
    /// and understood what it meant.
    ///
    /// `RelaySupervisionTests` proves the decision. These prove somebody asks
    /// it, and that the two answers are carried out rather than computed and
    /// dropped.
    func testBothSupervisorsActuallyAskTheKitAndActOnTheAnswer() throws {
        let ext = try source("ZippieCompanionTunnel/PacketTunnelProvider.swift")
        assertCalls(ext, "RelaySupervision.evaluate(",
                    "the extension no longer watches its own datapath, so a relay that "
                  + "holds its socket and never services it is invisible again")
        assertCalls(ext, "superviseSelf(since:",
                    "supervision is defined in the extension and never reaches the heartbeat, "
                  + "which is the only thing that runs with nobody holding the phone")
        assertCalls(ext, "cancelTunnelWithError(",
                    "the extension computes a remedy it never carries out - the exact shape "
                  + "of a mechanism that is unit-tested and has never run")
        assertCalls(ext, "RelaySupervisionStore.recordRemedy(",
                    "nothing records the attempt, so the cooldown is empty and a wedge that "
                  + "reproduces on restart cancels the tunnel every 75 seconds forever")

        let app = try source("ZippieCompanionApp/TunnelController.swift")
        assertCalls(app, "RelaySupervision.evaluate(",
                    "the app decides for itself whether the relay is wedged, which is how "
                  + "#44 shipped a sentence no test could reach")
        assertCalls(app, "connection.connectedDate",
                    "the app is judging silence without an anchor, or inventing one - a "
                  + "tunnel started by an on-demand rule has no other start time")
        assertCalls(app, "await waitForDisconnect(",
                    "the restart starts the tunnel before the stop has landed, so the call "
                  + "succeeds and nothing restarts (trap 7)")

        // The reason has to reach a human. A supervisor that holds in silence
        // is the failure this whole type was written to stop repeating.
        let screen = try source("ZippieCompanionApp/Design/RelayScreen.swift")
        assertCalls(screen, "await tunnel.supervise()",
                    "nothing on the screen ever runs a supervision pass")
        assertCalls(screen, "pass.remedy.why",
                    "supervision decides in silence - the operator sees a relay reading "
                  + "Ready and no explanation of why nothing is being done")
    }

    /// Datadog RESERVES `status` for the log level. An attribute by that name
    /// is overwritten on ingest, so the tunnel's real state never arrives.
    ///
    /// This cost a live debugging session on 2026-09-11: a phone was flipping
    /// through five tunnel states in under two seconds in a moving car, every
    /// transition reached Datadog correctly timestamped, and every single one
    /// read `status: info`. The instrumentation existed, looked healthy on a
    /// dashboard, and could not answer the one question it was written for.
    /// A rename back would be silent and total, so it is pinned here.
    func testTheTunnelStatusIsNotLoggedUnderDatadogsReservedKey() throws {
        let text = try source("ZippieCompanionApp/Observability.swift")
        XCTAssertTrue(text.contains("\"tunnel_status\":"),
                      "the tunnel's state must be logged under a key Datadog "
                    + "does not reserve, or it is destroyed on ingest")
        XCTAssertFalse(text.contains("\"status\": tunnelStatusName"),
                       "`status` is Datadog's log level - an attribute of that "
                    + "name is silently overwritten, which is worse than no "
                    + "instrumentation because it looks like coverage")
    }


    /// The cellular Disconnect rule cannot be asserted by running code here:
    /// NEOnDemandRuleInterfaceType.cellular is iOS-ONLY and this package's own
    /// tests compile for macOS, where the case does not exist. (.ethernet is
    /// the mirror image - macOS-only - and writing it here passed every local
    /// check before failing the iOS app build.) So its presence is read from
    /// the source instead, or dropping it would silently leave the tunnel
    /// running on cellular all day after the phone leaves the router.
    func testTheCellularDisconnectRuleStillExistsForIOS() throws {
        let text = try source("ZippieCompanionKit/Sources/ZippieCompanionKit/TunnelProfile.swift")
        XCTAssertTrue(text.contains("#if os(iOS)"),
                      "the iOS-only rule must stay behind a platform guard")
        // Matched on the ASSIGNMENT, not on a variable name - renaming the
        // local should not read as deleting the rule.
        XCTAssertTrue(text.contains("interfaceTypeMatch = .cellular"),
                      "a phone that walks away from the router onto cellular must "
                    + "still be disconnected")
        // CODE ONLY. The first version of this check failed on the comment
        // that warns against the very thing it forbids - a text tripwire that
        // cannot tell an instruction from a prohibition is worse than none.
        let code = text.split(separator: "\n")
            .filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix("//") }
            .joined(separator: "\n")
        XCTAssertFalse(code.contains("interfaceTypeMatch = .ethernet"),
                       "'ethernet' is unavailable in iOS and fails the app build")
    }

    /// #76: a connect attempt must still produce a span when `.connecting`
    /// was never observed for it - the fast-flap case measured 2026-09-11,
    /// where two taps of "Start relaying" on a phone whose tunnel was being
    /// torn down by an on-demand rule produced 6 transitions in 1.72s and 5
    /// in 1.79s, and ZERO `zippie.tunnel.connect` spans that hour.
    ///
    /// CODE ONLY for the `guard`/`duration_measured` checks: the fix is
    /// explained in a doc comment that quotes the very guard it replaced, and
    /// a tripwire that cannot tell an explanation from the thing being
    /// checked for trips on its own comment.
    func testAFastConnectAttemptWithNoObservedConnectingStillProducesASpan() throws {
        let text = try source("ZippieCompanionApp/Observability.swift")
        let code = text.split(separator: "\n")
            .filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix("//") }
            .joined(separator: "\n")

        assertDoesNotContain(
            code, "guard let startedAt else { return }",
            "a terminal transition with no recorded `.connecting` silently "
          + "drops the span again - the exact fast-flap case measured "
          + "2026-09-11 produced zero spans in an hour")
        assertCalls(
            code, "\"duration_measured\"",
            "a span with no measured start must say so explicitly, or a "
          + "reader cannot tell it apart from one with a genuine duration")
        assertCalls(
            code, "static func tunnelConnectRequested",
            "nothing marks the moment a connect was REQUESTED, so a fast "
          + "failure that never reaches `.connecting` cannot be told apart "
          + "from an ordinary stop - both land on `.disconnected` with no "
          + "recorded start")

        let controller = try source("ZippieCompanionApp/TunnelController.swift")
        assertCalls(
            controller, "Observability.tunnelStatus(",
            "TunnelController never forwards an OBSERVED status change to "
          + "Observability - without this, traceTunnelTransition only ever "
          + "runs for the one stale snapshot taken right after startTunnel() "
          + "returns, never for a transition the system actually delivered")
        assertCalls(
            controller, "Observability.tunnelConnectRequested()",
            "startTunnel never marks the moment a connect was requested, so "
          + "the fast-flap fallback span above can never fire")
    }

    /// Strips comment lines before a containment check, so a tripwire that
    /// asserts something is ABSENT cannot trip on the very comment that
    /// explains why it must stay absent. `prefix` is the file's own comment
    /// marker - `//` for Swift, `#` for the YAML in `project.yml`. Mirrors
    /// the code-only filter `testTheCellularDisconnectRuleStillExistsForIOS`
    /// already uses inline, generalised so the Datadog-wiring checks below
    /// can reuse it for both a Swift file and a YAML one.
    private func codeOnly(_ text: String, commentPrefix: String) -> String {
        text.split(separator: "\n", omittingEmptySubsequences: false)
            .filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix(commentPrefix) }
            .joined(separator: "\n")
    }

    /// THE BACKGROUND RELAY IS THE ONE THAT MATTERS, and until #73 it logged
    /// exclusively through `os.Logger` - the one call site that ever reached
    /// Datadog was the foreground toggle at `RelayScreen.swift:447`, which an
    /// operator rarely opens. `PacketTunnelProvider.swift` cannot be compiled
    /// by `swift test` at all (it is an app-extension Xcode target, not part
    /// of this package - see the type comment above), so this is read from
    /// source, the same way `testBothSupervisorsActuallyAskTheKitAndActOnTheAnswer`
    /// already proves supervision is wired rather than merely defined.
    func testTheExtensionShipsRelayStatsToDatadogOnItsHeartbeat() throws {
        let text = try source("ZippieCompanionTunnel/PacketTunnelProvider.swift")
        assertCalls(text, "TunnelObservability.start(legName:",
                    "the extension never brings the SDK up, so nothing it logs can reach Datadog")
        assertCalls(text, "reporter.nextHeartbeatTick()",
                    "the report cadence has no counter to ask, so it cannot rate-limit itself")
        assertCalls(text, "RelayTelemetry.shouldReport(tick:",
                    "the extension ships on every 2s heartbeat pass rather than on the coarser "
                  + "schedule #73 requires - that is 15x the request volume for no benefit")
        assertCalls(text, "TunnelObservability.report(",
                    "the heartbeat computes whether to report and never actually ships anything")
    }

    /// `DatadogCore` + `DatadogLogs` ONLY, never `DatadogRUM` or
    /// `DatadogTrace` - see the target comment in `project.yml` for why. This
    /// cannot be proven by `swift build`, which does not resolve Xcode
    /// project dependencies at all; only reading the generator spec can.
    func testTheTunnelExtensionLinksOnlyDatadogLogsNeverRUMOrTrace() throws {
        let text = try source("project.yml")
        guard let start = text.range(of: "\n  ZippieCompanionTunnel:") else {
            return XCTFail("ZippieCompanionTunnel target is gone from project.yml - "
                         + "if it moved, move this check with it")
        }
        // The next top-level target key in the file today. A literal
        // boundary rather than an indentation parser, matching the
        // `// MARK: -` boundary trick above - crude, and the only tripwire
        // available for a YAML file with no compiler of its own.
        let end = text.range(of: "\n  ZippieCompanionWidgetExtension:",
                             range: start.upperBound..<text.endIndex)?.lowerBound
            ?? text.endIndex
        let block = codeOnly(String(text[start.lowerBound..<end]), commentPrefix: "#")

        XCTAssertTrue(block.contains("product: DatadogCore"),
                      "the tunnel target no longer links DatadogCore - Datadog.initialize has "
                    + "nothing to call")
        XCTAssertTrue(block.contains("product: DatadogLogs"),
                      "the tunnel target no longer links DatadogLogs - Logs.enable has nothing "
                    + "to call")
        XCTAssertFalse(block.contains("product: DatadogRUM"),
                       "DatadogRUM is back in the extension target - it tracks view hierarchies "
                     + "and swizzles URLSession in a provider with neither, for memory this "
                     + "process cannot afford")
        XCTAssertFalse(block.contains("product: DatadogTrace"),
                       "DatadogTrace is back in the extension target - a second feature "
                     + "registry and its own storage, for memory this process cannot afford")
    }

    /// The extension's own Datadog wrapper must import the same two products
    /// `project.yml` links for it, and neither of the heavier ones.
    func testTheExtensionsDatadogWrapperImportsOnlyCoreAndLogs() throws {
        let text = try source("ZippieCompanionTunnel/TunnelObservability.swift")
        assertCalls(text, "import DatadogCore", "the wrapper cannot initialise the SDK without this")
        assertCalls(text, "import DatadogLogs", "the wrapper cannot log without this")
        let code = codeOnly(text, commentPrefix: "//")
        assertDoesNotContain(code, "import DatadogRUM",
                             "RUM is back in the process this memory budget cannot afford it in")
        assertDoesNotContain(code, "import DatadogTrace",
                             "Trace is back in the process this memory budget cannot afford it in")
    }

    /// Guarded so a tunnel that stops and restarts inside ONE process
    /// lifetime - which happens, see `stopTunnel`/`startTunnel` - never
    /// calls `Datadog.initialize` a second time. An unguarded `start()`
    /// looks identical to a guarded one until the second call, which no
    /// device test in this repository can exercise.
    func testTheExtensionsDatadogInitIsGuardedAgainstRunningTwice() throws {
        let text = try source("ZippieCompanionTunnel/TunnelObservability.swift")
        guard let fn = text.range(of: "static func start(legName:") else {
            return XCTFail("TunnelObservability.start() is gone - if it moved, move this check")
        }
        let bodyEnd = text.range(of: "\n    static func report(",
                                 range: fn.upperBound..<text.endIndex)?.lowerBound
            ?? text.endIndex
        let body = String(text[fn.lowerBound..<bodyEnd])
        assertCalls(body, "guard !started else { return }",
                    "start() has no guard, so a second call re-initialises the whole SDK")
        assertCalls(body, "started = true",
                    "the guard flag is never set, so it can never actually guard anything")
    }

    /// The BACKGROUND relay must be queryable under the SAME attribute shape
    /// the FOREGROUND toggle already ships, or an operator has to learn two
    /// log shapes for one fact. `RelayTelemetry.attributes` is the Kit's
    /// half of that promise (proven by `RelayTelemetryTests`); this proves
    /// `Observability.relayStats` - which this package cannot import or
    /// compile - still names the identical keys, so nobody can drift one
    /// shape without a test noticing.
    func testTheForegroundAndBackgroundRelayLogsShareTheSameAttributeKeys() throws {
        let appSide = try source("ZippieCompanionApp/Observability.swift")
        let kitSide = try source("ZippieCompanionKit/Sources/ZippieCompanionKit/RelayTelemetry.swift")
        for key in ["cellular_ready", "up.datagrams", "up.bytes",
                    "down.datagrams", "down.bytes", "errors", "last_error",
                    "router.ever_inbound", "router.last_inbound_age_s"] {
            let quoted = "\"\(key)\""
            XCTAssertTrue(appSide.contains(quoted),
                          "the foreground shape dropped \(key) - RelayTelemetry would still "
                        + "ship it, and the two paths would disagree")
            XCTAssertTrue(kitSide.contains(quoted),
                          "the background shape dropped \(key) - the foreground toggle would "
                        + "still ship it, and the two paths would disagree")
        }
    }

    /// THE KILL SWITCH. Whether `DatadogCore` actually fits inside this
    /// process's jetsam ceiling is not knowable without a device; if it
    /// does not, iOS kills the extension silently, the on-demand rule
    /// restarts it, and the same SDK pushes it over the same ceiling again
    /// - a loop whose only other exit is a new TestFlight build. The escape
    /// hatch only works if the check runs BEFORE `Datadog.initialize`, so
    /// this proves both that `start()` asks and that it asks first.
    func testTheExtensionConsultsTheKillSwitchBeforeInitialisingDatadog() throws {
        let text = try source("ZippieCompanionTunnel/TunnelObservability.swift")
        guard let fn = text.range(of: "static func start(legName:") else {
            return XCTFail("TunnelObservability.start() is gone - if it moved, move this check")
        }
        let bodyEnd = text.range(of: "\n    static func report(",
                                 range: fn.upperBound..<text.endIndex)?.lowerBound
            ?? text.endIndex
        let body = String(text[fn.lowerBound..<bodyEnd])

        guard let check = body.range(of: "RelayTelemetry.isTelemetryEnabled(in:") else {
            return XCTFail("start() never asks the kill switch - an operator has no way to "
                         + "turn this off without shipping a new build")
        }
        guard let initCall = body.range(of: "Datadog.initialize(") else {
            return XCTFail("Datadog.initialize is gone from start() - if it moved, move this "
                         + "check")
        }
        XCTAssertTrue(check.lowerBound < initCall.lowerBound,
                      "the kill switch is checked AFTER Datadog.initialize - by then the SDK "
                    + "is already up and the switch cannot prevent the very thing it exists "
                    + "to prevent")
        assertCalls(body, "RelayConfiguration.sharedDefaults",
                    "the kill switch reads something other than the shared App Group, which "
                    + "is the one channel an operator can write without a new profile install "
                    + "or a rebuild")

        // The switch must also gate REPORTING, not just initialisation - a
        // process that skipped `Datadog.initialize` must not go on to call
        // `Logger.create` (via `log`) from `report` either, or disabling
        // the switch would still wake Datadog's Logger machinery.
        assertCalls(text, "guard enabled else { return }",
                    "report() no longer checks whether the switch was engaged at startup, so "
                  + "a disabled process would still touch Datadog's Logger machinery")
    }

    /// Grug flagged (#73 PR review) that `enabled = true` was set BEFORE
    /// `Datadog.initialize`/`Logs.enable` returned - unreachable given
    /// `report()`'s only caller is a `Task` created after `start()` has
    /// already returned synchronously in `startContributor`, but a fix that
    /// costs one line's position is cheaper than an argument about whether
    /// a future call site could ever make it reachable. This pins the
    /// corrected order so it cannot silently drift back.
    func testTheEnabledFlagIsRaisedOnlyAfterTheSDKIsFullyUp() throws {
        let text = try source("ZippieCompanionTunnel/TunnelObservability.swift")
        guard let fn = text.range(of: "static func start(legName:") else {
            return XCTFail("TunnelObservability.start(legName:) is gone - if it moved, move this check")
        }
        let bodyEnd = text.range(of: "\n    static func report(",
                                 range: fn.upperBound..<text.endIndex)?.lowerBound
            ?? text.endIndex
        let body = String(text[fn.lowerBound..<bodyEnd])

        guard let logsEnable = body.range(of: "Logs.enable()") else {
            return XCTFail("Logs.enable() is gone from start() - if it moved, move this check")
        }
        guard let enabledFlag = body.range(of: "enabled = true") else {
            return XCTFail("enabled = true is gone from start() - report() would never fire")
        }
        XCTAssertTrue(logsEnable.upperBound <= enabledFlag.lowerBound,
                      "enabled is raised before Logs.enable() returns - a concurrent report() "
                    + "could observe enabled==true and touch the Logger before the SDK has "
                    + "finished coming up")
    }

    /// #74's platform/device tags, mirrored onto the extension's OWN stream -
    /// without this, a background-relay log line is exactly the ambiguous
    /// "which phone, which process" reading #74 was filed to end, just one
    /// process over from the one #74 actually fixed.
    func testTheExtensionsStreamCarriesThePlatformAndDeviceTagsToo() throws {
        let text = try source("ZippieCompanionTunnel/TunnelObservability.swift")
        assertCalls(text, "Logs.addAttribute(forKey: \"platform\", value: \"ios\")",
                    "the extension's logs carry no platform tag - indistinguishable from "
                  + "Android's relay heartbeat again, the exact 2026-09-11 misdiagnosis")
        assertCalls(text, "Logs.addAttribute(forKey: \"device\", value: legName)",
                    "the extension's logs carry no per-device tag - one phone's stream cannot "
                  + "be told apart from another's of the same platform")
        assertCalls(text, "Logs.addAttribute(forKey: \"process\", value: \"tunnel\")",
                    "nothing distinguishes this process's stream from the app's own on the "
                  + "same device now that both carry platform/device")
    }

    /// #75's own TODO: `BuildInfo.commitLabel` existed, was shown on the Bond
    /// screen, and was never wired into Datadog - "are we running the fix?"
    /// was answerable by eye on the phone and not by a query. Checks all
    /// three Datadog surfaces the app instruments (Logs, RUM, Trace), the
    /// same three `platform`/`device` already reach - a build tag on only
    /// one of them would say "traceable" for a log line and not for the
    /// span or RUM session sitting right next to it.
    func testTheAppsBuildCommitReachesAllThreeDatadogSurfaces() throws {
        let text = try source("ZippieCompanionApp/Observability.swift")
        assertCalls(text, "Logs.addAttribute(forKey: \"build_commit\", value: BuildInfo.commitLabel)",
                    "logs carry no build_commit tag - \"are we running the fix\" is answerable "
                  + "by eye on the phone and not by a Datadog query")
        assertCalls(text, "\"build_commit\": BuildInfo.commitLabel",
                    "RUM sessions carry no build_commit attribute")
        assertCalls(text, "merged[\"build_commit\"] = BuildInfo.commitLabel",
                    "APM spans carry no build_commit tag - the one surface with per-request "
                  + "detail would be the one surface that cannot be traced to a commit")
    }

    /// The extension's own stream (#75, mirroring #74's platform/device
    /// wiring for the same reason): the background relay is the process that
    /// actually carries traffic, and it is a SEPARATE bundle with its own
    /// Info.plist stamped by the same build phase - nothing here can import
    /// `BuildInfo` from the app target, so this checks the duplicated read
    /// directly rather than a shared symbol.
    func testTheExtensionsStreamCarriesTheBuildCommitTagToo() throws {
        let text = try source("ZippieCompanionTunnel/TunnelObservability.swift")
        assertCalls(text, "forKey: \"build_commit\"",
                    "the extension's logs carry no build_commit tag - the background relay "
                  + "cannot be traced to a commit even though the app's own stream now can")
        assertCalls(text, "infoDictionary?[\"ZippieGitCommit\"]",
                    "the extension reads something other than the Info.plist key "
                  + "embed-build-info.sh actually stamps")
    }

}
