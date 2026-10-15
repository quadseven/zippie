package app.zippie.companion

/**
 * Per-person NextDNS profile, so two phones behind one bond report to two
 * different profiles under two different device names.
 *
 * NextDNS encodes both in the DoH URL itself:
 *
 *     https://dns.nextdns.io/<profile>/<device-name>
 *
 * That is the whole reason this works EVERYWHERE the phone goes rather than
 * only on the router's wifi: the setting rides with the device, not with the
 * network.
 */
data class NextDNSProfile(
    val profileID: String,
    val deviceName: String = "",
) {
    init {
        require(isValid) { "profileID must be 4-16 lowercase hex characters" }
    }

    /**
     * A profile id is hex-ish and short. Validating here rather than at the
     * point of use means a typo is refused while the user is still looking at
     * the field, instead of silently resolving nothing later - DNS failing is
     * indistinguishable from a broken network.
     */
    val isValid: Boolean
        get() = profileID.length in 4..16 && profileID.all { it.isLetterOrDigit() && (it.isDigit() || it.isLowerCase()) }

    /**
     * Device names travel in a URL path segment. Anything that would need
     * escaping is replaced with a dash.
     */
    val sanitizedDeviceName: String
        get() {
            val allowed = setOf('-', '_') + ('a'..'z') + ('A'..'Z') + ('0'..'9')
            return deviceName.map { c -> if (c in allowed) c else '-' }.joinToString("").trim('-')
        }

    /**
     * The DoH endpoint this phone should use.
     */
    val dohURL: String?
        get() = if (isValid) {
            val path = "/$profileID${if (sanitizedDeviceName.isNotEmpty()) "/$sanitizedDeviceName" else ""}"
            "https://dns.nextdns.io$path"
        } else null

    /**
     * The DoT hostname, for Android Private DNS which takes a hostname rather
     * than a URL.
     */
    val dotHostname: String?
        get() = if (isValid) "$profileID.dns.nextdns.io" else null

    companion object {
        /**
         * Attempt to parse a NextDNS profile from a DoH URL.
         * Returns null if the URL is not a NextDNS endpoint.
         */
        fun fromDohURL(url: String): NextDNSProfile? {
            if (!url.startsWith("https://dns.nextdns.io/")) return null
            val path = url.substring("https://dns.nextdns.io/".length)
            val components = path.split("/").filter { it.isNotEmpty() }
            if (components.isEmpty() || components.size > 2) return null
            val profileID = components[0]
            val deviceName = if (components.size == 2) components[1] else ""
            return try {
                NextDNSProfile(profileID, deviceName)
            } catch (_: IllegalArgumentException) {
                null
            }
        }
    }
}
