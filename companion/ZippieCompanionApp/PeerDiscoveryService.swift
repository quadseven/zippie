import Foundation
import Network
import ZippieCompanionKit

#if canImport(Zippie)
import Zippie
#endif

/// Same-LAN discovery of other paired zippie devices (#270): advertise
/// `_zippie._tcp`, browse for it, challenge every device found, and offer the
/// ones that prove a pairing as legs.
///
/// WHAT IS TRUSTED, AND BY WHAT. A Bonjour result is a claim from anything on
/// the wifi. It becomes a `VerifiedPeer` only when the Go binding has checked
/// its proof against the roster (`GoPeerCrypto`), and the leg it offers dials
/// the endpoint inside that proof, never the address Bonjour resolved.
///
/// ADOPTION IS A LEASE, like every announced leg. Adding a peer starts a
/// delegated `LegAnnouncer` for it, renewed only while the peer is still
/// advertised and verified. When it leaves the wifi the announcer withdraws
/// it; when it comes back verified, renewing resumes without another tap.
@MainActor
final class PeerDiscoveryService: ObservableObject {
    enum Status: Equatable {
        /// No pairing bundle on this phone: nothing to prove itself with and
        /// no roster to verify others against.
        case unpaired
        case running
        case failed(String)
    }

    @Published private(set) var status: Status = .unpaired
    @Published private(set) var roster = PeerRoster()
    /// Client ids the operator chose to add. Kept while a peer comes and goes,
    /// so a phone that drops off the wifi for a minute rejoins on its own.
    @Published private(set) var adopted: Set<UInt32> = []
    @Published private(set) var lastOutcome: [UInt32: LegAnnouncer.Outcome] = [:]
    /// Why adopted peers are not being announced, when something blocks all
    /// of them at once.
    @Published private(set) var adoptionBlocker: String?

    /// The pairing bundle's keychain item: same keychain code as the console
    /// token, its own item.
    nonisolated static let pairingStore = ConsoleWriteToken(service: "app.zippie.companion.peers",
                                                            account: "pairing-bundle")

    private struct Adoption {
        let peer: VerifiedPeer
        let announcer: LegAnnouncer
        let config: LegAnnouncer.Config
    }

    private let crypto: PeerCrypto
    private let queue = DispatchQueue(label: "app.zippie.companion.peers")
    private let answering = PeerWire.Gate(limit: PeerWire.maxAnswering)
    private var pairing: PeerPairing?
    private var listener: NWListener?
    private var browser: NWBrowser?
    private var running: [UInt32: Adoption] = [:]

    init(crypto: PeerCrypto = GoPeerCrypto()) {
        self.crypto = crypto
    }

    var ownClientID: UInt32? { pairing?.clientID }

    // MARK: - lifecycle

    func start() {
        guard listener == nil, browser == nil else { return }
        guard let raw = Self.pairingStore.read(), let pairing = PeerPairing(bundleJSON: raw) else {
            status = .unpaired
            return
        }
        self.pairing = pairing
        do {
            listener = try advertise(pairing)
        } catch {
            fail("advertising: \(error)")
            return
        }
        browser = browse(pairing)
        status = .running
    }

    func stop() {
        listener?.cancel()
        listener = nil
        browser?.cancel()
        browser = nil
        roster = PeerRoster()
        reconcile()
    }

    /// Brings discovery back after it stopped on a failure. iOS can reclaim a
    /// suspended app's listening socket, which surfaces as the listener or
    /// browser failing; without this the app would stay deaf to peers - and
    /// stop renewing adopted legs - until it was relaunched.
    func resumeIfStopped() {
        guard case .failed = status else { return }
        restart()
    }

    /// Re-reads the pairing bundle, for after the operator stores or forgets
    /// one on the diagnostics screen.
    func restart() {
        stop()
        pairing = nil
        start()
    }

    private func fail(_ reason: String) {
        stop()
        status = .failed(reason)
        Observability.peerDiscovery("failed", detail: reason)
    }

    // MARK: - adopting a peer as a leg

    func adopt(_ peer: VerifiedPeer) {
        adopted.insert(peer.clientID)
        reconcile()
    }

    func release(_ peer: VerifiedPeer) {
        adopted.remove(peer.clientID)
        lastOutcome[peer.clientID] = nil
        reconcile()
    }

    /// One announcer per adopted peer that is verified RIGHT NOW, and none for
    /// the rest. Called after every change to the roster or the adopted set.
    private func reconcile() {
        var desired: [UInt32: VerifiedPeer] = [:]
        for peer in roster.verifiedPeers where adopted.contains(peer.clientID) {
            desired[peer.clientID] = peer
        }
        stopStale(keeping: desired)
        startMissing(desired)
    }

    private func stopStale(keeping desired: [UInt32: VerifiedPeer]) {
        for (id, adoption) in running where desired[id] != adoption.peer {
            running[id] = nil
            Task { await adoption.announcer.stop(adoption.config) }
        }
    }

    private func startMissing(_ desired: [UInt32: VerifiedPeer]) {
        let pending = desired.filter { running[$0.key] == nil }
        guard !pending.isEmpty else { return }
        let console = Settings.consoleLANHost.trimmingCharacters(in: .whitespaces)
        guard let token = ConsoleWriteToken.shared.read(), !console.isEmpty else {
            adoptionBlocker = "Store the router's write token on Diagnostics to add a device as a leg."
            return
        }
        adoptionBlocker = nil
        for (id, peer) in pending {
            let config = peer.announcerConfig(consoleHost: console, token: token)
            let announcer = LegAnnouncer()
            running[id] = Adoption(peer: peer, announcer: announcer, config: config)
            let host = peer.host
            let report: @Sendable (LegAnnouncer.Outcome) -> Void = { [weak self] outcome in
                Task { @MainActor in self?.lastOutcome[id] = outcome }
            }
            Task { await announcer.start(config, address: { host }, report: report) }
        }
    }

    // MARK: - advertising this phone

    private func advertise(_ pairing: PeerPairing) throws -> NWListener {
        let listener = try NWListener(using: .tcp)
        listener.service = NWListener.Service(
            type: PeerDiscovery.serviceType,
            txtRecord: NWTXTRecord(PeerDiscovery.txtRecord(clientID: pairing.clientID)))
        listener.newConnectionHandler = { [crypto, queue, answering] connection in
            // Anything on the wifi can connect; a household needs a handful.
            guard answering.enter() else { return connection.cancel() }
            PeerWire.answer(connection, pairing: pairing, endpoint: PeerWire.relayEndpoint(),
                            crypto: crypto, queue: queue, done: answering.leave)
        }
        listener.stateUpdateHandler = { [weak self] state in
            guard case .failed(let error) = state else { return }
            Task { @MainActor in self?.fail("advertising: \(error)") }
        }
        listener.start(queue: queue)
        return listener
    }

    // MARK: - finding and challenging others

    private func browse(_ pairing: PeerPairing) -> NWBrowser {
        let browser = NWBrowser(
            for: .bonjourWithTXTRecord(type: PeerDiscovery.serviceType, domain: nil),
            using: .tcp)
        browser.browseResultsChangedHandler = { [weak self] _, changes in
            Task { @MainActor in self?.apply(changes) }
        }
        browser.stateUpdateHandler = { [weak self] state in
            guard case .failed(let error) = state else { return }
            Task { @MainActor in self?.fail("browsing: \(error)") }
        }
        browser.start(queue: queue)
        return browser
    }

    private func apply(_ changes: Set<NWBrowser.Result.Change>) {
        for change in changes {
            switch change {
            case .added(let result):
                found(result)
            case .removed(let result):
                roster.lost(service: Self.key(result))
            case .changed(old: let old, new: let new, flags: _):
                roster.lost(service: Self.key(old))
                found(new)
            case .identical:
                continue
            @unknown default:
                continue
            }
        }
        reconcile()
    }

    private func found(_ result: NWBrowser.Result) {
        guard let pairing else { return }
        let service = Self.key(result)
        guard case .bonjour(let txt) = result.metadata,
              let claimed = PeerDiscovery.claimedClientID(in: txt.dictionary) else {
            roster.found(service: service)
            settle(service, .refused("no client id advertised"))
            return
        }
        // This phone's own advertisement comes back to its own browser.
        guard claimed != pairing.clientID else { return }
        roster.found(service: service)
        let endpoint = result.endpoint
        let queue = self.queue
        Task { [crypto] in
            let standing = await PeerVerification.verify(claimedID: claimed, pairing: pairing,
                                                         crypto: crypto) { message in
                try await PeerWire.exchange(message, with: endpoint, queue: queue)
            }
            self.settle(service, standing)
        }
    }

    private func settle(_ service: String, _ standing: PeerStanding) {
        roster.record(service: service, standing)
        if case .refused(let reason) = standing {
            Observability.peerDiscovery("refused", detail: reason)
        }
        reconcile()
    }

    private static func key(_ result: NWBrowser.Result) -> String {
        "\(result.endpoint)"
    }
}

/// The TCP half of the handshake: one message each way, then close.
enum PeerWire {
    /// On the same wifi a reply takes milliseconds. A device that has not
    /// answered in this long is refused rather than left "checking" forever.
    static let timeout: TimeInterval = 4

    enum Failure: Error, CustomStringConvertible {
        case noLocalAddress, timedOut, tooLong

        var description: String {
            switch self {
            case .noLocalAddress: return "this phone has no address on the local network"
            case .timedOut: return "the device did not answer in time"
            case .tooLong: return "the device sent more than a handshake message"
            }
        }
    }

    /// The endpoint this phone signs: its wifi address and the relay's listen
    /// port, which is what the router dials for any phone leg.
    static func relayEndpoint() -> String? {
        guard let address = LocalAddress.localIPv4() else { return nil }
        return "\(address):\(Settings.listenPort)"
    }

    /// How many challenges this phone answers at once.
    static let maxAnswering = 8

    /// Counts connections being answered. Touched only on the discovery
    /// queue, where the listener and every connection call back.
    final class Gate: @unchecked Sendable {
        private let limit: Int
        private var active = 0

        init(limit: Int) { self.limit = limit }

        func enter() -> Bool {
            guard active < limit else { return false }
            active += 1
            return true
        }

        func leave() { active -= 1 }
    }

    /// This phone's side: read a challenge, answer it, close. Bounded by
    /// `timeout` like the verifier's side, so a peer that connects and never
    /// sends cannot hold the connection open. `done` runs once, when the
    /// connection is cancelled for any reason.
    static func answer(_ connection: NWConnection, pairing: PeerPairing, endpoint: String?,
                       crypto: PeerCrypto, queue: DispatchQueue, done: @escaping () -> Void) {
        connection.stateUpdateHandler = { state in
            switch state {
            case .cancelled: done()
            case .failed: connection.cancel()
            default: break
            }
        }
        queue.asyncAfter(deadline: .now() + timeout) { connection.cancel() }
        connection.start(queue: queue)
        readMessage(connection) { result in
            do {
                guard let endpoint else { throw Failure.noLocalAddress }
                let reply = try PeerVerification.answer(try result.get(), pairing: pairing,
                                                        endpoint: endpoint, crypto: crypto)
                connection.send(content: reply, completion: .contentProcessed { _ in
                    connection.cancel()
                })
            } catch {
                Observability.peerDiscovery("unanswered", detail: String(describing: error))
                connection.cancel()
            }
        }
    }

    /// The verifier's side: send one message to a discovered service and
    /// return its reply.
    static func exchange(_ message: Data, with endpoint: NWEndpoint,
                         queue: DispatchQueue) async throws -> Data {
        let connection = NWConnection(to: endpoint, using: .tcp)
        defer { connection.cancel() }
        return try await withCheckedThrowingContinuation { continuation in
            let once = Once(continuation)
            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    connection.send(content: message, completion: .contentProcessed { error in
                        if let error { return once.finish(.failure(error)) }
                        readMessage(connection) { once.finish($0) }
                    })
                case .failed(let error), .waiting(let error):
                    // WAITING is a failure here, not a pause: it means there is
                    // no path to the device, and the timeout would only delay
                    // saying so.
                    once.finish(.failure(error))
                default:
                    break
                }
            }
            queue.asyncAfter(deadline: .now() + timeout) { once.finish(.failure(Failure.timedOut)) }
            connection.start(queue: queue)
        }
    }

    /// Reads until a newline, the far end closing, or the size cap.
    static func readMessage(_ connection: NWConnection, buffer: Data = Data(),
                            completion: @escaping (Result<Data, Error>) -> Void) {
        connection.receive(minimumIncompleteLength: 1,
                           maximumLength: PeerHandshake.maxMessageBytes) { data, _, isComplete, error in
            var buffer = buffer
            if let data { buffer.append(data) }
            if let error { return completion(.failure(error)) }
            if buffer.count > PeerHandshake.maxMessageBytes { return completion(.failure(Failure.tooLong)) }
            if buffer.contains(UInt8(ascii: "\n")) || isComplete { return completion(.success(buffer)) }
            readMessage(connection, buffer: buffer, completion: completion)
        }
    }

    /// Resumes a continuation exactly once: the timeout and the connection race.
    private final class Once: @unchecked Sendable {
        private let lock = NSLock()
        private var continuation: CheckedContinuation<Data, Error>?

        init(_ continuation: CheckedContinuation<Data, Error>) {
            self.continuation = continuation
        }

        func finish(_ result: Result<Data, Error>) {
            lock.lock()
            let pending = continuation
            continuation = nil
            lock.unlock()
            pending?.resume(with: result)
        }
    }
}

/// `PeerCrypto` backed by the gomobile binding, so every trust decision is the
/// Go `ClientRegistry`'s.
///
/// A BUILD WITHOUT THE FRAMEWORK VERIFIES NOTHING. Every call throws, every
/// discovered device is refused, and nothing is offered - the fail-closed
/// answer, rather than a stand-in that trusted the advertisement.
struct GoPeerCrypto: PeerCrypto {
    struct NotBundled: Error, CustomStringConvertible {
        var description: String { "this build has no datapath framework, so no device can be verified" }
    }

    func newChallenge() throws -> String {
        #if canImport(Zippie)
        var error: NSError?
        let challenge = MobileNewPeerChallenge(&error)
        if let error { throw error }
        return challenge
        #else
        throw NotBundled()
        #endif
    }

    func proof(credentialJSON: String, challengeHex: String, endpoint: String) throws -> String {
        #if canImport(Zippie)
        var error: NSError?
        let proof = MobilePeerProof(credentialJSON, challengeHex, endpoint, &error)
        if let error { throw error }
        return proof
        #else
        throw NotBundled()
        #endif
    }

    func verify(rosterJSON: String, claimedID: Int, challengeHex: String,
                proofHex: String) throws -> String {
        #if canImport(Zippie)
        var error: NSError?
        let verified = MobileVerifyPeer(rosterJSON, claimedID, challengeHex, proofHex, &error)
        if let error { throw error }
        return verified
        #else
        throw NotBundled()
        #endif
    }
}
