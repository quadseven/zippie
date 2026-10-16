package app.zippie.companion

import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.InputStream

/**
 * Same-LAN discovery of other paired zippie devices (#270). A port of
 * PeerDiscovery.swift, including the reasoning:
 *
 * A phone registers `_zippie._tcp` with NSD and discovers the same. What it
 * finds is an UNAUTHENTICATED CLAIM from anything on the wifi: a stranger on
 * hotel wifi can advertise any name and any client id. So a discovered device
 * is never named in the UI, never offered as a leg, until it has answered a
 * fresh challenge with the pairing credential behind the id it claims.
 *
 * THE TRUST DECISION IS MADE IN GO, not here. [PeerCrypto] is a seam for the
 * gomobile binding (`mobile/discovery.go`), which verifies with the same
 * `ClientRegistry` that authenticates every datapath frame. This file is the
 * plumbing around it, kept free of Android types so the JVM tests reach it.
 */
object PeerDiscovery {
    /** Must match the iOS app's PeerDiscovery.serviceType, or neither platform
     *  sees the other. */
    const val SERVICE_TYPE = "_zippie._tcp"

    /** The TXT key carrying the CLAIMED client id - a lookup hint, nothing more. */
    const val CLIENT_ID_KEY = "id"

    private const val MAX_CLIENT_ID = 0xFFFF_FFFFL

    fun txtRecord(clientId: Long): Map<String, String> = mapOf(CLIENT_ID_KEY to clientId.toString())

    /** The claimed id from NSD's attribute map, or null when there is no usable
     *  claim. Zero is refused: it cannot be told apart from an unset field. */
    fun claimedClientId(attributes: Map<String, ByteArray?>): Long? {
        val raw = attributes[CLIENT_ID_KEY]?.toString(Charsets.UTF_8) ?: return null
        val id = raw.toLongOrNull() ?: return null
        return id.takeIf { it in 1..MAX_CLIENT_ID }
    }
}

/**
 * This phone's pairing material: its own credential, which it proves itself
 * with, and the roster of paired devices, which it verifies others against.
 * The bundle is `{"client_id", "key_hex", "roster": [{"id", "name",
 * "key_hex"}]}`, the shape the pairing ceremony (#31) will hand a device; until
 * then MDM delivers it (ManagedConfig.KEY_PEER_PAIRING). `key_hex` is this
 * device's own pairing key. The roster is NOT home's clients.json: it is what
 * `zippie-clienthome -discovery-roster` prints from it, every key replaced by a
 * one-way discovery key, so the bundle can verify peers and cannot speak the
 * datapath as any of them.
 */
data class PeerPairing(val clientId: Long, val keyHex: String, val rosterJson: String) {

    /** The credential in the binding's own field names. */
    val credentialJson: String
        get() = JSONObject().put("client_id", clientId).put("key_hex", keyHex).toString()

    /** REDACTED: a data class prints every field, and this one holds keys. */
    override fun toString(): String = "PeerPairing(clientId=$clientId, key=<redacted>)"

    companion object {
        /** Null rather than a partial pairing: a phone that advertised with no
         *  usable key would invite challenges it can never answer. */
        fun parse(bundleJson: String?): PeerPairing? {
            if (bundleJson.isNullOrBlank()) return null
            return try {
                val o = JSONObject(bundleJson)
                val id = o.optLong("client_id", 0)
                val key = o.optString("key_hex")
                val roster: JSONArray = o.getJSONArray("roster")
                val keyOk = key.length >= 32 && key.all { it.isDigit() || it.lowercaseChar() in 'a'..'f' }
                if (id in 1..0xFFFF_FFFFL && keyOk) PeerPairing(id, key, roster.toString()) else null
            } catch (e: JSONException) {
                null
            }
        }
    }
}

/** The two messages of the handshake, one JSON object per line. */
object PeerHandshake {
    /** Bounds what either side reads, so a hostile peer cannot stream forever
     *  into memory. A real proof is a few hundred bytes. */
    const val MAX_MESSAGE_BYTES = 4096

    fun challengeMessage(hex: String): ByteArray = message("challenge", hex)
    fun proofMessage(hex: String): ByteArray = message("proof", hex)

    fun challengeIn(data: ByteArray): String? = field("challenge", data)
    fun proofIn(data: ByteArray): String? = field("proof", data)

    /** The whole message, however it arrives, within this long. */
    const val DEADLINE_MS = 4_000L

    /**
     * Reads one message: up to a newline or the far end closing, never past
     * [MAX_MESSAGE_BYTES], and never past [deadlineMs] in total. The caller's
     * socket read timeout bounds one read; this bounds the message, so a peer
     * trickling a byte just inside the read timeout cannot hold the
     * connection for hours.
     */
    fun read(
        input: InputStream,
        deadlineMs: Long = DEADLINE_MS,
        clock: () -> Long = System::currentTimeMillis,
    ): ByteArray {
        val giveUpAt = clock() + deadlineMs
        val out = ByteArrayOutputStream()
        while (out.size() <= MAX_MESSAGE_BYTES) {
            val b = input.read()
            if (b < 0 || b == '\n'.code) return out.toByteArray()
            if (clock() > giveUpAt) throw PeerHandshakeException("the peer took too long to send its message")
            out.write(b)
        }
        throw PeerHandshakeException("the peer sent more than a handshake message")
    }

    private fun message(key: String, value: String): ByteArray =
        (JSONObject().put(key, value).toString() + "\n").toByteArray()

    private fun field(key: String, data: ByteArray): String? {
        if (data.size > MAX_MESSAGE_BYTES) return null
        return try {
            JSONObject(data.toString(Charsets.UTF_8)).optString(key).ifEmpty { null }
        } catch (e: JSONException) {
            null
        }
    }
}

class PeerHandshakeException(message: String) : Exception(message)

/** The gomobile binding's three discovery calls; the real one is GoPeerCrypto. */
interface PeerCrypto {
    fun newChallenge(): String
    fun proof(credentialJson: String, challengeHex: String, endpoint: String): String
    fun verify(rosterJson: String, claimedId: Long, challengeHex: String, proofHex: String): String
}

/**
 * A device that has PROVED it holds a paired credential, built only from the
 * binding's result: its name is the roster's and its endpoint is the one it
 * signed.
 */
data class VerifiedPeer(val clientId: Long, val name: String, val host: String, val port: Int) {
    val endpoint: String get() = "$host:$port"

    /** The router's key for this leg, reduced to its slug rule; a name that
     *  reduces to nothing usable falls back to the unique client id. */
    val legName: String
        get() {
            val slug = LegName.sanitise(name)?.take(LegName.MAX_LENGTH)?.trimEnd('-')
            return slug?.takeIf(LegName::isValid) ?: "peer-$clientId"
        }

    /** DELEGATED, because this phone is not the leg and the router would
     *  otherwise dial it (#252). */
    fun announcerConfig(consoleHost: String, token: String) = LegAnnouncer.Config(
        consoleHost = consoleHost,
        token = token,
        name = legName,
        label = name.ifEmpty { legName },
        listenPort = port,
        delegated = true,
    )

    companion object {
        /** Parses `{"client_id", "name", "endpoint": "host:port"}` from VerifyPeer. */
        fun parse(json: String): VerifiedPeer? = try {
            val o = JSONObject(json)
            val id = o.optLong("client_id", 0)
            val endpoint = o.optString("endpoint")
            val colon = endpoint.lastIndexOf(':')
            val port = if (colon > 0) endpoint.substring(colon + 1).toIntOrNull() else null
            if (id > 0 && port != null && port in 1..65535) {
                VerifiedPeer(id, o.optString("name"), endpoint.substring(0, colon), port)
            } else {
                null
            }
        } catch (e: JSONException) {
            null
        }
    }
}

/** Where one discovered service stands. */
sealed class PeerStanding {
    object Checking : PeerStanding()
    data class Verified(val peer: VerifiedPeer) : PeerStanding()

    /** Why, in words, so a refusal is visible rather than a peer that silently
     *  never appears. */
    data class Refused(val reason: String) : PeerStanding()
}

/** Both halves of the handshake, around the binding. */
object PeerVerification {
    /** This phone's side: answer a challenge with a proof naming [endpoint]. */
    fun answer(request: ByteArray, pairing: PeerPairing, endpoint: String, crypto: PeerCrypto): ByteArray {
        val challenge = PeerHandshake.challengeIn(request)
            ?: throw PeerHandshakeException("the request was not a challenge")
        return PeerHandshake.proofMessage(crypto.proof(pairing.credentialJson, challenge, endpoint))
    }

    /**
     * The verifier's side: challenge a discovered device through [exchange]
     * (send one message, return the reply) and let the binding decide. Every
     * failure is a refusal with its reason; nothing ends in Verified unless the
     * binding said so.
     */
    fun verify(
        claimedId: Long,
        pairing: PeerPairing,
        crypto: PeerCrypto,
        exchange: (ByteArray) -> ByteArray,
    ): PeerStanding = try {
        val challenge = crypto.newChallenge()
        val proof = PeerHandshake.proofIn(exchange(PeerHandshake.challengeMessage(challenge)))
        if (proof == null) {
            PeerStanding.Refused("the device did not answer with a proof")
        } else {
            val result = crypto.verify(pairing.rosterJson, claimedId, challenge, proof)
            VerifiedPeer.parse(result)?.let { PeerStanding.Verified(it) }
                ?: PeerStanding.Refused("the verification result was unreadable")
        }
    } catch (e: Exception) {
        // Exception, not Throwable: an Error is a broken process, not a peer
        // to refuse. Everything the binding or the socket throws lands here.
        PeerStanding.Refused(e.message ?: e.javaClass.simpleName)
    }
}

/** Every service NSD has reported, and where each stands. Not thread-safe:
 *  the caller confines it to one thread. */
class PeerRoster {
    private val standings = mutableMapOf<String, PeerStanding>()

    fun found(service: String) {
        standings.putIfAbsent(service, PeerStanding.Checking)
    }

    /** Ignored for a service already lost: a verification that finishes after
     *  the device left must not bring it back. */
    fun record(service: String, standing: PeerStanding) {
        if (standings.containsKey(service)) standings[service] = standing
    }

    fun lost(service: String) {
        standings.remove(service)
    }

    /** Forgets every service, for when discovery stops. */
    fun clear() {
        standings.clear()
    }

    /** Counted, never named: a refused device's name is its own unproven claim. */
    val refusedCount: Int get() = standings.values.count { it is PeerStanding.Refused }

    /** Every verified peer, each device once. An adopted peer stays here after
     *  it stops being offered; this is what says its lease is worth renewing. */
    val verifiedPeers: List<VerifiedPeer>
        get() = standings.values
            .filterIsInstance<PeerStanding.Verified>()
            .map { it.peer }
            .sortedWith(compareBy({ it.name }, { it.clientId }))
            .distinctBy { it.clientId }

    companion object {
        /** The verified peers worth offering as legs: not this phone, and not
         *  already a leg by name or by endpoint. [PeerSnapshot.offers] is the
         *  caller; the rule lives here beside the roster it reads. */
        fun offersFrom(
            verified: List<VerifiedPeer>,
            ownClientId: Long,
            legNames: Set<String>,
            legEndpoints: Set<String>,
        ): List<VerifiedPeer> = verified
            .filter { it.clientId != ownClientId }
            .filter { it.legName !in legNames && it.endpoint !in legEndpoints }
    }
}

/**
 * What the Status screen renders: an immutable copy of discovery, published
 * on every change. The roster itself stays confined to the discovery thread.
 */
data class PeerSnapshot(
    val status: Status = Status.UNPAIRED,
    val failure: String? = null,
    val ownClientId: Long = 0,
    val verified: List<VerifiedPeer> = emptyList(),
    val refusedCount: Int = 0,
    /** Client ids the operator chose to add. */
    val adopted: Set<Long> = emptySet(),
    val outcomes: Map<Long, LegAnnouncer.Outcome> = emptyMap(),
    /** Why adopted peers are not being announced, when something blocks all
     *  of them at once. */
    val blocker: String? = null,
) {
    enum class Status { UNPAIRED, RUNNING, FAILED }

    fun offers(legNames: Set<String>, legEndpoints: Set<String>): List<VerifiedPeer> =
        PeerRoster.offersFrom(verified, ownClientId, legNames, legEndpoints)
}
