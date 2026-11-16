package app.zippie.companion

/**
 * The "Nearby" list: verified peers on the local network, and nothing else.
 *
 * Fed by [PeerDiscovery]'s callbacks. A refused service never reaches
 * [onVerified], so it cannot appear here; a service that goes away reaches
 * [onLost] in the same browse cycle and leaves the list with it.
 */
class NearbyList {

    private val lock = Any()
    private val byService = mutableMapOf<String, VerifiedPeer>()

    /** The verified peers, ordered by client id so the list does not jump. */
    val peers: List<VerifiedPeer>
        get() = synchronized(lock) { byService.values.sortedBy { it.clientId } }

    fun onVerified(serviceName: String, peer: VerifiedPeer) {
        synchronized(lock) { byService[serviceName] = peer }
    }

    fun onLost(serviceName: String) {
        synchronized(lock) { byService.remove(serviceName) }
    }
}

/**
 * What choosing a nearby peer writes: the leg name and the fields of the
 * router's leg-edit call (PUT /api/legs/<name>), keyed exactly as a manual
 * edit keys them. Choosing a peer must produce the same config as typing it.
 */
data class NearbyLegEdit(val legName: String, val fields: Map<String, Any>)

object NearbyLeg {

    /** The edit that adds [peer] as an enabled leg labeled with its id. */
    fun edit(peer: VerifiedPeer): NearbyLegEdit = NearbyLegEdit(
        legName = "peer-${peer.clientId}",
        fields = mapOf("label" to "Nearby peer ${peer.clientId}", "enabled" to true),
    )
}
