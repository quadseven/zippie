import Foundation

/// A hard ceiling on how much cellular the relay may spend.
///
/// WHY THIS BECAME URGENT. While the relay only ran in the foreground, the
/// user watching the screen WAS the budget - it could not burn a plan
/// unattended because it could not run unattended. On-demand (#2250) removed
/// that limiter: the tunnel now starts itself whenever the phone joins the
/// router's wifi and keeps relaying with the screen off. Per-connection daily
/// and monthly caps are table stakes for anything that relays unattended, and
/// the phase-2 review flagged the absence.
///
/// A BUDGET THAT ONLY WARNS IS NOT A BUDGET. Exceeding it stops the relay.
/// The alternative - carry on and show a badge - is the behaviour that
/// produces a surprise bill, and the phone is the one place in this system
/// where the data is metered and someone else pays for it.
public struct DataBudget: Sendable, Equatable, Codable {
    /// Bytes per day. Zero means unlimited, which is the correct default for a
    /// feature nobody has configured: inventing a cap would silently throttle a
    /// working relay.
    public var dailyBytes: UInt64
    /// Bytes per calendar month. Zero means unlimited.
    public var monthlyBytes: UInt64

    public init(dailyBytes: UInt64 = 0, monthlyBytes: UInt64 = 0) {
        self.dailyBytes = dailyBytes
        self.monthlyBytes = monthlyBytes
    }

    public static let unlimited = DataBudget()
    public var isConfigured: Bool { dailyBytes > 0 || monthlyBytes > 0 }
}

/// What the relay is allowed to do right now.
public enum BudgetVerdict: Equatable, Sendable {
    case allowed
    case dailyExhausted(used: UInt64, limit: UInt64)
    case monthlyExhausted(used: UInt64, limit: UInt64)

    public var isAllowed: Bool { self == .allowed }

    /// Said in plain words, because "relay stopped" with no reason is
    /// indistinguishable from a crash.
    public var reason: String? {
        switch self {
        case .allowed:
            return nil
        case let .dailyExhausted(used, limit):
            return "Daily data cap reached (\(Self.mb(used)) of \(Self.mb(limit)))."
                + " Relaying resumes tomorrow."
        case let .monthlyExhausted(used, limit):
            return "Monthly data cap reached (\(Self.mb(used)) of \(Self.mb(limit)))."
                + " Relaying resumes next month."
        }
    }

    private static func mb(_ b: UInt64) -> String {
        String(format: "%.0f MB", Double(b) / 1_048_576)
    }
}

/// Counts relayed bytes against the budget, and rolls the counters over.
///
/// Counts BOTH directions. A relay that only counted upstream would let the
/// download half of a bonded session run unmetered, which on a phone is the
/// larger half.
public struct BudgetLedger: Sendable, Equatable, Codable {
    public var budget: DataBudget
    public private(set) var dayUsed: UInt64
    public private(set) var monthUsed: UInt64
    /// Day-of-era and month index of the counters, so rollover is a comparison
    /// rather than a timer that has to survive the process being suspended -
    /// which, on a phone, it will not.
    public private(set) var dayStamp: Int
    public private(set) var monthStamp: Int

    public init(budget: DataBudget = .unlimited, now: Date = Date(),
                calendar: Calendar = .current) {
        self.budget = budget
        self.dayUsed = 0
        self.monthUsed = 0
        self.dayStamp = Self.dayIndex(now, calendar)
        self.monthStamp = Self.monthIndex(now, calendar)
    }

    static func dayIndex(_ d: Date, _ c: Calendar) -> Int {
        let comps = c.dateComponents([.year, .month, .day], from: d)
        return (comps.year ?? 0) * 10_000 + (comps.month ?? 0) * 100 + (comps.day ?? 0)
    }

    static func monthIndex(_ d: Date, _ c: Calendar) -> Int {
        let comps = c.dateComponents([.year, .month], from: d)
        return (comps.year ?? 0) * 100 + (comps.month ?? 0)
    }

    /// Rolls the day and month counters if the calendar has moved on.
    /// Idempotent, and safe to call on every datagram.
    public mutating func rollover(now: Date = Date(), calendar: Calendar = .current) {
        let d = Self.dayIndex(now, calendar)
        let m = Self.monthIndex(now, calendar)
        if d != dayStamp {
            dayUsed = 0
            dayStamp = d
        }
        if m != monthStamp {
            monthUsed = 0
            monthStamp = m
        }
    }

    public mutating func record(bytes: UInt64, now: Date = Date(),
                                calendar: Calendar = .current) {
        rollover(now: now, calendar: calendar)
        // Saturating rather than wrapping. A counter that wraps past UInt64.max
        // would reset the budget to zero used, which is the one arithmetic
        // outcome that silently disables the cap.
        dayUsed = dayUsed.addingReportingOverflow(bytes).overflow ? .max : dayUsed + bytes
        monthUsed = monthUsed.addingReportingOverflow(bytes).overflow ? .max : monthUsed + bytes
    }

    public func verdict(now: Date = Date(), calendar: Calendar = .current) -> BudgetVerdict {
        var probe = self
        probe.rollover(now: now, calendar: calendar)
        if probe.budget.monthlyBytes > 0 && probe.monthUsed >= probe.budget.monthlyBytes {
            return .monthlyExhausted(used: probe.monthUsed, limit: probe.budget.monthlyBytes)
        }
        if probe.budget.dailyBytes > 0 && probe.dayUsed >= probe.budget.dailyBytes {
            return .dailyExhausted(used: probe.dayUsed, limit: probe.budget.dailyBytes)
        }
        return .allowed
    }
}
