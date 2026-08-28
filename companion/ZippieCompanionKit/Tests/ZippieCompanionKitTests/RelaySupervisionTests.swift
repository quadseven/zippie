import XCTest
@testable import ZippieCompanionKit

/// What supervision is allowed to conclude, and what it is allowed to do.
///
/// THE READING THESE ARE WRITTEN FROM. On 2026-08-22 a companion leg stood in
/// the router's list as `state: degraded, loss_pct: 65, rtt_ms: null,
/// in_bond: true`, on wifi at -35 dBm and 576 Mbit/s. `rtt_ms: null` says the
/// leg had never completed a single round trip, which no amount of link loss
/// produces - a lossy link still returns SOME probes. The relay held its socket
/// and never serviced it, and the phone's own screen read "Ready".
///
/// Two rules these pin, in tension with each other on purpose:
///
///   1. A relay that is announced, in the bond, and hearing nothing must
///      eventually be called what it is.
///   2. A relay that is merely idle, briefly stalled, paused on the data cap,
///      or thirty seconds old must NEVER be restarted, because the cost of
///      being wrong is the working leg itself.
final class RelaySupervisionTests: XCTestCase {

    private let now = Date(timeIntervalSince1970: 2_000_000)

    /// A report as the extension would have left it.
    ///
    /// THE DEFAULT IS A WORKING RELAY - cellular up, heartbeat fresh, router
    /// heard a second ago - so that every test which is not ABOUT the datapath
    /// starts from health. Written the other way first, with no inbound
    /// timestamp, and it made two tests that meant "this is fine" assert on a
    /// relay that had been deaf for ten minutes.
    ///
    /// - Parameters:
    ///   - age: how long ago the heartbeat was written.
    ///   - heardAgo: when the router last arrived; nil means it never has.
    private func report(age: TimeInterval = 0,
                        heardAgo: TimeInterval? = 1,
                        _ mutate: (inout CellularRelay.Stats) -> Void = { _ in }) -> RelayStatus {
        var stats = CellularRelay.Stats()
        stats.cellularReady = true
        if let heardAgo { stats.lastRouterInboundAt = now.addingTimeInterval(-heardAgo) }
        mutate(&stats)
        return RelayStatus(stats: stats, updatedAt: now.addingTimeInterval(-age))
    }

    private func running(_ seconds: TimeInterval) -> Date {
        now.addingTimeInterval(-seconds)
    }

    // MARK: - the incident

    /// THE BUG. Ten minutes of listening, a heartbeat written two seconds ago,
    /// and nothing has ever arrived. The process is plainly alive; the datapath
    /// is plainly not being serviced.
    func testARelayThatHasListenedForTenMinutesAndHeardNothingIsAFault() {
        let v = RelaySupervision.evaluate(run: .running,
                                          report: report(age: 2, heardAgo: nil),
                                          runningSince: running(600),
                                          now: now)

        XCTAssertEqual(v, .nothingArriving(silentFor: 600, everArrived: false))
        XCTAssertTrue(v.isFault)
        XCTAssertTrue(v.summary.contains("never once arrived"), v.summary)
    }

    /// The same silence, on a relay the router HAD been reaching, has to read
    /// differently - "never dialled" and "dialled and stopped" are different
    /// faults with different fixes, which is the distinction `RelayVerdict`
    /// splits `.listening` from `.routerQuiet` for.
    func testNeverArrivedAndStoppedArrivingReadDifferently() {
        let never = RelaySupervision.evaluate(
            run: .running, report: report(age: 2, heardAgo: nil),
            runningSince: running(600), now: now)
        let stopped = RelaySupervision.evaluate(
            run: .running, report: report(age: 2, heardAgo: 300),
            runningSince: running(600), now: now)

        XCTAssertEqual(never, .nothingArriving(silentFor: 600, everArrived: false))
        XCTAssertEqual(stopped, .nothingArriving(silentFor: 300, everArrived: true))
        XCTAssertNotEqual(never.summary, stopped.summary)
        XCTAssertTrue(stopped.summary.contains("was being heard and stopped"), stopped.summary)
    }

    // MARK: - the far more expensive mistake

    /// A relay that came up moments ago has heard nothing for exactly the
    /// reason a healthy one does. Restarting it is how supervision turns a
    /// working phone into a boot loop.
    func testAFreshRelayThatHasHeardNothingIsNotAFault() {
        for age in [0.0, 10.0, 44.0, RelaySupervision.deafAfter - 1] {
            let v = RelaySupervision.evaluate(run: .running,
                                              report: report(age: 1, heardAgo: nil),
                                              runningSince: running(age),
                                              now: now)
            XCTAssertEqual(v, .healthy, "a \(age)s-old relay must not be called deaf")
            XCTAssertFalse(v.isFault)
        }
    }

    /// The restart threshold is deliberately LATER than every display
    /// threshold. At 30s of silence the screen already says the router is quiet
    /// and supervision still says nothing is wrong, because a sentence that is
    /// early costs a word and a restart that is early costs the leg.
    func testTheScreenReportsBeforeSupervisionActs() {
        let quiet = RelayVerdict.routerQuietAfter + 5
        let stats = report(age: 1, heardAgo: quiet)

        XCTAssertEqual(RelayVerdict.evaluate(run: .running, report: stats, now: now),
                       .routerQuiet(silentFor: quiet))
        XCTAssertEqual(RelaySupervision.evaluate(run: .running, report: stats,
                                                 runningSince: running(600), now: now),
                       .healthy)
    }

    /// Stated as a relationship rather than as three magic numbers, so nobody
    /// can lower one of them past the display without this failing.
    func testEveryThresholdIsLaterThanTheDisplayItShadows() {
        XCTAssertGreaterThan(RelaySupervision.heartbeatStoppedAfter,
                             RelayStatus.stalenessThreshold,
                             "the screen must say 'not reporting' before anything restarts")
        XCTAssertGreaterThan(RelaySupervision.deafAfter,
                             RelayVerdict.routerQuietAfter,
                             "the screen must say 'router quiet' before anything restarts")
        // The router cannot dial a leg it has not admitted yet, so the deaf
        // threshold has to outlast one announcement lease or a slow console
        // reads as a wedge.
        XCTAssertGreaterThan(RelaySupervision.deafAfter,
                             LegAnnouncer.leaseSeconds,
                             "a relay still waiting to be admitted is not deaf")
    }

    // MARK: - the heartbeat

    func testAStoppedHeartbeatIsTheProcessGoingAway() {
        let v = RelaySupervision.evaluate(
            run: .running,
            report: report(age: RelaySupervision.heartbeatStoppedAfter + 1),
            runningSince: running(600), now: now)

        XCTAssertEqual(v, .heartbeatStopped(quietFor: RelaySupervision.heartbeatStoppedAfter + 1))
        XCTAssertTrue(v.isFault)
    }

    /// A STALE REPORT'S COUNTERS ARE A CORPSE. `cellularReady` and
    /// `budgetExhausted` in a report nobody has rewritten for a minute describe
    /// a process that is gone, so the heartbeat has to be read before any of
    /// them or the verdict is read from tea leaves.
    func testTheHeartbeatIsReadBeforeAnyCounter() {
        let dead = report(age: RelaySupervision.heartbeatStoppedAfter + 1) {
            $0.cellularReady = false
            $0.budgetExhausted = "Daily cap of 2 GB reached."
        }
        let v = RelaySupervision.evaluate(run: .running, report: dead,
                                          runningSince: running(600), now: now)

        guard case .heartbeatStopped = v else {
            return XCTFail("a stale report was judged from its own dead counters: \(v)")
        }
    }

    /// These phones sit unattended for days and take NTP corrections
    /// unprompted. A clock that jumps backwards must fall below every
    /// threshold rather than above them.
    func testAClockThatWentBackwardsNeverTripsAnything() {
        let future = RelayStatus(stats: report().stats, updatedAt: now.addingTimeInterval(600))
        XCTAssertEqual(RelaySupervision.evaluate(run: .running, report: future,
                                                 runningSince: running(600), now: now),
                       .healthy)

        // And the same for the datapath anchor: a relay whose recorded start is
        // in the future has been listening for a negative time.
        XCTAssertEqual(RelaySupervision.evaluate(run: .running, report: report(age: 1),
                                                 runningSince: now.addingTimeInterval(600),
                                                 now: now),
                       .healthy)
    }

    // MARK: - what a restart cannot fix

    func testNotRunningIsNotSupervised() {
        for run in [RelayRun.off, .starting, .stopping] {
            let v = RelaySupervision.evaluate(run: run, report: report(),
                                              runningSince: running(600), now: now)
            XCTAssertFalse(v.isFault, "\(run) must never be a fault")
            XCTAssertFalse(v.summary.isEmpty)
        }
    }

    /// The three causes of a missing report, none of which a restart touches.
    /// Named in the sentence because the next person to see this is reading a
    /// log line, not this file.
    func testNoReportNamesAllThreeCauses() {
        let v = RelaySupervision.evaluate(run: .running, report: nil,
                                          runningSince: running(600), now: now)

        XCTAssertFalse(v.isFault)
        for cause in ["client mode", "flushed its first heartbeat", "App Group"] {
            XCTAssertTrue(v.summary.contains(cause), "missing \(cause): \(v.summary)")
        }
    }

    /// A relay holding the data cap carries nothing BECAUSE it was told to.
    func testAPausedRelayIsNotAWedgedOne() {
        let v = RelaySupervision.evaluate(
            run: .running,
            report: report(age: 1) { $0.budgetExhausted = "Daily cap of 2 GB reached." },
            runningSince: running(600), now: now)

        XCTAssertFalse(v.isFault)
        XCTAssertTrue(v.summary.contains("Daily cap of 2 GB reached."), v.summary)
    }

    func testNoCellularIsNotAWedgeBecauseARestartCannotSummonARadio() {
        let v = RelaySupervision.evaluate(
            run: .running,
            report: report(age: 1) {
                $0.cellularReady = false
                $0.lastError = "up: no route to host"
            },
            runningSince: running(600), now: now)

        XCTAssertFalse(v.isFault)
        XCTAssertTrue(v.summary.contains("up: no route to host"), v.summary)
    }

    /// Without an anchor, "silent for ten minutes" and "started a second ago"
    /// are the same reading. Refuse rather than guess.
    func testNoStartAnchorRefusesRatherThanGuessing() {
        let v = RelaySupervision.evaluate(run: .running, report: report(age: 1),
                                          runningSince: nil, now: now)

        XCTAssertFalse(v.isFault)
        XCTAssertTrue(v.summary.contains("connectedDate"), v.summary)
    }

    /// `lastRouterInboundAt` is optional so a report from a build that predates
    /// it still decodes. In that window the forwarded count proves the router
    /// arrived and leaves only the WHEN unknown - the same allowance
    /// `RelayVerdict` makes before it says the router has sent nothing.
    func testAnOlderExtensionBinaryIsNotADeafSocket() {
        let v = RelaySupervision.evaluate(
            run: .running,
            report: report(age: 1, heardAgo: nil) { $0.upDatagrams = 4_812 },
            runningSince: running(600), now: now)

        XCTAssertFalse(v.isFault)
        XCTAssertTrue(v.summary.contains("older extension binary"), v.summary)
    }

    /// A jetsam leaves the last report behind - only a clean stop clears it -
    /// so the app can read an inbound timestamp belonging to the process that
    /// died while a fresh relay is seconds old. Measuring from that timestamp
    /// would restart the new relay immediately, every time, forever.
    func testInboundOlderThanTheRelayBelongsToTheProcessThatDied() {
        let leftover = report(age: 1, heardAgo: 3_600)
        let v = RelaySupervision.evaluate(run: .running, report: leftover,
                                          runningSince: running(5), now: now)

        XCTAssertEqual(v, .healthy)
    }

    // MARK: - who may do what

    private var wedged: RelaySupervision {
        RelaySupervision.evaluate(run: .running, report: report(age: 2, heardAgo: nil),
                                  runningSince: running(600), now: now)
    }

    /// The app holds the only lever that is a real restart: stop the tunnel,
    /// wait for it to go down, start it again. Unlike Android's
    /// `startForegroundService`, there is no live-service no-op to route round.
    func testTheAppRestartsTheTunnel() {
        let r = wedged.remedy(for: .app, onDemandArmed: false, lastRemedyAt: nil, now: now)

        guard case .restartTunnel = r else { return XCTFail("expected a restart, got \(r)") }
        XCTAssertTrue(r.acts)
    }

    /// The extension can only destroy itself. That is a remedy ONLY because an
    /// on-demand rule reconnects the tunnel afterwards.
    func testTheExtensionCancelsItselfWhenSomethingWillBringItBack() {
        let r = wedged.remedy(for: .tunnelExtension, onDemandArmed: true,
                              lastRemedyAt: nil, now: now)

        guard case .cancelTunnel = r else { return XCTFail("expected a cancel, got \(r)") }
        XCTAssertTrue(r.acts)
    }

    /// THE HARD GATE. With no router SSID configured `OnDemandPolicy` installs
    /// no rule, so cancelling would turn a leg that carries nothing into a leg
    /// that is gone until somebody opens the app.
    func testTheExtensionRefusesToCancelWithNothingToBringItBack() {
        let r = wedged.remedy(for: .tunnelExtension, onDemandArmed: false,
                              lastRemedyAt: nil, now: now)

        XCTAssertFalse(r.acts)
        XCTAssertTrue(r.why.contains("OnDemandPolicy installs no rule"), r.why)
        // And it says what would fix it, because a refusal nobody can act on is
        // a refusal nobody reads twice.
        XCTAssertTrue(r.why.contains("Set the router's wifi names"), r.why)
    }

    func testAHealthyRelayIsNeverTouchedByEitherSupervisor() {
        let healthy = RelaySupervision.evaluate(run: .running, report: report(age: 1),
                                                runningSince: running(600), now: now)
        XCTAssertEqual(healthy, .healthy)

        for who in RelaySupervisor.allCases {
            for armed in [true, false] {
                let r = healthy.remedy(for: who, onDemandArmed: armed,
                                       lastRemedyAt: nil, now: now)
                XCTAssertFalse(r.acts, "\(who) armed=\(armed) restarted a working relay")
            }
        }
    }

    // MARK: - the cooldown

    /// The extension re-evaluates on every heartbeat. Without a floor between
    /// attempts, a wedge that reproduces on restart cancels the tunnel every 75
    /// seconds forever on a phone nobody is holding.
    func testASecondRemedyInsideTheCooldownHolds() {
        let r = wedged.remedy(for: .tunnelExtension, onDemandArmed: true,
                              lastRemedyAt: now.addingTimeInterval(-60), now: now)

        XCTAssertFalse(r.acts)
        XCTAssertTrue(r.why.contains("Supervision already acted"), r.why)
        // Says how long the hold has left, so a log reader is not left counting.
        XCTAssertTrue(r.why.contains("holding for another"), r.why)
    }

    func testTheCooldownExpires() {
        let r = wedged.remedy(for: .tunnelExtension, onDemandArmed: true,
                              lastRemedyAt: now.addingTimeInterval(-RelaySupervision.remedyCooldown - 1),
                              now: now)
        XCTAssertTrue(r.acts)
    }

    /// A backwards clock makes the last remedy look like it happened in the
    /// future. That must hold rather than act - erring toward doing nothing is
    /// the right way to be wrong here.
    func testABackwardsClockHoldsRatherThanActing() {
        let r = wedged.remedy(for: .app, onDemandArmed: true,
                              lastRemedyAt: now.addingTimeInterval(600), now: now)
        XCTAssertFalse(r.acts)
    }

    // MARK: - nothing declines in silence

    /// THE RULE THIS WHOLE TYPE IS BUILT AROUND. Four mechanisms in this tree
    /// have been found declining without saying so, and each one cost hours
    /// before anybody worked out that the thing being debugged had never run.
    /// Every combination of verdict, supervisor, on-demand state and cooldown
    /// has to produce a sentence.
    func testEveryOutcomeCarriesAReadableReason() {
        for v in RelaySupervision.allCasesForCopyReview {
            XCTAssertFalse(v.summary.isEmpty, "\(v) has no summary")
            for who in RelaySupervisor.allCases {
                for armed in [true, false] {
                    for last in [nil, now.addingTimeInterval(-1)] as [Date?] {
                        let r = v.remedy(for: who, onDemandArmed: armed,
                                         lastRemedyAt: last, now: now)
                        XCTAssertGreaterThan(r.why.count, 40,
                            "\(v) / \(who) / armed=\(armed) / last=\(String(describing: last)) "
                          + "declined with: '\(r.why)'")
                        // A sentence, not a token. Whoever reads this is
                        // reading a log line at 2am.
                        XCTAssertTrue(r.why.hasSuffix("."), "not a sentence: \(r.why)")
                    }
                }
            }
        }
    }

    // MARK: - the cooldown has to survive the restart it causes

    func testTheRemedyMarkerRoundTripsThroughTheAppGroup() {
        let suite = "zippie.tests.\(UUID().uuidString)"
        let d = UserDefaults(suiteName: suite)!
        defer { d.removePersistentDomain(forName: suite) }

        // Never supervised is nil, not the epoch. Both answers happen to let
        // the next remedy through, but only one of them is true.
        XCTAssertNil(RelaySupervisionStore.lastRemedy(from: d))

        RelaySupervisionStore.recordRemedy(at: now, to: d)
        XCTAssertEqual(RelaySupervisionStore.lastRemedy(from: d)?.timeIntervalSince1970,
                       now.timeIntervalSince1970)

        RelaySupervisionStore.clear(from: d)
        XCTAssertNil(RelaySupervisionStore.lastRemedy(from: d))
    }

    /// `double(forKey:)` returns 0 for a missing key and 0 is a real date, so
    /// the absent case is decided by `object(forKey:)` instead.
    func testAZeroedMarkerReadsAsNeverRatherThanAs1970() {
        let suite = "zippie.tests.\(UUID().uuidString)"
        let d = UserDefaults(suiteName: suite)!
        defer { d.removePersistentDomain(forName: suite) }

        d.set(0.0, forKey: RelaySupervisionStore.key)
        XCTAssertNil(RelaySupervisionStore.lastRemedy(from: d))
    }

    /// The stored marker is what the cooldown is computed from, so the two have
    /// to be wired together and not merely both exist.
    func testAStoredMarkerSuppressesTheNextRemedy() {
        let suite = "zippie.tests.\(UUID().uuidString)"
        let d = UserDefaults(suiteName: suite)!
        defer { d.removePersistentDomain(forName: suite) }

        RelaySupervisionStore.recordRemedy(at: now.addingTimeInterval(-30), to: d)
        let r = wedged.remedy(for: .tunnelExtension, onDemandArmed: true,
                              lastRemedyAt: RelaySupervisionStore.lastRemedy(from: d), now: now)

        XCTAssertFalse(r.acts, r.why)
    }
}
