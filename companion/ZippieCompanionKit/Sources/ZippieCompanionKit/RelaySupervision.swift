import Foundation

/// Whether a relay that is RUNNING is actually being serviced, and which of the
/// two processes on this phone is in a position to do anything about it.
///
/// THE INCIDENT, 2026-08-22. A companion phone sat in the router's leg list as
/// `state: degraded, loss_pct: 65, rtt_ms: null, in_bond: true`, on wifi
/// measured at -35 dBm and 576 Mbit/s. `rtt_ms: null` is the whole tell: a
/// genuinely lossy link still returns SOME probes, and this leg had never once
/// completed a round trip. The extension was up, the listener was bound, the
/// announcement lease was being renewed - and nothing was servicing the socket.
/// So the router dialled a leg that was announced, in the bond, and deaf, and
/// spread real traffic over a path that swallowed it.
///
/// The phone's own screen said "Ready" - `RelayVerdict.listening`, which is the
/// correct and benign sentence for the first ten seconds of a relay's life and
/// the wrong one after ten minutes. Nothing on the phone escalated, because
/// nothing on the phone was watching the clock.
///
/// WHAT ANDROID DOES, AND THE ONE PART OF IT THAT PORTS. `RelayLiveness` over
/// there watches ONE signal - the report heartbeat - because its insight is
/// that a frozen relay IS a running relay, so supervision's
/// `startForegroundService` was a no-op that had been confirming the broken
/// state every 15 minutes and changing nothing. What ports is the SHAPE of the
/// judgement: evidence in, a verdict with a reason out, and thresholds derived
/// from the display's and deliberately later than them, because being wrong
/// costs a working leg.
///
/// What does not port is the hands. Android's supervisor is a directBootAware
/// receiver woken by `setAndAllowWhileIdle`, which the system delivers even in
/// Doze, so something always wakes up to look. iOS has no such thing, and
/// pretending otherwise would be the kind of mechanism that declines silently.
///
/// WHAT iOS CAN ACTUALLY DO, STATED BEFORE ANY CODE RELIES ON IT. There are two
/// processes and each has exactly one lever:
///
///   - THE APP can `stopVPNTunnel()` and then `startVPNTunnel()`. That is a
///     genuine teardown and rebuild of the extension PROCESS - strictly
///     stronger than Android's lever, because there is no "deliver another
///     onStartCommand to a live service" no-op to route around. It works only
///     while the app is running, which on iOS means while somebody has it open.
///   - THE EXTENSION can `cancelTunnelWithError(_:)`, whose own SDK header
///     describes it as being "called by tunnel provider implementations to
///     initiate tunnel destruction when a network error is encountered that
///     renders the tunnel no longer viable" - a description of this fault
///     rather than a reach. What brings the tunnel BACK is the on-demand rule
///     `TunnelProfile` installs: with a router SSID configured the system
///     reconnects on its own. With no SSID, `OnDemandPolicy.isEnabled` is false
///     and NO rule is installed, so a cancel would take the leg down with
///     nothing on the phone able to raise it. The extension's remedy is hard
///     gated on that fact and declines out loud when it is missing.
///
/// AND THE GAP THAT IS NOT CLOSED HERE. If the extension process is alive but
/// is not being SCHEDULED, the code that would notice is the code that is not
/// running, and the app is not open to notice on its behalf. That state
/// recovers only on a network change - which re-evaluates the on-demand rules -
/// or when a person opens the app. There is no background wake-up on iOS to
/// close it with: `BGAppRefreshTask` is opportunistic, budgeted, and does not
/// run at all after a force-quit, and a silent push needs the very network
/// under suspicion. Written down rather than papered over with a mechanism that
/// would never fire.
///
/// Pure, and takes evidence plus a clock, so every branch is provable under
/// `swift test`. The app and the extension have no test target at all (#48),
/// which is exactly why the decision lives here and not there.
public enum RelaySupervision: Equatable, Sendable {

    /// There is nothing to judge, or nothing a restart could fix. Carries the
    /// sentence saying which - a supervisor that declines without a reason is
    /// indistinguishable from one that is broken.
    case standDown(why: String)

    /// Reporting on schedule and hearing the router. Do not touch it:
    /// restarting a working relay drops the bond's leg for nothing.
    case healthy

    /// The report has stopped being rewritten. The extension is gone, or is
    /// alive and no longer being scheduled.
    case heartbeatStopped(quietFor: TimeInterval)

    /// The heartbeat is fine and nothing is arriving from the router - the
    /// process is alive and its datapath is not being serviced. This is the
    /// 2026-08-22 leg.
    ///
    /// `everArrived` is kept because "never dialled" and "dialled and stopped"
    /// are different faults with different fixes, the same distinction
    /// `RelayVerdict` splits `.listening` from `.routerQuiet` for.
    case nothingArriving(silentFor: TimeInterval, everArrived: Bool)

    // MARK: - thresholds, all derived rather than picked

    /// How long the heartbeat may be silent before the extension counts as gone.
    ///
    /// DERIVED FROM THE SCREEN'S THRESHOLD so the two cannot drift apart when
    /// the heartbeat interval changes. `RelayStatus.stalenessThreshold` is what
    /// the UI uses to say "not reporting" - a display decision, where being
    /// early costs a word. This is a RESTART decision, where being early costs
    /// a working leg, so it waits three times as long. Android draws the same
    /// line for the same reason (`RelayLiveness.FROZEN_AFTER_MS`, six
    /// heartbeats against the display's five).
    public static let heartbeatStoppedAfter: TimeInterval = 3 * RelayStatus.stalenessThreshold

    /// How long the router may go unheard before the socket counts as deaf.
    ///
    /// PAIRED WITH THE SCREEN'S QUIET THRESHOLD, three times over.
    /// `RelayVerdict.routerQuietAfter` is 25s because the router's
    /// `persistent_keepalive` is 15s, so a live leg proves itself at least that
    /// often with no user traffic at all, and in packet mode the agent sprays
    /// probes every 500ms. Three consecutive display-level quiet windows with
    /// NOTHING arriving is not an idle bond, it is a socket nobody is
    /// servicing.
    ///
    /// It also has to clear one announcement lease
    /// (`LegAnnouncer.leaseSeconds`, 45s) for the never-arrived case: the
    /// router cannot dial a leg it has not accepted yet, and calling a relay
    /// deaf while it is still waiting to be admitted would restart it forever
    /// on a slow console.
    public static let deafAfter: TimeInterval = 3 * RelayVerdict.routerQuietAfter

    /// The floor between two remedies.
    ///
    /// MATCHED TO ANDROID'S SUPERVISION CADENCE (`BootReceiver`'s steady-state
    /// retry, 15 minutes) rather than invented, because it answers the same
    /// question: how often is it worth re-trying a fix that has already failed
    /// once. The rate this guards is real - the extension re-evaluates on every
    /// heartbeat, so with no floor a wedge that reproduces on restart would
    /// cancel the tunnel every 75 seconds, forever, on a phone nobody is
    /// holding.
    public static let remedyCooldown: TimeInterval = 15 * 60

    // MARK: - the decision

    /// Judge the relay from the evidence.
    ///
    /// ORDER IS THE ARGUMENT, the same way it is in `RelayVerdict.evaluate`.
    /// The heartbeat is read before any counter, because a stale report's
    /// counters are a corpse and deciding anything from them would be reading
    /// tea leaves. Then the states a restart cannot help, so they can never be
    /// mistaken for a wedge. Only then the datapath.
    ///
    /// - Parameters:
    ///   - runningSince: when THIS relay instance began listening. The app
    ///     takes it from `NEVPNConnection.connectedDate`; the extension knows
    ///     its own start. Optional because a caller that cannot supply it must
    ///     get an explicit refusal, not a guess - see the branch below.
    public static func evaluate(
        run: RelayRun,
        report: RelayStatus?,
        runningSince: Date?,
        now: Date = Date(),
        heartbeatStoppedAfter: TimeInterval = RelaySupervision.heartbeatStoppedAfter,
        deafAfter: TimeInterval = RelaySupervision.deafAfter
    ) -> RelaySupervision {
        switch run {
        case .off:      return .standDown(why: "The relay is off. There is nothing to supervise.")
        case .starting: return .standDown(why: "The tunnel is still coming up.")
        case .stopping: return .standDown(why: "The tunnel is on its way down.")
        case .running:  break
        }

        guard let report else {
            return .standDown(why:
                "The tunnel is up and no report has been written. That has three causes and a "
              + "restart fixes none of them: client mode never writes one, a contributor may not "
              + "have flushed its first heartbeat yet, and a mis-signed App Group entitlement "
              + "makes every write vanish in silence.")
        }

        // A CLOCK THAT WENT BACKWARDS MUST NEVER READ AS A STOPPED HEARTBEAT.
        // These phones sit unattended for days and take NTP corrections
        // unprompted, so a negative age is expected rather than impossible. It
        // falls below every threshold here by construction, which is the right
        // way for this to be wrong: waiting one more cycle costs seconds, and a
        // restart loop costs the leg.
        let quiet = now.timeIntervalSince(report.updatedAt)
        if quiet >= heartbeatStoppedAfter { return .heartbeatStopped(quietFor: quiet) }

        let stats = report.stats
        if let reason = stats.budgetExhausted {
            return .standDown(why:
                "Paused on purpose - \(reason) A relay holding the cap carries nothing BECAUSE it "
              + "was told to, and restarting it would spend the first byte it saved.")
        }
        guard stats.cellularReady else {
            let detail = stats.lastError.map { " - \($0)" } ?? ""
            return .standDown(why:
                "Cellular is not usable\(detail). Restarting the tunnel cannot summon a radio, "
              + "and the screen already says so (RelayVerdict.noCellular).")
        }

        guard let runningSince else {
            return .standDown(why:
                "Nothing here knows when this relay started listening, so silence cannot be told "
              + "apart from a relay that came up a second ago. The caller has to supply the "
              + "anchor - the app has NEVPNConnection.connectedDate, the extension has its own "
              + "start - and guessing one would restart healthy legs at boot.")
        }

        // AN OLDER EXTENSION BINARY IS NOT A DEAF SOCKET. `lastRouterInboundAt`
        // is optional precisely so a report written by a build that predates it
        // still decodes, and in that case the forwarded count PROVES the router
        // arrived and leaves only the WHEN unknown. `RelayVerdict` makes the
        // same allowance before it says "the router has not sent anything";
        // this one has to make it before it restarts anything.
        if stats.lastRouterInboundAt == nil, stats.upDatagrams > 0 {
            return .standDown(why:
                "The report carries no inbound timestamp, but \(stats.upDatagrams) datagrams have "
              + "been forwarded, which proves the router arrived. That is an older extension "
              + "binary mid-upgrade, not a deaf socket, and calling it one would restart a leg "
              + "that is working.")
        }

        // INBOUND OLDER THAN THE RELAY BELONGS TO A DIFFERENT PROCESS. A jetsam
        // leaves the last report behind (only a clean `stopTunnel` clears it),
        // so the app can read a timestamp from the instance that died while a
        // fresh one is seconds old. Anchoring on whichever is LATER asks the
        // only sensible question: when did THIS relay last have a reason to be
        // quiet.
        let lastHeard = max(stats.lastRouterInboundAt ?? runningSince, runningSince)
        let silence = now.timeIntervalSince(lastHeard)
        guard silence >= deafAfter else { return .healthy }
        return .nothingArriving(silentFor: silence,
                                everArrived: stats.lastRouterInboundAt != nil)
    }

    /// Whether this is a fault at all. `.standDown` deliberately is not one:
    /// declining to judge and judging something healthy are different answers,
    /// and only one of them is evidence that the relay is fine.
    public var isFault: Bool {
        switch self {
        case .heartbeatStopped, .nothingArriving: return true
        case .healthy, .standDown:                return false
        }
    }

    /// A short, stable token per case, with no numbers in it.
    ///
    /// FOR DE-DUPLICATION, NOT FOR READING. Supervision runs once per heartbeat
    /// - every two seconds - so a caller that logged whenever the SENTENCE
    /// changed would log on every pass, because `summary` carries durations
    /// that tick. This is what "has anything actually changed" is asked of, and
    /// it is the shape a metric tag wants for the same reason
    /// `Observability.verdictName` exists for `ProbeVerdict`.
    ///
    /// Every `.standDown` collapses to one token deliberately. They are all
    /// non-faults, they are all already on the Relay screen in full, and
    /// splitting them would put the one with a climbing datagram count back in
    /// the log every two seconds.
    public var name: String {
        switch self {
        case .standDown:        return "stand-down"
        case .healthy:          return "healthy"
        case .heartbeatStopped: return "heartbeat-stopped"
        case let .nothingArriving(_, everArrived):
            return everArrived ? "stopped-arriving" : "never-arrived"
        }
    }

    /// One line that has to be enough to diagnose from `log stream` alone - in
    /// the extension that log line is the ONLY artefact anybody ever sees.
    public var summary: String {
        switch self {
        case let .standDown(why):
            return why
        case .healthy:
            return "Reporting on schedule and hearing the router."
        case let .heartbeatStopped(quietFor):
            return "No report for \(RelayVerdict.ago(quietFor)). The heartbeat is rewritten every "
                 + "\(Int(RelayStatus.heartbeatInterval))s even when nothing changes, so silence "
                 + "there means the extension is gone or is no longer being scheduled."
        case let .nothingArriving(silentFor, everArrived):
            let lead = everArrived
                ? "The router was being heard and stopped \(RelayVerdict.ago(silentFor)) ago."
                : "Listening \(RelayVerdict.ago(silentFor)) and the router has never once arrived."
            return lead
                 + " The heartbeat is fine, so the process is alive and it is the datapath that is "
                 + "not being serviced. From the router this is the leg drawn as degraded with no "
                 + "round trip ever measured."
        }
    }

    /// What the asking process may do about it, and why - including when the
    /// answer is nothing.
    ///
    /// THE REMEDY DEPENDS ON WHO IS ASKING, not on which fault it is. Both
    /// faults are cured by the same thing, a relay process that is torn down
    /// and built again; the two supervisors simply hold different levers to
    /// reach it, and one of those levers only works when on-demand is armed.
    ///
    /// - Parameters:
    ///   - onDemandArmed: whether an on-demand rule is installed for this
    ///     tunnel, i.e. whether anything would reconnect it. Read from
    ///     `OnDemandPolicy.isEnabled` over the SAME router SSIDs `TunnelProfile`
    ///     builds its rules from, so the two cannot disagree.
    ///   - lastRemedyAt: when supervision last acted, from
    ///     `RelaySupervisionStore` - which persists it across the restart it
    ///     caused, because an in-memory value would die with the process and be
    ///     no cooldown at all.
    public func remedy(for supervisor: RelaySupervisor,
                       onDemandArmed: Bool,
                       lastRemedyAt: Date?,
                       now: Date = Date(),
                       cooldown: TimeInterval = RelaySupervision.remedyCooldown) -> RelayRemedy {
        guard isFault else { return .hold(why: summary) }

        if let lastRemedyAt {
            // A backwards clock reads as "acted a moment ago" and holds. That
            // errs toward doing nothing, which is the right way to be wrong.
            let since = now.timeIntervalSince(lastRemedyAt)
            if since < cooldown {
                return .hold(why:
                    "\(summary) Supervision already acted \(RelayVerdict.ago(max(0, since))) ago "
                  + "and is holding for another \(RelayVerdict.ago(cooldown - since)). A restart "
                  + "that did not fix this will not fix it sooner, and this branch is reached "
                  + "every \(Int(RelayStatus.heartbeatInterval))s.")
            }
        }

        switch supervisor {
        case .app:
            return .restartTunnel(why:
                "\(summary) Stopping the tunnel and starting it again really does tear the "
              + "extension process down and build a new one. The app is the only thing that can, "
              + "and only while it is open.")
        case .tunnelExtension:
            guard onDemandArmed else {
                return .hold(why:
                    "\(summary) The extension's only lever is cancelTunnelWithError, and nothing "
                  + "would bring the tunnel back: no router wifi name is configured, so "
                  + "OnDemandPolicy installs no rule. Cancelling here would turn a leg that "
                  + "carries nothing into a leg that is gone. Set the router's wifi names to let "
                  + "this phone recover without anyone holding it.")
            }
            return .cancelTunnel(why:
                "\(summary) Cancelling from inside the extension, which the on-demand rule then "
              + "reconnects on the router's wifi. This is the only remedy that works with nobody "
              + "holding the phone.")
        }
    }

    /// Every case with a representative payload, so a rule can be asserted
    /// across all of them at once. Not `CaseIterable` - the associated values
    /// mean there is no single instance per case, and a rule that only checked
    /// the payload-free ones would miss exactly the sentences that vary. The
    /// same reason `RelayVerdict.allCasesForCopyReview` exists.
    static let allCasesForCopyReview: [RelaySupervision] = [
        .standDown(why: "The relay is off. There is nothing to supervise."),
        .healthy,
        .heartbeatStopped(quietFor: 45),
        .nothingArriving(silentFor: 90, everArrived: false),
        .nothingArriving(silentFor: 90, everArrived: true),
    ]
}

/// Which process is asking. They see the same evidence and hold different
/// levers, so the verdict is shared and the remedy is not.
public enum RelaySupervisor: String, Equatable, Sendable, CaseIterable {
    /// The containing app, which owns the `NETunnelProviderManager` and is the
    /// only thing that can stop and start a tunnel. Awake only while open.
    case app
    /// The Network Extension, which can only destroy itself and rely on the
    /// on-demand rule to bring the tunnel back. Awake whenever it is scheduled,
    /// which is the case that matters: nobody is holding this phone.
    case tunnelExtension
}

/// What supervision may do, and why. Every case carries a sentence, including
/// - especially - the one that does nothing.
public enum RelayRemedy: Equatable, Sendable {
    /// Change nothing. `why` is not decoration: four separate mechanisms in
    /// this tree have been found declining in silence, and each cost hours
    /// before anyone worked out that the thing they were debugging had never
    /// run.
    case hold(why: String)
    /// App only. `stopVPNTunnel()`, wait for `.disconnected`, `startVPNTunnel()`.
    case restartTunnel(why: String)
    /// Extension only. `cancelTunnelWithError(_:)`, with the on-demand rule as
    /// the thing that reconnects.
    case cancelTunnel(why: String)

    public var why: String {
        switch self {
        case let .hold(why), let .restartTunnel(why), let .cancelTunnel(why): return why
        }
    }

    /// Whether anything is about to happen. Exists so a caller can log at the
    /// right level and record the remedy in the store without re-switching.
    public var acts: Bool {
        switch self {
        case .hold:                            return false
        case .restartTunnel, .cancelTunnel:    return true
        }
    }
}

/// Remembers when supervision last acted, ACROSS the restart it caused.
///
/// AN IN-MEMORY COOLDOWN WOULD BE NO COOLDOWN AT ALL. Both remedies end the
/// process that decided on them, so a `lastRemedyAt` held in a variable dies
/// with it and the replacement starts from a clean slate. The real interval
/// between attempts would then be however long the fault takes to recur - which
/// for a wedge that reproduces immediately is exactly the thrash the cooldown
/// exists to prevent.
///
/// App group defaults for the same reasons `RelayStatusStore` uses them: a tiny
/// value, written rarely, that two processes both have to see.
public enum RelaySupervisionStore {
    static let key = "relaySupervisionLastRemedy"

    /// STORED AS SECONDS AND READ BACK THROUGH `object(forKey:)`, not through
    /// `double(forKey:)` alone: that returns 0 for a missing key and 0 is a
    /// real date, so a phone that has never been supervised would read as one
    /// supervised in 1970. Harmless in this direction - an ancient date means
    /// the cooldown has expired, which is the correct answer for "never acted"
    /// - but it is the shape of bug that bites whoever copies this next, so it
    /// is closed here rather than left as a coincidence.
    public static func lastRemedy(from defaults: UserDefaults) -> Date? {
        guard defaults.object(forKey: key) != nil else { return nil }
        let seconds = defaults.double(forKey: key)
        guard seconds > 0 else { return nil }
        return Date(timeIntervalSince1970: seconds)
    }

    public static func recordRemedy(at now: Date = Date(), to defaults: UserDefaults) {
        defaults.set(now.timeIntervalSince1970, forKey: key)
    }

    /// Called when the operator stops or removes the tunnel by hand. A cooldown
    /// left behind from an automatic restart must not suppress the first
    /// supervision of a relay a person just started.
    public static func clear(from defaults: UserDefaults) {
        defaults.removeObject(forKey: key)
    }
}
