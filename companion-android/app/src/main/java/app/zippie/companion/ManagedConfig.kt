package app.zippie.companion

/**
 * Configuration delivered by the MDM, so a managed phone needs no hands.
 *
 * WHY THIS EXISTS. The relay installs and runs by itself, and then does nothing,
 * because announcing itself to the router needs a write token that only a human
 * could type in. On 2026-08-11 that left a freshly provisioned Pixel installed,
 * launched, and permanently absent from the bond - and the same gap had an
 * iPhone answering 401 every sixteen seconds for a day.
 *
 * A Device Owner can set application restrictions, and Headwind can deliver
 * them. Declared in res/xml/app_restrictions.xml; read at runtime through
 * RestrictionsManager. That turns the last manual step into a policy the fleet
 * already knows how to push.
 *
 * THE MERGE IS THE WHOLE CORRECTNESS QUESTION, and it is not "managed wins".
 *
 * A restrictions Bundle that omits a key is NOT the same as one that sets it
 * empty. Android hands the app an empty Bundle in ordinary situations - no
 * policy set yet, a device that was never managed, a policy that configures
 * only some keys. If absent meant "clear it", every unmanaged phone would have
 * its working local configuration wiped on next launch, and a policy that set
 * only the token would blank the router address alongside it.
 *
 * So: a key PRESENT and non-blank overrides local storage, because that is the
 * operator's stated policy. A key ABSENT or blank leaves local storage alone.
 * Managed configuration can add and change; it cannot silently subtract.
 */
object ManagedConfig {

    const val KEY_TOKEN = "announceToken"
    const val KEY_CONSOLE_LAN_HOST = "consoleLanHost"
    const val KEY_CONSOLE_URL = "consoleUrl"
    const val KEY_HOME_HOST = "homeHost"
    const val KEY_HOME_PORT = "homePort"
    const val KEY_LISTEN_PORT = "listenPort"
    const val KEY_AUTO_START = "autoStartRelay"
    const val KEY_DD_TOKEN = "ddClientToken"
    const val KEY_DD_SITE = "ddSite"

    /** Offer this phone as a home screen (#222). Absent means no - see
     *  HomeScreenRole for why the default must be off. */
    const val KEY_HOME_SCREEN_MODE = "homeScreenMode"

    /** Every key an MDM may set. Kept beside the parser so a key added to one
     *  and not the other is visible in review rather than at runtime. */
    val KEYS = listOf(
        KEY_TOKEN, KEY_CONSOLE_LAN_HOST, KEY_CONSOLE_URL, KEY_HOME_HOST,
        KEY_HOME_PORT, KEY_LISTEN_PORT, KEY_AUTO_START, KEY_DD_TOKEN, KEY_DD_SITE,
        KEY_HOME_SCREEN_MODE,
    )

    /**
     * Apply MDM-set values on top of what is stored locally.
     *
     * [managed] is the restrictions Bundle flattened to a map so this stays a
     * pure function the JVM tests can reach - the Android read is a one-liner
     * in [AndroidManagedConfig] and has nothing to decide.
     */
    fun merge(stored: RelayConfiguration, managed: Map<String, Any?>): RelayConfiguration {
        fun str(key: String): String? =
            (managed[key] as? String)?.trim()?.takeIf { it.isNotEmpty() }

        fun int(key: String): Int? = when (val v = managed[key]) {
            is Int -> v
            is Long -> v.toInt()
            // Headwind's application-settings UI stores everything as text, so a
            // port arrives as "51999" rather than 51999. Refusing that would
            // make the feature look broken for the only MDM actually pushing it.
            is String -> v.trim().toIntOrNull()
            else -> null
        }?.takeIf { it in 1..65535 }

        return stored.copy(
            announceToken = str(KEY_TOKEN) ?: stored.announceToken,
            // VALIDATED, not merely trimmed. A pushed value that fails is REFUSED
            // and the previous one kept, because the alternative - storing it and
            // failing later - sends the write token somewhere it should never go.
            consoleLanHost = validConsoleLanHost(str(KEY_CONSOLE_LAN_HOST))
                ?: stored.consoleLanHost,
            consoleUrl = validConsoleUrl(str(KEY_CONSOLE_URL)) ?: stored.consoleUrl,
            homeHost = str(KEY_HOME_HOST) ?: stored.homeHost,
            homePort = int(KEY_HOME_PORT) ?: stored.homePort,
            listenPort = int(KEY_LISTEN_PORT) ?: stored.listenPort,
            // The telemetry credential arrives the same way the announce
            // token does. Without this the device can never be given one:
            // the field existed in RelayConfiguration but nothing could set
            // it, which is a credential path that looks wired and is not.
            ddClientToken = str(KEY_DD_TOKEN) ?: stored.ddClientToken,
            ddSite = str(KEY_DD_SITE) ?: stored.ddSite,
        )
    }

    /**
     * Whether the MDM asked this phone to start relaying on its own.
     *
     * Separate from [merge] because it is an ACTION, not a setting: merging it
     * into RelayConfiguration would make "should I start" a property of the
     * configuration, and then every code path that saves a configuration would
     * have an opinion about starting the relay.
     *
     * Absent means no - a phone must never begin relaying because a key was
     * missing.
     */
    fun autoStart(managed: Map<String, Any?>): Boolean = when (val v = managed[KEY_AUTO_START]) {
        is Boolean -> v
        is String -> v.trim().equals("true", ignoreCase = true)
        else -> false
    }

    /**
     * Is this host a private-use address this phone may send the WRITE TOKEN to?
     *
     * THIS IS A CREDENTIAL BOUNDARY, NOT A TYPO CHECK. consoleLanHost is two
     * things at once: the address cleartext HTTP is permitted to, and the address
     * announceConfig() sends the router's write token to. An operator who
     * fat-fingers a public address into the MDM console - or an MDM that is
     * compromised - would have this phone post that token, in the clear, to a
     * stranger's server, while every comment in this app calls that path "the
     * trusted LAN channel".
     *
     * So the app refuses. A console lives on a private network by definition;
     * there is no legitimate deployment where this value is routable on the
     * internet.
     *
     * AN IP LITERAL, NOT A HOSTNAME. A hostname cannot be judged without
     * resolving it, which is not something a pure function can do and not
     * something worth trusting anyway - a hostname that resolves privately today
     * can resolve publicly tomorrow. The cleartext exemption in
     * network_security_config.xml matches literals too, so this costs nothing
     * that was actually available.
     */
    fun isPrivateHostLiteral(host: String): Boolean {
        val h = host.trim().removeSurrounding("[", "]").lowercase()
        if (h.isEmpty()) return false
        if (h == "localhost") return true

        val v4 = h.split(".")
        if (v4.size == 4) {
            val o = v4.map { it.toIntOrNull() ?: return false }
            if (o.any { it !in 0..255 }) return false
            return when {
                o[0] == 10 -> true                       // 10.0.0.0/8
                o[0] == 127 -> true                      // loopback
                o[0] == 172 && o[1] in 16..31 -> true    // 172.16.0.0/12
                o[0] == 192 && o[1] == 168 -> true       // 192.168.0.0/16
                o[0] == 169 && o[1] == 254 -> true       // link-local
                else -> false
            }
        }

        // IPv6: unique-local fc00::/7 (fc.. or fd..), link-local fe80::/10, or ::1.
        if (h.contains(":")) {
            if (h == "::1") return true
            return h.startsWith("fc") || h.startsWith("fd") || h.startsWith("fe8") ||
                h.startsWith("fe9") || h.startsWith("fea") || h.startsWith("feb")
        }
        return false
    }

    /** Splits `host:port`, returning null unless BOTH halves are valid AND the
     *  host passes [isPrivateHostLiteral]. */
    fun validConsoleLanHost(value: String?): String? {
        val v = value?.trim().orEmpty()
        if (v.isEmpty()) return null
        val idx = v.lastIndexOf(':')
        if (idx <= 0 || idx == v.length - 1) return null
        val host = v.substring(0, idx)
        val port = v.substring(idx + 1).toIntOrNull() ?: return null
        if (port !in 1..65535) return null
        if (!isPrivateHostLiteral(host)) return null
        return v
    }

    /**
     * The tailnet console URL, which must be https.
     *
     * Unlike consoleLanHost there is no "trusted LAN" argument available here:
     * this address answers from anywhere on earth, which is the entire reason it
     * exists. Plain http would put the same write token on the open internet.
     */
    fun validConsoleUrl(value: String?): String? {
        val v = value?.trim().orEmpty()
        if (v.isEmpty()) return null
        return if (v.startsWith("https://", ignoreCase = true) && v.length > "https://".length) v else null
    }

    /** For the diagnostics screen: which keys the MDM actually set. Says
     *  "managed" honestly rather than implying it when nothing was pushed. */
    fun managedKeys(managed: Map<String, Any?>): List<String> =
        KEYS.filter { k ->
            when (val v = managed[k]) {
                null -> false
                is String -> v.isNotBlank()
                else -> true
            }
        }
}
