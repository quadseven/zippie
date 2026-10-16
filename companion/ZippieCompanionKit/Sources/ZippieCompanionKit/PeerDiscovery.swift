import Foundation

/// Same-LAN discovery of other paired zippie devices (#270).
///
/// A phone advertises `_zippie._tcp` and browses for the same. What it finds is
/// an UNAUTHENTICATED CLAIM from anything on the wifi: a stranger on hotel
/// wifi can advertise any name and any client id. So a discovered device is
/// never named in the UI, never offered as a leg, until it has answered a
/// fresh challenge with the pairing credential behind the id it claims.
///
/// THE TRUST DECISION IS MADE IN GO, not here. `PeerCrypto` is a seam for the
/// gomobile binding (`mobile/discovery.go`), which verifies with the same
/// `ClientRegistry` code that authenticates every datapath frame. This file is
/// the plumbing around it - message shapes, the pairing material, and which
/// verified peers are worth offering - kept here so it can be tested without a
/// device.
public enum PeerDiscovery {
    /// The Bonjour service type. NSBonjourServices in the app's Info.plist must
    /// list this exact string, or iOS refuses to browse it and says nothing.
    public static let serviceType = "_zippie._tcp"

    /// The TXT key carrying the CLAIMED client id. A lookup hint for the
    /// verifier and nothing more.
    public static let clientIDKey = "id"

    public static func txtRecord(clientID: UInt32) -> [String: String] {
        [clientIDKey: String(clientID)]
    }

    /// The claimed id, or nil when there is no usable claim. Zero is refused
    /// because it cannot be told apart from an unset field.
    public static func claimedClientID(in txt: [String: String]) -> UInt32? {
        guard let raw = txt[clientIDKey], let id = UInt32(raw), id > 0 else { return nil }
        return id
    }
}

/// This phone's pairing material: its own credential, which it proves itself
/// with, and the roster of paired devices, which it verifies others against.
///
/// One JSON bundle - `{"client_id", "key_hex", "roster": [{"id", "name",
/// "key_hex"}]}` - because that is the shape the pairing ceremony (#31) will
/// hand a device, and until it exists the operator provisions it by hand.
/// `key_hex` is this device's own pairing key. The roster is NOT home's
/// clients.json: it is what `zippie-clienthome -discovery-roster` prints from
/// it, every key replaced by a one-way discovery key, so a phone's bundle can
/// verify peers and cannot speak the datapath as any of them.
public struct PeerPairing: Sendable, Equatable {
    public let clientID: UInt32
    public let keyHex: String
    /// The roster as JSON, passed to Go untouched.
    public let rosterJSON: String

    /// Nil rather than a partial pairing: a phone that advertised with no
    /// usable key would invite challenges it can never answer.
    public init?(bundleJSON: String) {
        guard let object = try? JSONSerialization.jsonObject(with: Data(bundleJSON.utf8)),
              let bundle = object as? [String: Any],
              let clientID = (bundle["client_id"] as? NSNumber)?.uint32Value, clientID > 0,
              let keyHex = bundle["key_hex"] as? String,
              keyHex.count >= 32, keyHex.allSatisfy(\.isHexDigit),
              let roster = bundle["roster"] as? [Any],
              let rosterData = try? JSONSerialization.data(withJSONObject: roster),
              let rosterJSON = String(data: rosterData, encoding: .utf8) else { return nil }
        self.clientID = clientID
        self.keyHex = keyHex
        self.rosterJSON = rosterJSON
    }

    /// The credential in the binding's own field names - the same ones
    /// `ClientConfig.datapathJSON` uses.
    public var credentialJSON: String {
        let object: [String: Any] = ["client_id": clientID, "key_hex": keyHex]
        guard let data = try? JSONSerialization.data(withJSONObject: object),
              let json = String(data: data, encoding: .utf8) else { return "{}" }
        return json
    }
}

/// The two messages of the handshake, one JSON object per line: the verifier
/// sends `{"challenge": hex}`, the device answers `{"proof": hex}`.
public enum PeerHandshake {
    /// Bounds what either side reads. A proof is a 29-byte header, a sealed
    /// payload of challenge plus endpoint, and hex doubles it; 4 KiB is ample
    /// and stops a hostile peer streaming forever into memory.
    public static let maxMessageBytes = 4096

    public static func challengeMessage(_ hex: String) -> Data { message("challenge", hex) }
    public static func proofMessage(_ hex: String) -> Data { message("proof", hex) }

    public static func challenge(in data: Data) -> String? { field("challenge", in: data) }
    public static func proof(in data: Data) -> String? { field("proof", in: data) }

    private static func message(_ key: String, _ value: String) -> Data {
        guard let data = try? JSONSerialization.data(withJSONObject: [key: value]) else { return Data() }
        return data + Data("\n".utf8)
    }

    private static func field(_ key: String, in data: Data) -> String? {
        guard data.count <= maxMessageBytes,
              let object = try? JSONSerialization.jsonObject(with: data),
              let value = (object as? [String: Any])?[key] as? String,
              !value.isEmpty else { return nil }
        return value
    }
}

/// The gomobile binding's three discovery calls. A protocol so the handshake
/// can be tested with a stand-in; the real one is `mobile/discovery.go`.
public protocol PeerCrypto: Sendable {
    func newChallenge() throws -> String
    func proof(credentialJSON: String, challengeHex: String, endpoint: String) throws -> String
    func verify(rosterJSON: String, claimedID: Int, challengeHex: String,
                proofHex: String) throws -> String
}

/// A device that has PROVED it holds a paired credential. Built only from
/// the binding's verification result, so its name is the roster's and its
/// endpoint is the one it signed.
public struct VerifiedPeer: Sendable, Equatable {
    public let clientID: UInt32
    public let name: String
    public let host: String
    public let port: UInt16

    public init(clientID: UInt32, name: String, host: String, port: UInt16) {
        self.clientID = clientID
        self.name = name
        self.host = host
        self.port = port
    }

    /// Parses `{"client_id", "name", "endpoint": "host:port"}` from VerifyPeer.
    public init?(verifiedJSON: String) {
        guard let object = try? JSONSerialization.jsonObject(with: Data(verifiedJSON.utf8)),
              let fields = object as? [String: Any],
              let clientID = (fields["client_id"] as? NSNumber)?.uint32Value, clientID > 0,
              let endpoint = fields["endpoint"] as? String,
              let colon = endpoint.lastIndex(of: ":"),
              let port = UInt16(endpoint[endpoint.index(after: colon)...]), port > 0 else { return nil }
        let host = String(endpoint[..<colon])
        guard !host.isEmpty else { return nil }
        self.init(clientID: clientID, name: fields["name"] as? String ?? "", host: host, port: port)
    }

    public var endpoint: String { "\(host):\(port)" }

    /// The router's key for this leg. The roster name is written by a person,
    /// so it is reduced to the router's slug rule; one that reduces to nothing
    /// usable falls back to the client id, which is unique by construction.
    public var legName: String {
        if let slug = LegName.sanitise(name) {
            var trimmed = String(slug.prefix(LegName.maxLength))
            while trimmed.hasSuffix("-") { trimmed.removeLast() }
            if LegName.isValid(trimmed) { return trimmed }
        }
        return "peer-\(clientID)"
    }

    /// The announcement that makes this peer a leg. DELEGATED, because this
    /// phone is not the leg and the router would otherwise dial it (#252).
    public func announcerConfig(consoleHost: String, token: String) -> LegAnnouncer.Config {
        LegAnnouncer.Config(consoleHost: consoleHost, token: token, name: legName,
                            label: name.isEmpty ? legName : name, listenPort: port,
                            delegated: true)
    }
}

/// Where one discovered service stands.
public enum PeerStanding: Sendable, Equatable {
    case checking
    case verified(VerifiedPeer)
    /// Why, in words, so the refusal is visible rather than a peer that
    /// silently never appears.
    case refused(String)
}

public enum PeerHandshakeError: Error, Equatable {
    case malformedChallenge
}

/// Both halves of the handshake, around the binding.
public enum PeerVerification {
    /// The device's side: answer a challenge with a proof naming `endpoint`,
    /// the "host:port" the router should dial for this phone's relay.
    public static func answer(_ request: Data, pairing: PeerPairing, endpoint: String,
                              crypto: PeerCrypto) throws -> Data {
        guard let challenge = PeerHandshake.challenge(in: request) else {
            throw PeerHandshakeError.malformedChallenge
        }
        let proof = try crypto.proof(credentialJSON: pairing.credentialJSON,
                                     challengeHex: challenge, endpoint: endpoint)
        return PeerHandshake.proofMessage(proof)
    }

    /// The verifier's side: challenge a discovered device through `exchange`
    /// (send one message, return its reply) and let the binding decide.
    ///
    /// Every failure is a refusal with its reason. There is no path that ends
    /// in `.verified` without the binding having said so.
    public static func verify(claimedID: UInt32, pairing: PeerPairing, crypto: PeerCrypto,
                              exchange: @Sendable (Data) async throws -> Data) async -> PeerStanding {
        do {
            let challenge = try crypto.newChallenge()
            let reply = try await exchange(PeerHandshake.challengeMessage(challenge))
            guard let proof = PeerHandshake.proof(in: reply) else {
                return .refused("the device did not answer with a proof")
            }
            let result = try crypto.verify(rosterJSON: pairing.rosterJSON, claimedID: Int(claimedID),
                                           challengeHex: challenge, proofHex: proof)
            guard let peer = VerifiedPeer(verifiedJSON: result) else {
                return .refused("the verification result was unreadable")
            }
            return .verified(peer)
        } catch {
            return .refused(String(describing: error))
        }
    }
}

/// Every service the browser has reported, and where each stands.
public struct PeerRoster: Sendable, Equatable {
    public private(set) var standings: [String: PeerStanding] = [:]

    public init() {}

    public mutating func found(service: String) {
        if standings[service] == nil { standings[service] = .checking }
    }

    /// Ignored for a service already lost: a verification that finishes after
    /// the device left must not bring it back.
    public mutating func record(service: String, _ standing: PeerStanding) {
        guard standings[service] != nil else { return }
        standings[service] = standing
    }

    public mutating func lost(service: String) {
        standings[service] = nil
    }

    /// How many devices on this wifi failed to prove a pairing. Counted, never
    /// named: a refused device's name is its own unproven claim.
    public var refusedCount: Int {
        standings.values.filter { if case .refused = $0 { return true } else { return false } }.count
    }

    /// Every peer currently verified, each device once however many services
    /// it advertises. An adopted peer stays here after it stops being offered;
    /// this is what says its lease is still worth renewing.
    public var verifiedPeers: [VerifiedPeer] {
        var seen: Set<UInt32> = []
        return standings.values
            .compactMap { standing -> VerifiedPeer? in
                if case .verified(let peer) = standing { return peer }
                return nil
            }
            .sorted { ($0.name, $0.clientID) < ($1.name, $1.clientID) }
            .filter { seen.insert($0.clientID).inserted }
    }

    /// The verified peers worth offering as legs: not this phone, and not
    /// already a leg by name or by endpoint.
    public func offers(ownClientID: UInt32, legNames: Set<String>,
                       legEndpoints: Set<String>) -> [VerifiedPeer] {
        verifiedPeers
            .filter { $0.clientID != ownClientID }
            .filter { !legNames.contains($0.legName) && !legEndpoints.contains($0.endpoint) }
    }
}
