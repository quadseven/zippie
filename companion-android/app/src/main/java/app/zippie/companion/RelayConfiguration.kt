package app.zippie.companion

import android.content.Context
import android.content.SharedPreferences
import android.os.UserManager

/**
 * Everything the two services and the status screen need, in one place.
 *
 * Mirrors RelayConfiguration.swift where the fields mean the same thing, so the
 * two apps cannot drift into meaning different things by the same name. It
 * carries none of the Swift type's App Group machinery: on Android the relay is
 * a foreground service in THIS process, not an extension with its own
 * container, so the cross-process handoff that dominates that type has nothing
 * to solve here.
 *
 * The router SSID from the iOS type is deliberately absent - see RouterProximity
 * for why this app decides its mode by probing the console rather than by
 * reading a network name.
 */
data class RelayConfiguration(
    /** UDP port the router dials on this phone. */
    val listenPort: Int = 51999,
    /**
     * The home transport's public endpoint - the one this phone's legs spray to.
     *
     * NO DEFAULT, DELIBERATELY. This used to name the author's own host, which
     * meant every install of this app carried a stranger's infrastructure inside
     * it and a phone that was never configured would quietly try to reach it.
     * Empty is the honest starting state: this app cannot know where anyone's
     * home is until somebody tells it.
     */
    val homeHost: String = "",
    val homePort: Int = 51902,
    /**
     * The router's console on its own LAN.
     *
     * CONFIGURATION, NOT INFERENCE. The phone could guess the router's address
     * from its own (take the wifi IP, replace the last octet with .1) and that
     * guess would be right on this network and wrong in a hotel. A wrong guess
     * means polling a stranger's device, so the address is stated rather than
     * derived. Cleartext to this host is permitted by
     * res/xml/network_security_config.xml; change one and the other must follow,
     * or the fetch fails with a cleartext error that looks like a dead router.
     * That coupling is the reason this cannot be freely configured yet - see #156.
     *
     * NO DEFAULT. It named one specific household's router, on every install.
     */
    val consoleLanHost: String = "",
    /** The console over the tailnet. Answers from anywhere on earth, which is
     *  exactly why reaching it proves nothing about where this phone is.
     *  NO DEFAULT: it named a specific tailnet host. */
    val consoleUrl: String = "",
    /** Zero means unlimited - see DataBudget for why inventing a cap would be
     *  wrong. */
    val dailyBudgetBytes: Long = 0,
    val monthlyBudgetBytes: Long = 0,
    /**
     * The router's console WRITE token, which is what lets this phone announce
     * itself as a leg. Reads are open; writes are not, because an announcement
     * adds a leg the router will dial.
     *
     * EMPTY IS NORMAL AND IS NOT AN ERROR. The relay still relays without it -
     * it simply does not appear in the bond unless somebody adds a static entry,
     * which is the thing announcing exists to make unnecessary.
     */
    val announceToken: String = "",
    /** Datadog client token, so this phone can report on itself over CELLULAR
     *  while the router - and therefore adb, and therefore every other way of
     *  asking it anything - is unreachable (#186). Empty disables shipping
     *  entirely, which is the correct default for a device nobody has
     *  provisioned. */
    val ddClientToken: String = "",
    val ddSite: String = "datadoghq.com",
) {
    val budget: DataBudget get() = DataBudget(dailyBudgetBytes, monthlyBudgetBytes)

    /**
     * Everything the announcer needs, or null when this phone is not configured
     * to announce. Mirrors `announceConfig` in RelayConfiguration.swift.
     *
     * The LAN console address, never the tailnet one: the tailnet console
     * answers from anywhere on earth, and announcing to it would be telling a
     * router to dial a private address on a network this phone is not on.
     */
    fun announceConfig(name: String, label: String): LegAnnouncer.Config? {
        val c = LegAnnouncer.Config(
            consoleHost = consoleLanHost.trim(),
            token = announceToken,
            name = name,
            label = label,
            listenPort = listenPort,
        )
        return if (c.isUsable) c else null
    }

    /**
     * REDACTED. A data class prints every field it has, so the day someone logs
     * the configuration to chase a bad host, the router's write token goes into
     * logcat with it.
     */
    override fun toString(): String =
        "RelayConfiguration(listenPort=$listenPort, homeHost=$homeHost, homePort=$homePort, " +
            "consoleLanHost=$consoleLanHost, consoleUrl=$consoleUrl, " +
            "dailyBudgetBytes=$dailyBudgetBytes, monthlyBudgetBytes=$monthlyBudgetBytes, " +
            "announceToken=${if (announceToken.isEmpty()) "<unset>" else "<redacted>"})"

    /**
     * Best-first console addresses. Both are needed and neither is a fallback
     * for the other: on the router's own wifi the LAN address is the one that
     * answers, and away from it only the tailnet name does.
     */
    val consoleCandidates: List<ConsoleCandidate>
        get() = buildList {
            val lan = consoleLanHost.trim()
            if (lan.isNotEmpty()) add(ConsoleCandidate("http://$lan/api/status", isLocal = true))
            val remote = consoleUrl.trim()
            if (remote.isNotEmpty() && none { it.url == remote }) {
                add(ConsoleCandidate(remote, isLocal = false))
            }
        }

    /**
     * Lenient on purpose, and only here. A settings screen must still render
     * when a field is nonsense; the SERVICES must refuse to start instead of
     * guessing, which is what [validated] is for.
     */
    fun validated(): Result<RelayConfiguration> {
        if (homeHost.isBlank()) {
            return Result.failure(IllegalArgumentException("home host is empty"))
        }
        if (listenPort !in 1..65535) {
            return Result.failure(IllegalArgumentException("listen port $listenPort out of range"))
        }
        if (homePort !in 1..65535) {
            return Result.failure(IllegalArgumentException("home port $homePort out of range"))
        }
        return Result.success(this)
    }

    companion object {
        private const val PREFS = "zippie"
        private const val KEY_LISTEN_PORT = "listenPort"
        private const val KEY_HOME_HOST = "homeHost"
        private const val KEY_HOME_PORT = "homePort"
        private const val KEY_CONSOLE_LAN = "consoleLanHost"
        private const val KEY_CONSOLE_URL = "consoleUrl"
        private const val KEY_DAILY_BUDGET = "dailyBudgetBytes"
        private const val KEY_MONTHLY_BUDGET = "monthlyBudgetBytes"
        private const val KEY_ANNOUNCE_TOKEN = "announceToken"
        private const val KEY_DD_TOKEN = "ddClientToken"
        private const val KEY_DD_SITE = "ddSite"
        /** LegName.KEY, restated so this file does not depend on that object. */
        private const val LEG_NAME_KEY = "legName"

        /**
         * The store, from whichever storage is READABLE RIGHT NOW.
         *
         * BEFORE FIRST UNLOCK THERE IS NO CREDENTIAL-ENCRYPTED STORAGE. Reading
         * it throws, and on 2026-08-16 that killed the relay one second after
         * BootReceiver correctly started it:
         *
         *   W ContextImpl: Failed to ensure /data/user/0/app.zippie.companion/shared_prefs
         *       at RelayConfiguration.prefs(RelayConfiguration.kt:145)
         *       at RelayService.onStartCommand(RelayService.kt:161)
         *   E AndroidRuntime: Unable to start service RelayService
         *   I ActivityManager: Process app.zippie.companion has died
         *   W ActivityManager: Scheduling restart of crashed service in 1800000ms
         *
         * Android then deferred the restart by THIRTY MINUTES, which is why the
         * phone looked permanently dead rather than briefly broken.
         *
         * It only happens on a LOCKED boot - the one case that matters for a
         * relay phone nobody touches, and the one a human can never hit by hand
         * because they unlock the phone before looking.
         *
         * So when the user is locked, read the device-protected copy that
         * [mirrorForLockedBoot] keeps in step. Same file name, different
         * storage: unlocked callers see exactly what they always did.
         */
        fun prefs(context: Context): SharedPreferences =
            storageContext(context).getSharedPreferences(PREFS, Context.MODE_PRIVATE)

        private fun storageContext(context: Context): Context {
            val app = context.applicationContext
            val um = app.getSystemService(UserManager::class.java)
            // isUserUnlocked, not isDeviceLocked: the question is whether
            // credential-encrypted storage is MOUNTED, not whether a lockscreen
            // is showing.
            return if (um != null && !um.isUserUnlocked) {
                app.createDeviceProtectedStorageContext()
            } else {
                app
            }
        }

        /**
         * Copy the whole configuration into device-protected storage.
         *
         * Called whenever the config could have changed, so the locked-boot read
         * above is never stale. It mirrors EVERY field the relay needs, not just
         * the console ones BootConfigStore keeps for its own decision - a relay
         * that starts with no homeHost or no token is running, and useless.
         */
        /**
         * Storage for things whose value must NOT depend on whether the phone is
         * unlocked - always device-protected.
         *
         * The leg NAME is identity, not configuration. It lived in
         * credential-encrypted storage, so once prefs() started returning
         * device-protected storage on a locked boot the minted suffix was
         * invisible and the phone renamed itself: pixel-6a-17d0 -> pixel-6a-a554
         * on the 2026-08-16 cold boot. That rename was introduced by the fix for
         * the locked-boot crash, which is exactly the kind of second-order damage
         * worth naming rather than quietly patching.
         *
         * It is not cosmetic. The router keys its leg table by this string, so a
         * rename is a NEW leg: it loses the transport pid that #163 made stable
         * per name (and the keepalive-loss history keyed to that pid), any
         * legs.json operator override addressed to the old name stops applying,
         * and the old leg lingers until its lease expires.
         */
        fun stablePrefs(context: Context): SharedPreferences =
            context.applicationContext
                .createDeviceProtectedStorageContext()
                .getSharedPreferences(PREFS, Context.MODE_PRIVATE)

        fun mirrorForLockedBoot(context: Context) {
            val current = load(context)
            context.applicationContext
                .createDeviceProtectedStorageContext()
                .getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit()
                .putInt(KEY_LISTEN_PORT, current.listenPort)
                .putString(KEY_HOME_HOST, current.homeHost)
                .putInt(KEY_HOME_PORT, current.homePort)
                .putString(KEY_CONSOLE_LAN, current.consoleLanHost)
                .putString(KEY_CONSOLE_URL, current.consoleUrl)
                .putLong(KEY_DAILY_BUDGET, current.dailyBudgetBytes)
                .putLong(KEY_MONTHLY_BUDGET, current.monthlyBudgetBytes)
                .putString(KEY_ANNOUNCE_TOKEN, current.announceToken)
                .putString(KEY_DD_TOKEN, current.ddClientToken)
                .putString(KEY_DD_SITE, current.ddSite)
                .apply()
            // Carry an already-minted leg name across, once, so applying this
            // fix does not itself cause one final rename.
            val minted = context.applicationContext
                .getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getString(LEG_NAME_KEY, null)
            if (!minted.isNullOrBlank()) {
                stablePrefs(context).edit().putString(LEG_NAME_KEY, minted).apply()
            }
        }

        /** Per-field fallback, because a partially written store must still
         *  produce a usable configuration rather than an empty one. */
        fun load(context: Context): RelayConfiguration {
            val p = prefs(context)
            val d = RelayConfiguration()
            return RelayConfiguration(
                listenPort = p.getInt(KEY_LISTEN_PORT, d.listenPort),
                homeHost = p.getString(KEY_HOME_HOST, d.homeHost)?.trim().orEmpty()
                    .ifEmpty { d.homeHost },
                homePort = p.getInt(KEY_HOME_PORT, d.homePort),
                consoleLanHost = p.getString(KEY_CONSOLE_LAN, d.consoleLanHost).orEmpty()
                    .ifEmpty { d.consoleLanHost },
                consoleUrl = p.getString(KEY_CONSOLE_URL, d.consoleUrl).orEmpty()
                    .ifEmpty { d.consoleUrl },
                dailyBudgetBytes = p.getLong(KEY_DAILY_BUDGET, d.dailyBudgetBytes),
                monthlyBudgetBytes = p.getLong(KEY_MONTHLY_BUDGET, d.monthlyBudgetBytes),
                announceToken = p.getString(KEY_ANNOUNCE_TOKEN, d.announceToken).orEmpty(),
                ddClientToken = p.getString(KEY_DD_TOKEN, d.ddClientToken).orEmpty(),
                ddSite = p.getString(KEY_DD_SITE, d.ddSite).orEmpty().ifEmpty { d.ddSite },
            )
        }

        /**
         * Written on its own rather than through [save], so the one screen that
         * handles the token does not have to hold every other field to store it
         * - and so a future settings screen cannot blank the token by saving a
         * configuration it read before the token was set.
         */
        fun saveAnnounceToken(context: Context, token: String) {
            prefs(context).edit().putString(KEY_ANNOUNCE_TOKEN, token).apply()
        }

        fun save(context: Context, config: RelayConfiguration) {
            prefs(context).edit()
                .putInt(KEY_LISTEN_PORT, config.listenPort)
                .putString(KEY_HOME_HOST, config.homeHost)
                .putInt(KEY_HOME_PORT, config.homePort)
                .putString(KEY_CONSOLE_LAN, config.consoleLanHost)
                .putString(KEY_CONSOLE_URL, config.consoleUrl)
                .putLong(KEY_DAILY_BUDGET, config.dailyBudgetBytes)
                .putLong(KEY_MONTHLY_BUDGET, config.monthlyBudgetBytes)
                .putString(KEY_ANNOUNCE_TOKEN, config.announceToken)
                .putString(KEY_DD_TOKEN, config.ddClientToken)
                .putString(KEY_DD_SITE, config.ddSite)
                .apply()
        }
    }
}
