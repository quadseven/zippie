import Foundation
import Security

/// Writing to the router: one leg, one edit, one answer.
///
/// DELIBERATELY NOT FOLDED INTO BondStatus. Reads are open, anonymous and
/// cheap enough to poll every few seconds; a write carries a bearer token,
/// changes how the household's traffic is routed, and has to be reported as
/// having failed when it fails. Sharing a type between them would blur the one
/// line this UI cannot afford to blur - the difference between "we asked" and
/// "the router did it".
///
/// The endpoint is `PUT /api/legs/<name>`, implemented in the bond agent's
/// `do_PUT` / `LegStore.update`. Its contract, as verified against the live
/// console on 2026-08-05:
///
///   - 200 returns `{"leg": name, "applied": {...}}` - the WHOLE stored entry
///     after the merge, which is the only read-back path there is. Nothing
///     else on the router publishes the descriptive fields.
///   - 400 for an unknown field or an unparseable body, with the reason in
///     `{"error": ...}`.
///   - 401 for a bad or missing token. Checked BEFORE the leg is looked up, so
///     a 404 can only be seen by a caller that already authenticated.
///   - 404 for an unknown leg.
public struct LegEdit: Equatable, Sendable {

    /// The fields the router will accept, and nothing else.
    ///
    /// AN ENUM RATHER THAN STRINGS because the router answers 400 to an
    /// unknown key and names it - which is the right server behaviour and a
    /// terrible thing to discover from a phone in a moving car. Making the
    /// unknown key unrepresentable moves that failure to compile time; a 400
    /// then means the app is newer than the router, which is worth saying
    /// differently.
    public enum Field: String, Sendable, CaseIterable {
        /// THE HARD GATE, not a preference. Every leg in a higher-numbered
        /// tier carries nothing at all while any leg in a lower-numbered tier
        /// is alive. This is the whole mechanism for keeping an emergency SIM
        /// in reserve.
        case tier
        /// Order WITHIN a tier. It never promotes a leg across tiers.
        case priority
        /// Share within a tier. Sets the leg's configured weight, which the
        /// policy then scales by health - see `LegEditSnapshot.configWeight`
        /// against `effectiveWeight`.
        case weight
        /// An absolute ceiling in kilobits per second, enforced in the
        /// datapath at the last point before bytes leave. NOT a low weight: a
        /// small share of a busy bond is still real volume.
        case maxKbps = "max_kbps"
        case monthlyCapGB = "monthly_cap_gb"
        case costClass = "cost_class"
        case label
        case enabled

        // Descriptive only. The agent stores these and never routes on them.
        case carrier
        case planName = "plan_name"
        case planType = "plan_type"
        case billingDay = "billing_day"
        case notes

        /// True when changing this field changes where packets go. The UI owes
        /// the reader a different warning for those.
        public var changesRouting: Bool {
            switch self {
            case .tier, .priority, .weight, .maxKbps, .monthlyCapGB,
                 .costClass, .label, .enabled:
                return true
            case .carrier, .planName, .planType, .billingDay, .notes:
                return false
            }
        }
    }

    /// One JSON leaf, in both directions. The same type decodes the router's
    /// echo, so a value that survived the round trip can be compared with what
    /// was sent rather than assumed to match.
    public enum Value: Equatable, Sendable, Codable {
        case int(Int)
        case number(Double)
        case text(String)
        case flag(Bool)
        /// JSON null, which REMOVES the override and lets zippie.toml win
        /// again. Distinct from zero: the agent reads a 0 cap as "no budget"
        /// but still keeps the override shadowing the config file.
        case cleared

        public init(from decoder: Decoder) throws {
            let c = try decoder.singleValueContainer()
            if c.decodeNil() { self = .cleared; return }
            // Bool first: JSONDecoder will happily read `true` as a Bool and
            // refuse it as an Int, but the reverse ordering has bitten enough
            // codebases to be worth stating.
            if let b = try? c.decode(Bool.self) { self = .flag(b); return }
            if let i = try? c.decode(Int.self) { self = .int(i); return }
            if let d = try? c.decode(Double.self) { self = .number(d); return }
            self = .text(try c.decode(String.self))
        }

        public func encode(to encoder: Encoder) throws {
            var c = encoder.singleValueContainer()
            switch self {
            case .int(let v):    try c.encode(v)
            case .number(let v): try c.encode(v)
            case .text(let v):   try c.encode(v)
            case .flag(let v):   try c.encode(v)
            case .cleared:       try c.encodeNil()
            }
        }

        private var numeric: Double? {
            switch self {
            case .int(let v):    return Double(v)
            case .number(let v): return v
            default:             return nil
            }
        }

        /// Same value, allowing for JSON's indifference to 15 versus 15.0.
        ///
        /// Used to check the router's echo against what was sent. Plain
        /// equality would report an unconfirmed field every time a cap was
        /// sent as 15 and echoed as 15.0, and a false "not applied" is as
        /// damaging here as a false "applied".
        public func matches(_ other: Value) -> Bool {
            if let a = numeric, let b = other.numeric { return a == b }
            return self == other
        }

        /// The value as a human reads it. No units - those belong to whichever
        /// row is drawing it.
        public var text: String {
            switch self {
            case .int(let v):    return "\(v)"
            case .number(let v): return v == v.rounded() ? "\(Int(v))" : "\(v)"
            case .text(let v):   return v
            case .flag(let v):   return v ? "on" : "off"
            case .cleared:       return "cleared"
            }
        }
    }

    public private(set) var fields: [Field: Value]

    public init(_ fields: [Field: Value] = [:]) { self.fields = fields }

    public mutating func set(_ field: Field, _ value: Value) { fields[field] = value }
    /// Remove the override so the router's own config file decides again.
    public mutating func clear(_ field: Field) { fields[field] = .cleared }

    public var isEmpty: Bool { fields.isEmpty }
    public var touchesRouting: Bool { fields.keys.contains(where: \.changesRouting) }

    func body() throws -> Data {
        var out: [String: Value] = [:]
        for (field, value) in fields { out[field.rawValue] = value }
        return try JSONEncoder().encode(out)
    }
}

/// What the router says it stored, which is the only evidence a write landed.
///
/// The app must never draw an edit as applied on the strength of having sent
/// it. This is the receipt, and `unconfirmed(_:)` is the part that matters: a
/// field the router did not echo was NOT stored, however healthy the 200 was.
public struct LegEditReceipt: Equatable, Sendable {
    public let leg: String
    /// The merged entry, keyed by the fields this app understands. Keys a
    /// newer router might add are dropped rather than surfaced - this type
    /// exists to confirm what WE sent, and inventing a row for a key the UI
    /// has no words for would be noise.
    public let applied: [LegEdit.Field: LegEdit.Value]

    public func value(_ field: LegEdit.Field) -> LegEdit.Value? { applied[field] }

    /// Fields the caller asked for that the router's echo does not back up.
    ///
    /// Empty is the only result that licenses the UI to say an edit was
    /// applied. A non-empty result means a partial write, which reads as a
    /// success from the status line and is exactly the lie this app exists to
    /// refuse.
    public func unconfirmed(_ edit: LegEdit) -> [LegEdit.Field] {
        edit.fields.compactMap { field, wanted in
            switch wanted {
            case .cleared:
                // A cleared field must be ABSENT from the entry. Still present
                // means the override survived and the leg is still overridden.
                return applied[field] == nil ? nil : field
            default:
                guard let got = applied[field], got.matches(wanted) else { return field }
                return nil
            }
        }
        .sorted { $0.rawValue < $1.rawValue }
    }
}

extension LegEditReceipt: Decodable {
    private enum CodingKeys: String, CodingKey { case leg, applied }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        leg = try c.decode(String.self, forKey: .leg)
        let raw = try c.decodeIfPresent([String: LegEdit.Value].self, forKey: .applied) ?? [:]
        var known: [LegEdit.Field: LegEdit.Value] = [:]
        for (key, value) in raw {
            if let field = LegEdit.Field(rawValue: key) { known[field] = value }
        }
        applied = known
    }
}

/// Everything the console publishes about one leg that an editor needs.
///
/// A SECOND DECODER OVER THE SAME PAYLOAD, on purpose. `BondStatus.Path` is
/// the status screen's view and does not carry `priority` or `config_weight` -
/// and an editor that cannot show the CONFIGURED weight can only offer to
/// overwrite a number it never read. Two small readers over one payload is
/// cheaper than one type serving two screens with different needs.
public struct LegEditSnapshot: Equatable, Sendable, Decodable {
    public let name: String
    public let label: String?
    public let state: String?
    public let tier: Int?
    public let priority: Int?
    /// What the operator set. This is what `LegEdit.Field.weight` writes.
    public let configWeight: Int?
    /// What the policy is using right now, after health and cost scaling. It
    /// is NOT editable and drifts from `configWeight` constantly - showing one
    /// in the other's place is how a screen ends up reporting a weight nobody
    /// ever typed.
    public let effectiveWeight: Int?
    public let maxKbps: Int?
    public let costClass: String?
    public let monthlyCapGB: Double?
    public let usageGB: Double?
    public let overSoftLimit: Bool?
    public let inBond: Bool?

    enum CodingKeys: String, CodingKey {
        case name, label, state, tier, priority
        case configWeight = "config_weight"
        case effectiveWeight = "effective_weight"
        case maxKbps = "max_kbps"
        case costClass = "cost_class"
        case monthlyCapGB = "monthly_cap_gb"
        case usageGB = "usage_gb"
        case overSoftLimit = "over_soft_limit"
        case inBond = "in_bond"
    }

    /// A cap of zero is NO CAP, not a cap of zero.
    ///
    /// The agent defaults `monthly_cap_gb` to 0.0 and reads that as "ignore
    /// the budget", so every unconfigured leg publishes 0.0. Rendering that as
    /// "0 GB" would tell the reader this leg is forbidden to carry anything,
    /// which is the opposite of what it means.
    public var capGB: Double? {
        guard let monthlyCapGB, monthlyCapGB > 0 else { return nil }
        return monthlyCapGB
    }

    /// Same rule for the ceiling: 0 means uncapped.
    public var ceilingKbps: Int? {
        guard let maxKbps, maxKbps > 0 else { return nil }
        return maxKbps
    }

    /// How much of the stated cap the MEASURED usage has taken, or nil when
    /// either half is missing. Never clamped to a floor, because the honest
    /// answer when a leg is over its cap is a number above 1.
    public var usedFraction: Double? {
        guard let capGB, let usageGB else { return nil }
        return usageGB / capGB
    }

    public var remainingGB: Double? {
        guard let capGB, let usageGB else { return nil }
        return capGB - usageGB
    }

    /// Pull one leg out of a `/api/status` body.
    ///
    /// Matched on `name`, which is the stable identifier - `label` is free
    /// text an operator can change at any moment and two legs may share one.
    public static func find(_ leg: String,
                            inStatus data: Data) -> Result<LegEditSnapshot, LegEditError> {
        struct Envelope: Decodable { let paths: [LegEditSnapshot]? }
        do {
            let envelope = try JSONDecoder().decode(Envelope.self, from: data)
            guard let found = (envelope.paths ?? []).first(where: { $0.name == leg }) else {
                return .failure(.noSuchLeg(leg))
            }
            return .success(found)
        } catch {
            return .failure(.unreadableReply(error.localizedDescription))
        }
    }
}

/// Why a write did not happen, in the terms a person can act on.
///
/// SEPARATE CASES, NOT ONE STRING. "It did not work" sends the reader to the
/// wrong place: a bad token is a copy-paste job, an unknown leg is a renamed
/// config, a refusal is a value the router would not take, and an unreachable
/// console usually means the phone is on the wrong network. Those are four
/// different next actions.
public enum LegEditError: Error, Equatable, Sendable {
    /// Nothing differed from what the router already has.
    case nothingToSend
    /// No write token stored on this phone.
    case tokenMissing
    /// The console address cannot be turned into a write URL.
    case badConsoleAddress(String)
    /// HTTP 401. The router's own words are carried through.
    case notAuthorised(String)
    /// HTTP 404 - no leg by that name.
    case noSuchLeg(String)
    /// HTTP 400 - the router would not take the value, and said why.
    case refused(String)
    /// Any other status. Named separately so it is never quietly folded into
    /// one of the cases above and mis-diagnosed.
    case unexpectedStatus(Int, String)
    /// The request never got an answer.
    case unreachable(String)
    /// An answer arrived that could not be read, so what was applied is
    /// unknown - which is worse than a clean failure and has to say so.
    case unreadableReply(String)

    public var message: String {
        switch self {
        case .nothingToSend:
            return "Nothing was changed, so nothing was sent."
        case .tokenMissing:
            return "No write token is stored on this phone. The router keeps "
                 + "one in its state directory as console_token; paste it in "
                 + "below before saving."
        case .badConsoleAddress(let address):
            return "\"\(address)\" is not an address this app can write to. "
                 + "Check the console address in the Relay tab."
        case .notAuthorised(let said):
            return "The router rejected the write token (\(said)). Copy "
                 + "console_token from the router again - a stale or "
                 + "half-selected paste looks exactly like this."
        case .noSuchLeg(let leg):
            return "The router has no leg called \"\(leg)\". It was most "
                 + "likely renamed in zippie.toml, which also resets that "
                 + "leg's history. Reopen the status screen for the new name."
        case .refused(let said):
            return "The router refused the change and applied nothing: \(said)"
        case .unexpectedStatus(let code, let said):
            return "The router answered HTTP \(code), which this app does not "
                 + "know how to read: \(said). Nothing is known to have been "
                 + "applied."
        case .unreachable(let detail):
            return "Could not reach the router's console, so nothing was "
                 + "sent: \(detail)"
        case .unreadableReply(let detail):
            return "The router answered but its reply could not be read "
                 + "(\(detail)), so what it applied is unknown. Reload before "
                 + "editing again."
        }
    }
}

/// Reads one leg and writes one leg. Both halves take the SAME injected
/// transport so a test can exercise the whole round trip - send an edit, hand
/// back a router reply, check what the app concludes - with no network at all.
public struct LegEditClient: Sendable {
    public typealias Transport = @Sendable (URLRequest) async throws -> (Data, URLResponse)

    private let transport: Transport
    private let timeout: TimeInterval

    public init(timeout: TimeInterval = 8,
                transport: @escaping Transport = { try await URLSession.shared.data(for: $0) }) {
        self.transport = transport
        self.timeout = timeout
    }

    /// The write URL for a leg, derived from the console's status URL.
    ///
    /// The leg name is escaped to a SINGLE path segment. Percent-encoding with
    /// `.urlPathAllowed` would leave a "/" intact, and a leg called "a/b" would
    /// then address a different endpoint entirely rather than 404 honestly.
    public static func writeURL(console: URL, leg: String) -> URL? {
        guard var parts = URLComponents(url: console, resolvingAgainstBaseURL: false),
              parts.host != nil, parts.scheme != nil,
              let escaped = leg.addingPercentEncoding(withAllowedCharacters: .legNameSegment),
              !escaped.isEmpty else { return nil }
        parts.percentEncodedPath = "/api/legs/" + escaped
        parts.query = nil
        parts.fragment = nil
        return parts.url
    }

    public func snapshot(of leg: String,
                         status: URL) async -> Result<LegEditSnapshot, LegEditError> {
        var request = URLRequest(url: status)
        request.timeoutInterval = timeout
        // The whole point of reloading is to see what the router has NOW; a
        // cached body would show the values the edit was supposed to change.
        request.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (data, response) = try await transport(request)
            if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                return .failure(.unexpectedStatus(http.statusCode, Self.routerMessage(data)))
            }
            return LegEditSnapshot.find(leg, inStatus: data)
        } catch {
            return .failure(.unreachable(error.localizedDescription))
        }
    }

    /// Apply an edit. The result is the router's receipt or a named failure -
    /// there is no third outcome, and in particular no "probably fine".
    public func apply(_ edit: LegEdit,
                      to leg: String,
                      console: URL,
                      token: String) async -> Result<LegEditReceipt, LegEditError> {
        guard !edit.isEmpty else { return .failure(.nothingToSend) }
        guard let token = ConsoleWriteToken.normalise(token) else {
            return .failure(.tokenMissing)
        }
        guard let url = Self.writeURL(console: console, leg: leg) else {
            return .failure(.badConsoleAddress(console.absoluteString))
        }

        var request = URLRequest(url: url)
        request.httpMethod = "PUT"
        request.timeoutInterval = timeout
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        do {
            request.httpBody = try edit.body()
        } catch {
            return .failure(.unreadableReply(error.localizedDescription))
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await transport(request)
        } catch {
            return .failure(.unreachable(error.localizedDescription))
        }

        // No status at all means the transport handed back something that is
        // not an HTTP exchange. Guessing 200 there would invent a success.
        guard let http = response as? HTTPURLResponse else {
            return .failure(.unreadableReply("the reply was not an HTTP response"))
        }

        switch http.statusCode {
        case 200:
            do {
                return .success(try JSONDecoder().decode(LegEditReceipt.self, from: data))
            } catch {
                return .failure(.unreadableReply(error.localizedDescription))
            }
        case 400: return .failure(.refused(Self.routerMessage(data)))
        case 401: return .failure(.notAuthorised(Self.routerMessage(data)))
        case 404: return .failure(.noSuchLeg(leg))
        default:  return .failure(.unexpectedStatus(http.statusCode, Self.routerMessage(data)))
        }
    }

    /// The router puts its reason in `{"error": ...}`. Falling back to the raw
    /// body keeps a proxy's HTML error page readable rather than swallowing it
    /// into a blank message.
    private static func routerMessage(_ data: Data) -> String {
        struct Failure: Decodable { let error: String? }
        if let decoded = try? JSONDecoder().decode(Failure.self, from: data),
           let error = decoded.error, !error.isEmpty {
            return error
        }
        let raw = String(decoding: data.prefix(200), as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return raw.isEmpty ? "no reason given" : raw
    }
}

private extension CharacterSet {
    /// One path segment, conservatively. RFC 3986 unreserved characters only:
    /// anything else is escaped rather than trusted, because the set of
    /// characters a leg name may contain is whatever someone typed in a TOML
    /// file.
    static let legNameSegment: CharacterSet = {
        var set = CharacterSet.alphanumerics
        set.insert(charactersIn: "-._~")
        return set
    }()
}

/// The console's write token, kept in the keychain.
///
/// NOT UserDefaults, which is where every other setting in this app lives.
/// The rest are preferences - a hostname, a port - and this one is the single
/// credential that lets a device re-route the household's traffic. UserDefaults
/// is a plist in the container and rides along in an unencrypted backup; the
/// keychain does not.
public struct ConsoleWriteToken: Sendable {
    /// Default service. Shared by every install of this app; the account below
    /// separates it from anything else the bundle might store later.
    public static let shared = ConsoleWriteToken(service: "app.zippie.companion.console")

    private let service: String
    private let account = "console-write-token"

    public init(service: String) { self.service = service }

    /// A pasted token, cleaned up, or nil when there is nothing usable.
    ///
    /// PURE, AND THE PART WORTH TESTING. `cat console_token` yields a trailing
    /// newline and a long-press paste often carries surrounding whitespace;
    /// both produce a 401 that reads as "wrong token" and sends the reader
    /// back to the router to copy the same string again. Stripping a pasted
    /// "Bearer " prefix costs nothing and saves the same wasted trip.
    public static func normalise(_ raw: String) -> String? {
        var value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if value.lowercased().hasPrefix("bearer ") {
            value = String(value.dropFirst("bearer ".count))
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return value.isEmpty ? nil : value
    }

    public func read() -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data,
              let value = String(data: data, encoding: .utf8) else { return nil }
        return Self.normalise(value)
    }

    /// Stores the token, or reports that it could not. The boolean is not
    /// decoration: a keychain write can fail on a device with no entitlement
    /// and a UI that assumed success would show a saved token that is not
    /// there, then blame the router for the 401 that follows.
    @discardableResult
    public func save(_ raw: String) -> Bool {
        guard let token = Self.normalise(raw), let data = token.data(using: .utf8) else {
            return false
        }
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        // AfterFirstUnlock rather than WhenUnlocked: the editor is a foreground
        // screen today, but a token that vanishes behind a locked screen is a
        // failure that only shows up on a phone in a pocket.
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]
        let updated = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if updated == errSecSuccess { return true }
        guard updated == errSecItemNotFound else { return false }
        return SecItemAdd(query.merging(attributes) { $1 } as CFDictionary, nil) == errSecSuccess
    }

    @discardableResult
    public func remove() -> Bool {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        return status == errSecSuccess || status == errSecItemNotFound
    }
}
