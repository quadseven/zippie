package app.zippie.companion

import java.net.InetAddress
import java.net.InetSocketAddress

/**
 * WHO IS ALLOWED TO SPEAK TO THE RELAY.
 *
 * The same two holes the iOS relay carried until 2026-08-05, avoided here
 * rather than ported:
 *
 *  1. OPEN RELAY. The listener binds every interface, so any host on whatever
 *     wifi the phone joined could have a datagram forwarded over the user's
 *     CELLULAR to the home endpoint - their bytes, the user's data plan, the
 *     user's home transport. On hotel or cafe wifi that is a stranger spending
 *     someone else's plan and delivering bytes of their choosing to a home lab.
 *  2. REPLY HIJACK. If the reply target were replaced by whoever spoke most
 *     recently, one datagram from anyone on the LAN would redirect home's
 *     replies to them, mid-session.
 *
 * The relay is a DUMB HOP and never parses a frame - that is what lets the wire
 * format change without an app release - so it cannot authenticate what it
 * carries. Restricting WHO may talk to it is the only layer available.
 *
 * Lives outside RelayService so that these properties can be PROVEN in a unit
 * test. A guard that is only exercised by real sockets on a real phone is a
 * guard nobody checks.
 */
class RouterGuard(
    /**
     * How long the adopted router must be silent before a different source may
     * take its place. Long enough that a real router restart is never blocked,
     * short enough to recover from one that genuinely went away. Mirrors the
     * datapath's epoch takeover for the same reason.
     */
    private val takeoverIdleMs: Long = 5_000L,
) {
    /** Where replies go. LEARNED from inbound traffic rather than configured,
     *  because the router's source port is ephemeral and changes when it
     *  restarts. */
    @Volatile
    var peer: InetSocketAddress? = null
        private set

    private var lastActivityMs = 0L

    @Volatile
    var rejected: Long = 0L
        private set

    @Synchronized
    fun accept(
        address: InetAddress,
        port: Int,
        nowMs: Long = System.currentTimeMillis(),
    ): Boolean {
        if (!isPlausibleRouter(address)) {
            rejected++
            return false
        }
        val current = peer
        if (current != null && current.address != address &&
            nowMs - lastActivityMs < takeoverIdleMs
        ) {
            // A router is live. Do NOT let a second source displace it.
            rejected++
            return false
        }
        // Same address on a new port IS accepted: that is what a router restart
        // looks like, and refusing it would strand the relay until the takeover
        // window expired.
        peer = InetSocketAddress(address, port)
        lastActivityMs = nowMs
        return true
    }

    companion object {
        /**
         * Link-local and RFC1918 only. CGNAT (100.64.0.0/10) is excluded on
         * purpose even though a carrier may hand it out: the ROUTER is never
         * reached over carrier space, and that range is shared with the tailnet
         * (ADR 0022). Loopback is allowed so the relay can be driven from a
         * desk over adb.
         */
        fun isPlausibleRouter(address: InetAddress): Boolean {
            val b = address.address
            if (b.size != 4) return false
            val a0 = b[0].toInt() and 0xFF
            val a1 = b[1].toInt() and 0xFF
            return when {
                a0 == 10 -> true
                a0 == 192 && a1 == 168 -> true
                a0 == 172 && a1 in 16..31 -> true
                a0 == 169 && a1 == 254 -> true
                a0 == 127 -> true
                else -> false
            }
        }
    }
}
