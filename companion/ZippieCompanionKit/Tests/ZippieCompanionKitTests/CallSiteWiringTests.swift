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

    func testRelayTunnelNeverCapturesPhoneTraffic() throws {
        let provider = try source("ZippieCompanionTunnel/PacketTunnelProvider.swift")
        let inert = provider.components(separatedBy: "static func inertNetworkSettings()").last?
            .components(separatedBy: "return settings").first ?? ""
        XCTAssertEqual(inert.components(separatedBy: "includedRoutes").count - 1, 1)
        XCTAssertTrue(inert.contains("ipv4.includedRoutes = []"))
    }

    func testClientRecoveryIsConnectedToSettingsAndVisibleInApp() throws {
        let client = try source("ZippieCompanionTunnel/ClientTunnel.swift")
        let provider = try source("ZippieCompanionTunnel/PacketTunnelProvider.swift")
        let model = try source("ZippieCompanionApp/Design/BondModel.swift")
        XCTAssertTrue(client.contains("sendKeepalives()"))
        XCTAssertTrue(client.contains("try recovery.observe(statsJSON:"))
        XCTAssertTrue(client.contains("ipv4.includedRoutes = state == .capturing ?"))
        XCTAssertTrue(client.contains("ipv4.excludedRoutes = [NEIPv4Route.default()]"))
        XCTAssertTrue(provider.contains("ClientTunnel.networkSettings(config, state: state)"))
        XCTAssertTrue(model.contains("ClientProtectionStore.isReleased"))
    }

    func testFailedClientRouteSettingsReachRecoveryWithoutClaimingProtection() throws {
        let provider = try source("ZippieCompanionTunnel/PacketTunnelProvider.swift")
        let client = try source("ZippieCompanionTunnel/ClientTunnel.swift")
        XCTAssertTrue(provider.contains("if let error {\n                    Self.log.error(\"client route settings refused:"))
        XCTAssertTrue(provider.contains("done(error)"))
        XCTAssertTrue(client.contains("if let error {\n                    Self.log.error(\"client route change failed:"))
        XCTAssertTrue(client.contains("self.appliedRouteState = desired"))
        // The applied state moves only on success, so a refused change is
        // retried by the next probe instead of being recorded as done.
        let probe = client.components(separatedBy: "private func probeHome()").last ?? ""
        let failure = try XCTUnwrap(probe.range(of: "client route change failed:"))
        let success = try XCTUnwrap(probe.range(of: "} else {\n                    self.appliedRouteState = desired"))
        XCTAssertLessThan(failure.lowerBound, success.lowerBound)
    }

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
        assertCalls(settings, "controller.status.detail",
                    "the screen words status itself, so the tested preemption text never shows")

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

    func testProjectDoesNotLinkTheExternalReportingSDK() throws {
        let project = try source("project.yml")
        assertDoesNotContain(project, "dd-sdk-ios", "no target should resolve the retired SDK")
    }

    func testAppUsesUnifiedLoggingForConnectRequestsAndStatusChanges() throws {
        let logs = try source("ZippieCompanionApp/Observability.swift")
        let controller = try source("ZippieCompanionApp/TunnelController.swift")
        assertCalls(logs, "import OSLog", "connect diagnostics belong in the unified log")
        assertCalls(controller, "Observability.tunnelConnectRequested()", "log the request before it can fail")
        assertCalls(controller, "Observability.tunnelStatus(", "log the status observed by the controller")
    }

    func testTunnelKeepsStartupAnnounceAndHeartbeatDiagnostics() throws {
        let tunnel = try source("ZippieCompanionTunnel/PacketTunnelProvider.swift")
        assertCalls(tunnel, "Self.log.log(\"relay started\")", "startup should remain visible locally")
        assertCalls(tunnel, "Self.log.log(\"announced as a leg", "a successful announce should be visible")
        assertCalls(tunnel, "Self.log.error(\"router refused the announcement", "a refusal should be visible")
        assertCalls(tunnel, "await reporter.flush()", "the app still needs the App Group heartbeat")
    }

    private func codeOnly(_ text: String, commentPrefix: String) -> String {
        text.split(separator: "\n", omittingEmptySubsequences: false)
            .filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix(commentPrefix) }
            .joined(separator: "\n")
    }

    /// #65: an observed path change must actually reach the datapath, not
    /// only a log line (#64 shipped the observation; this is the fix on top
    /// of it). And the two other guarantees the issue asked for by name:
    /// contributor mode carries none of this, and the startup refusal for an
    /// unpinnable config was not lost in the refactor that introduced it.
    func testAnObservedPathChangeActuallyRebuildsLegs() throws {
        let text = try source("ZippieCompanionTunnel/ClientTunnel.swift")
        assertCalls(text, "rebuildLegs()",
                    "observed(_:) no longer rebuilds legs - an Ethernet insertion would "
                  + "log a transition and change nothing")
        assertCalls(text, "ClientConfig.rebuildPlan(from: pinnedLinks, to: resolved.links)",
                    "rebuildLegs() no longer asks the Kit's tested diff for what to add "
                  + "or remove")
        assertCalls(text, "datapath?.removeLink(pathID)",
                    "a leg whose interface disappeared is never told to the datapath - "
                  + "its socket would sit there stale")
        assertCalls(text, "guard admission.isStartable else { throw ClientTunnelError.noLegs }",
                    "client mode no longer refuses a config with no pinnable leg at "
                  + "startup")
    }

    func testContributorModeCarriesNoClientPathRebuildMachinery() throws {
        let text = try source("ZippieCompanionTunnel/PacketTunnelProvider.swift")
        for needle in ["NWPathMonitor", "rebuildLegs", "ClientConfig.rebuildPlan"] {
            assertDoesNotContain(text, needle,
                                "the contributor path picked up client mode's path-rebuild "
                              + "machinery - these two modes must stay independent")
        }
    }

    /// #244 rule 3, and the one acceptance criterion that is a build
    /// configuration rather than a behaviour: the widget's colours must come
    /// from the GENERATED tokens, so a change to `design/tokens.json` moves the
    /// widget along with the hub and Android.
    ///
    /// This is a trap with a live history. `Tokens.generated.swift` sat in the
    /// app target since 2026-08-06 with zero call sites anywhere in the repo -
    /// the app drew from Theme.swift's hand-authored `Ink`, which had already
    /// drifted from the token source. So "the widget shares Theme.swift with
    /// the app" is not evidence of anything: both halves can drift together and
    /// pass every check.
    ///
    /// Both halves are asserted. The file has to be IN the widget target, or the
    /// `Ink.gen_*` names do not resolve and the app build is red; and the view
    /// has to actually use them, or the file is shipped and ignored.
    func testTheWidgetDrawsWithTheGeneratedTokensAndHasThemInItsTarget() throws {
        let spec = try source("project.yml")
        guard let start = spec.range(of: "\n  ZippieCompanionWidgetExtension:") else {
            return XCTFail("ZippieCompanionWidgetExtension target is gone from project.yml - "
                         + "if it moved, move this check")
        }
        // To the end of the file: the widget target is the last top-level key.
        let block = codeOnly(String(spec[start.lowerBound..<spec.endIndex]), commentPrefix: "#")
        assertCalls(block, "ZippieCompanionApp/Design/Tokens.generated.swift",
                    "the widget target cannot see the generated tokens, so a token change moves "
                  + "the hub and Android but leaves the home screen behind")

        let view = try source("ZippieCompanionWidget/RelayWidgetView.swift")
        assertCalls(view, "Ink.gen_live",
                    "the widget's accent is the whole product signal; if it is not the generated "
                  + "one, the surface people believe without opening anything is the one "
                  + "surface a token change will miss")
        assertCalls(view, "Ink.gen_primary", "widget text is not taking the generated token")
        assertCalls(view, "Ink.gen_ground", "the widget background is not taking the generated token")
        // Hand-authored siblings would compile - they are in the same extension
        // - so this is the half the compiler cannot catch.
        assertDoesNotContain(codeOnly(view, commentPrefix: "//"), "Ink.live",
                             "the widget is back on Theme.swift's hand-authored accent, which has "
                           + "drifted from design/tokens.json")
        assertDoesNotContain(codeOnly(view, commentPrefix: "//"), "Ink.primary",
                             "the widget is back on Theme.swift's hand-authored text colour")
    }

    /// A widget that cannot see the bond shows the this-phone row forever, and
    /// the failure is invisible: the row is still correct, just less of it. So
    /// both ends of the handoff are pinned - the app must WRITE the record, and
    /// the provider must READ it.
    ///
    /// Without the writer this would look exactly like a working widget, which
    /// is the same shape as #48's `ClientConfig`: 34 passing tests and a value
    /// with no producer.
    func testTheAppPublishesTheBondSnapshotTheWidgetReads() throws {
        let model = try source("ZippieCompanionApp/Design/BondModel.swift")
        assertCalls(model, "WidgetBondStore.write(",
                    "nothing writes the bond snapshot, so the medium size can only ever show the "
                  + "this-phone row and the widget looks fine while being half-blind")
        assertCalls(model, "WidgetBondStore.clear(",
                    "a console that stops answering leaves the last good bond on disk, and the "
                  + "widget's own staleness gate then ages it out slowly instead of saying at "
                  + "once that the bond is unobservable")

        let provider = try source("ZippieCompanionWidget/RelayWidgetProvider.swift")
        assertCalls(provider, "WidgetBondStore.read(from: defaults)",
                    "the provider never reads what the app writes, so the two halves of the "
                  + "handoff can drift apart with nothing failing")
        assertCalls(provider, "WidgetTimeline.build(",
                    "the provider is assembling content somewhere other than the tested function")
    }

}

