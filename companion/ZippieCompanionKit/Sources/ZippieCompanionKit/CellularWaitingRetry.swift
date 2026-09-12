import Foundation

/// Whether a stuck cellular `NWConnection` should be torn down and recreated
/// (#92).
///
/// KEPT SEPARATE FROM `CellularRelay`, deliberately. Network.framework's real
/// socket paths "cannot be driven off-device" (`CellularRelay`'s own comment)
/// - but the DECISION of when to give up on iOS's own `.waiting` recovery and
/// force a fresh attempt is pure arithmetic on a timestamp and a generation
/// counter, and that half can and should be tested directly.
///
/// FOUND LIVE 2026-09-12: an iPhone with genuine 5G UW signal sat with its
/// relay's cellular connection stuck in `.waiting` for 14+ minutes
/// (`never_handshaked: true` on the router, 460 failed probes), with none of
/// the three documented `.waiting` causes present (aeroplane mode, no
/// cellular data for the app, Low Data Mode - all checked directly on the
/// device). `CellularRelay` created its one `NWConnection` at start and never
/// looked at it again, trusting iOS's own automatic `.waiting -> .ready`
/// recovery indefinitely - which Apple's docs promise but which is widely
/// unreliable in practice with `requiredInterfaceType` set.
///
/// THE GENERATION COUNTER IS WHAT MAKES A SCHEDULED RETRY SAFE TO IGNORE. A
/// retry is scheduled once per continuous waiting period, `cellularWaitingTimeout`
/// in the future. By the time it fires, the world may have moved on three
/// different ways: the connection went `.ready` on its own (this one must do
/// nothing), a PREVIOUS retry already fired and recreated the connection
/// (this one is stale and must not recreate it a second time), or `stop()`
/// was called (this one must not restart a relay that was deliberately
/// stopped). `shouldRetry` is the single gate all three routes back through.
struct CellularWaitingRetry: Sendable, Equatable {
    private(set) var waitingSince: Date?
    private(set) var generation = 0

    /// Cellular entered (or is still in) `.waiting`. Returns the generation
    /// to schedule a retry check against - capturing it here, not reading it
    /// back out later, is what lets a later `invalidate()` or a completed
    /// retry make that scheduled check a no-op.
    mutating func enteredWaiting(now: Date) -> Int {
        if waitingSince == nil { waitingSince = now }
        return generation
    }

    /// Cellular left `.waiting` on its own (reached `.ready`, or failed
    /// outright - either way there is nothing left to retry).
    mutating func leftWaiting() {
        waitingSince = nil
    }

    /// A relay stop. Bumps the generation so any retry already scheduled
    /// before the stop becomes stale rather than restarting a relay that was
    /// deliberately shut down.
    mutating func invalidate() {
        waitingSince = nil
        generation += 1
    }

    /// Called when a scheduled retry timer fires. `true` means: this is
    /// still the same uninterrupted waiting period that scheduled it, it has
    /// genuinely been waiting for at least `timeout`, and the caller should
    /// tear down and recreate the connection now - this call has already
    /// bumped the generation and cleared `waitingSince` to reflect that.
    mutating func shouldRetry(scheduledFor generation: Int, now: Date, timeout: TimeInterval) -> Bool {
        guard generation == self.generation,
              let since = waitingSince,
              now.timeIntervalSince(since) >= timeout
        else { return false }
        self.generation += 1
        self.waitingSince = nil
        return true
    }
}
