package app.zippie.companion

/**
 * Per-person NextDNS profile, so two phones behind one bond report to two
 * different profiles under two different device names.
 *
 * NextDNS encodes both in the DoH URL itself:
 *
 *     https://dns.nextdns.io/<profile>/<device-name>
 *
 * iOS installs this URL as a device DNS setting. Android uses the DoT hostname
 * below in its Private DNS setting.
 */
data class NextDNSProfile(
    val profileID: String,
    val deviceName: String = "",
) {
    /**
     * A profile id is hex-ish and short. Validating here rather than at the
     * point of use means a typo is refused while the user is still looking at
     * the field, instead of silently resolving nothing later - DNS failing is
     * indistinguishable from a broken network.
     */
    val isValid: Boolean
        get() = profileID.length in 4..16 && profileID.all { it in 'a'..'z' || it in '0'..'9' }

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
     * The DoT hostname for Android Private DNS. NextDNS uses a prefixed name;
     * spaces become two hyphens, and the first DNS label stays within 63 bytes.
     */
    val dotHostname: String?
        get() {
            if (!isValid) return null
            val name = deviceName.trim().flatMap { c ->
                when {
                    c == ' ' -> listOf('-', '-')
                    c in 'a'..'z' || c in 'A'..'Z' || c in '0'..'9' || c == '-' -> listOf(c)
                    else -> listOf('-')
                }
            }.joinToString("").trim('-').take(62 - profileID.length).trim('-')
            return "${if (name.isNotEmpty()) "$name-" else ""}$profileID.dns.nextdns.io"
        }

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
