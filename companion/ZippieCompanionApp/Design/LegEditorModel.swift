import Combine
import Foundation
import SwiftUI
import ZippieCompanionKit

/// The state behind the leg editor: what the router says now, what the operator
/// has typed, and what the router said it did about it.
///
/// THREE SEPARATE THINGS, AND THEY ARE NEVER MERGED. `reported` is the router's
/// own numbers and only ever changes when the router is read. `draft` is text
/// somebody typed and means nothing until it is sent. `outcome` is what the
/// router answered. An editor that folded the draft into the reported values on
/// save - the usual optimistic update - would draw an edit as applied on the
/// strength of having asked for it, which is precisely the failure this whole
/// app exists to refuse. So a change appears in the reported column only after
/// a successful write and a REREAD.
@MainActor
final class LegEditorModel: ObservableObject {

    /// What the last write did, as far as anyone can honestly say.
    enum Outcome: Equatable {
        case none
        case working
        /// The router took it. `unconfirmed` is normally empty; a non-empty
        /// list is a PARTIAL write and reads as a failure in the UI, because
        /// that is what it is.
        case applied(confirmed: [LegEdit.Field], unconfirmed: [LegEdit.Field])
        case failed(String)
    }

    /// How a typed string turns into JSON. The router coerces to the config's
    /// own type, but sending a float where it keeps an int gets the value
    /// rounded somewhere the operator cannot see it happen.
    private enum Entry {
        case whole(min: Int, max: Int?)
        case decimal(min: Double)
        case text
    }

    let legName: String
    /// The label the status screen was showing. Carried in so the editor's
    /// title does not flicker from the raw name to the label on first load.
    let legLabel: String

    @Published private(set) var reported: LegEditSnapshot?
    @Published private(set) var loading = false
    @Published private(set) var loadError: String?
    @Published private(set) var outcome: Outcome = .none
    /// The router's receipt for the last successful write. Shown verbatim
    /// because it is the ONLY read-back for the descriptive fields - nothing in
    /// /api/status publishes carrier or plan name.
    @Published private(set) var receipt: LegEditReceipt?

    @Published var draft: [LegEdit.Field: String] = [:]
    /// Never prefilled from the keychain. Showing a stored secret back to
    /// whoever picks up the phone buys nothing - the app can say a token is
    /// stored without displaying it.
    @Published var tokenEntry = ""
    @Published private(set) var hasToken: Bool

    /// The address that actually answered. The write goes to the SAME console
    /// that was read, so an edit can never be sent to a router other than the
    /// one whose values are on screen.
    private var answering: URL?
    private var baseline: [LegEdit.Field: String] = [:]

    private let client: LegEditClient
    private let token: ConsoleWriteToken
    private let consoles: () -> [String]

    init(legName: String,
         legLabel: String,
         client: LegEditClient = LegEditClient(),
         token: ConsoleWriteToken = .shared,
         consoles: @escaping () -> [String] = { Settings.consoleCandidates.map(\.url) }) {
        self.legName = legName
        self.legLabel = legLabel
        self.client = client
        self.token = token
        self.consoles = consoles
        self.hasToken = token.read() != nil
    }

    // MARK: - reading

    func load() async {
        loading = true
        defer { loading = false }

        var lastFailure: LegEditError?
        for address in consoles() {
            guard let url = URL(string: address) else { continue }
            switch await client.snapshot(of: legName, status: url) {
            case .success(let leg):
                answering = url
                reported = leg
                adoptBaseline(from: leg)
                loadError = nil
                return
            case .failure(let error):
                lastFailure = error
            }
        }

        // NOT LEFT ON THE LAST GOOD SNAPSHOT. Stale values presented as current
        // are what an editor must never do: the operator would be typing a
        // correction against numbers that may already have moved, and the form
        // is deliberately unavailable until the router can be read again.
        answering = nil
        reported = nil
        loadError = lastFailure.map(\.message)
            ?? "No console address is configured. Set one in the Relay tab."
    }

    /// Prefill from what the router reports, so the form starts as the truth
    /// rather than as blanks. Descriptive fields get NO baseline: the console
    /// does not publish them, so there is nothing honest to prefill with.
    private func adoptBaseline(from leg: LegEditSnapshot) {
        var next: [LegEdit.Field: String] = [:]
        // The name IS published by the console, unlike the descriptive fields,
        // so the box starts as the current name rather than blank - otherwise
        // it reads as "no name set" for a leg that plainly has one.
        if let label = leg.label, !label.isEmpty { next[.label] = label }
        if let tier = leg.tier { next[.tier] = "\(tier)" }
        if let priority = leg.priority { next[.priority] = "\(priority)" }
        if let weight = leg.configWeight { next[.weight] = "\(weight)" }
        if let ceiling = leg.ceilingKbps { next[.maxKbps] = "\(ceiling)" }
        if let cap = leg.capGB { next[.monthlyCapGB] = Self.plain(cap) }
        baseline = next

        // Only fields the operator has not touched are refreshed. Overwriting a
        // half-typed correction because a poll landed would be its own bug.
        for (field, value) in next where draft[field] == nil {
            draft[field] = value
        }
    }

    // MARK: - the form

    func binding(for field: LegEdit.Field) -> Binding<String> {
        Binding(get: { [weak self] in self?.draft[field] ?? "" },
                set: { [weak self] in self?.draft[field] = $0 })
    }

    func reported(_ field: LegEdit.Field) -> String? { baseline[field] }

    /// Changed fields only.
    ///
    /// A BLANK FIELD MEANS "LEAVE IT ALONE", never "clear it". The two are
    /// impossible to tell apart from an empty text field, and guessing wrong
    /// silently removes a cap. Clearing is a separate, named action.
    var changed: [LegEdit.Field] {
        Self.editable.filter { field in
            guard let typed = draft[field]?.trimmingCharacters(in: .whitespaces),
                  !typed.isEmpty else { return false }
            return typed != (baseline[field] ?? "")
        }
    }

    var canSave: Bool { !changed.isEmpty && !loading && outcome != .working && reported != nil }

    // MARK: - writing

    func save() async {
        guard let console = answering, reported != nil else {
            outcome = .failed("The router has not been read yet, so there is "
                            + "nothing to change. Reload first.")
            return
        }
        guard let stored = token.read() else {
            outcome = .failed(LegEditError.tokenMissing.message)
            return
        }

        let edit: LegEdit
        do {
            edit = try buildEdit()
        } catch let problem as ValidationProblem {
            outcome = .failed(problem.message)
            return
        } catch {
            outcome = .failed(error.localizedDescription)
            return
        }

        let asked = edit.fields.keys.sorted { $0.rawValue < $1.rawValue }
        outcome = .working
        let result = await client.apply(edit, to: legName, console: console, token: stored)

        switch result {
        case .failure(let error):
            // The draft is left exactly as typed. Clearing it here would lose
            // the operator's work to a failure they did not cause.
            outcome = .failed(error.message)
        case .success(let confirmation):
            receipt = confirmation
            let missed = confirmation.unconfirmed(edit)
            outcome = .applied(confirmed: asked.filter { !missed.contains($0) },
                               unconfirmed: missed)
            // Reread rather than assume. The agent applies overrides
            // immediately on write, so the next status carries the new values -
            // and if it does not, the screen must show that instead.
            await load()
            for field in asked where !missed.contains(field) {
                draft[field] = baseline[field]
            }
        }
    }

    /// Remove the overrides this editor can set, so the router's own
    /// zippie.toml decides again.
    ///
    /// A REAL OPERATION, not a form reset. Setting a cap back to 0 by hand
    /// leaves an override in legs.json shadowing the config file; only a null
    /// removes it. The two look identical on the status screen afterwards and
    /// behave differently the next time zippie.toml changes.
    func clearOverrides() async {
        guard let console = answering else {
            outcome = .failed("The router has not been read yet. Reload first.")
            return
        }
        guard let stored = token.read() else {
            outcome = .failed(LegEditError.tokenMissing.message)
            return
        }
        var edit = LegEdit()
        for field in Self.overridable { edit.clear(field) }

        outcome = .working
        switch await client.apply(edit, to: legName, console: console, token: stored) {
        case .failure(let error):
            outcome = .failed(error.message)
        case .success(let confirmation):
            receipt = confirmation
            let missed = confirmation.unconfirmed(edit)
            outcome = .applied(confirmed: Self.overridable.filter { !missed.contains($0) },
                               unconfirmed: missed)
            draft = [:]
            await load()
        }
    }

    // MARK: - the token

    /// Stores the token and reports whether it landed. The keychain can refuse,
    /// and a UI that assumed it did not would show a saved token that is not
    /// there and then blame the router for the 401.
    @discardableResult
    func saveToken() -> Bool {
        guard token.save(tokenEntry) else {
            outcome = .failed("The token could not be stored on this phone. "
                            + "Nothing was sent to the router.")
            return false
        }
        tokenEntry = ""
        hasToken = true
        return true
    }

    func forgetToken() {
        token.remove()
        hasToken = token.read() != nil
    }

    // MARK: - validation

    private struct ValidationProblem: Error { let message: String }

    private func buildEdit() throws -> LegEdit {
        var edit = LegEdit()
        for field in changed {
            let typed = (draft[field] ?? "").trimmingCharacters(in: .whitespaces)
            switch Self.entry(for: field) {
            case .whole(let low, let high):
                guard let value = Int(typed), value >= low, high.map({ value <= $0 }) ?? true else {
                    throw ValidationProblem(message: Self.rule(for: field))
                }
                edit.set(field, .int(value))
            case .decimal(let low):
                guard let value = Double(typed), value >= low else {
                    throw ValidationProblem(message: Self.rule(for: field))
                }
                edit.set(field, .number(value))
            case .text:
                edit.set(field, .text(typed))
            }
        }
        guard !edit.isEmpty else { throw ValidationProblem(message: LegEditError.nothingToSend.message) }
        return edit
    }

    /// Fields this screen writes, in the order it presents them.
    static let editable: [LegEdit.Field] = [
        // .label FIRST and non-negotiable. It was missing from this array while
        // the field was rendered on the screen, the Kit carried it, and the
        // router accepted it - so renaming a leg looked possible, typed fine,
        // and silently did nothing. `changed` filters on THIS list, so a field
        // absent here is a dead control no matter what the UI shows.
        .label,
        .tier, .weight, .priority, .maxKbps, .monthlyCapGB,
        .carrier, .planName, .planType, .billingDay,
    ]

    /// The subset that changes routing, and therefore the subset "clear
    /// overrides" hands back to zippie.toml.
    static let overridable: [LegEdit.Field] = [.tier, .weight, .priority, .maxKbps, .monthlyCapGB]

    private static func entry(for field: LegEdit.Field) -> Entry {
        switch field {
        case .tier:         return .whole(min: 1, max: 99)
        case .weight:       return .whole(min: 0, max: nil)
        case .priority:     return .whole(min: 0, max: nil)
        case .maxKbps:      return .whole(min: 0, max: nil)
        case .monthlyCapGB: return .decimal(min: 0)
        case .billingDay:   return .whole(min: 1, max: 31)
        default:            return .text
        }
    }

    /// What a rejected entry has to say to be actionable: the field, and the
    /// range that would have been accepted.
    private static func rule(for field: LegEdit.Field) -> String {
        switch field {
        case .tier:         return "Tier must be a whole number, 1 or higher."
        case .weight:       return "Weight must be a whole number, 0 or higher."
        case .priority:     return "Priority must be a whole number, 0 or higher."
        case .maxKbps:      return "The ceiling must be a whole number of kilobits per second, or 0 for none."
        case .monthlyCapGB: return "The monthly cap must be a number of GB, 0 or higher."
        case .billingDay:   return "The billing day must be a day of the month, 1 to 31."
        default:            return "That value could not be read."
        }
    }

    static func title(_ field: LegEdit.Field) -> String {
        switch field {
        case .tier:         return "Tier"
        case .weight:       return "Weight"
        case .priority:     return "Priority"
        case .maxKbps:      return "Ceiling"
        case .monthlyCapGB: return "Cap"
        case .carrier:      return "Carrier"
        case .planName:     return "Plan"
        case .planType:     return "Plan type"
        case .billingDay:   return "Billing day"
        default:            return field.rawValue
        }
    }

    /// A number without a trailing ".0", because "50" is what somebody typed
    /// and "50.0" is a rendering artefact they then have to match by hand.
    static func plain(_ value: Double) -> String {
        value == value.rounded() ? "\(Int(value))" : "\(value)"
    }
}
