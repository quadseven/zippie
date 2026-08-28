package app.zippie.companion

/**
 * Deciding whether a companion leg on the router is THIS phone.
 *
 * THE WORST BUG THIS APP COULD HAVE lives here. The bond already carries two
 * legs labelled "iPhone (Verizon)" and "Co-operator iPhone (Verizon)", and two Pixels
 * are about to join them. Marking the wrong one as yours would tell somebody
 * their phone is contributing while displaying another phone's traffic - wrong,
 * invisible, and reassuring, which is the worst combination available.
 *
 * So the rule is: match on evidence or do not match at all. The router
 * publishes the address:port it dials for each companion leg; if that is this
 * phone's own wifi address and listen port, the router is literally sending to
 * this device. Anything short of that is a guess, and this returns false for
 * all of them.
 */
object LegIdentity {

    /**
     * True only when [endpoint] provably names this phone.
     *
     * Both halves must match. Two phones on one wifi differ only by address, so
     * the host alone is nearly enough - but a stale config naming the right host
     * and the wrong port describes a leg that can never carry, and claiming it
     * as yours would hide exactly that fault behind a friendly row.
     */
    fun identifies(endpoint: String?, listenPort: Int, localIp: String?): Boolean {
        if (endpoint.isNullOrEmpty() || localIp.isNullOrEmpty()) return false

        // Split on the LAST colon: an IPv6 literal is full of them, and splitting
        // on the first would produce nonsense rather than no match.
        val colon = endpoint.lastIndexOf(':')
        if (colon <= 0 || colon == endpoint.length - 1) return false
        val host = endpoint.substring(0, colon)
        val port = endpoint.substring(colon + 1).toIntOrNull() ?: return false
        if (port != listenPort) return false
        return normalise(host) == normalise(localIp)
    }

    /** IPv6 literals arrive bracketed in an endpoint and bare from the interface
     *  list, and comparing those two forms directly never matches. */
    private fun normalise(host: String): String {
        var h = host.trim()
        if (h.startsWith("[") && h.endsWith("]")) h = h.substring(1, h.length - 1)
        // A scope id ("fe80::1%wlan0") describes how to reach the address, not
        // which address it is.
        val pct = h.indexOf('%')
        if (pct >= 0) h = h.substring(0, pct)
        return h.lowercase()
    }
}
