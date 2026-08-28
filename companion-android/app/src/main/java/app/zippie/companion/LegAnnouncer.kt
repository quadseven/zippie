package app.zippie.companion

import org.json.JSONException
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * Tells the router this phone is here, and keeps saying so.
 *
 * A port of LegAnnouncer.swift, including the reasoning:
 *
 * WHY THIS REPLACES A CONFIG ENTRY. The router used to carry a static leg for
 * each phone: a name and a fixed address in zippie.toml. A phone is not a fixed
 * address. It moves on DHCP, it leaves, it comes back, and the entry stayed - so
 * the router kept dialling an address a phone held once, sprayed megabytes into
 * it, and reported the leg as healthy because a configured leg passes the
 * shallow checks.
 *
 * WORSE THAN USELESS FOR THIS PHONE, not merely stale. `match_interfaces`
 * excludes companion legs on their relay ENDPOINT rather than on the shared
 * br-lan, so a static entry that claims this phone's address takes the endpoint
 * and the announced leg is then refused the bridge, left with no interface and
 * DOWN. The static entries were deleted for that reason; announcing is the only
 * route in.
 *
 * AN ANNOUNCEMENT IS A LEASE. It expires. A phone that goes into a tunnel or
 * runs out of battery stops renewing and its leg goes away on its own, which is
 * the property a config file can never have.
 *
 * RENEWED FROM THE FOREGROUND SERVICE, not the activity. The relay is a
 * foreground service precisely so it survives the screen going off; announcing
 * from the UI would mean the leg expiring the moment someone pocketed the phone
 * while it was still relaying.
 */
class LegAnnouncer(
    // NO DEFAULT. Every caller must say which route its console posts take,
    // because getting that wrong is silent: an unpinned announce still compiles,
    // still runs, and simply never reaches the router on a cold boot (#168).
    private val post: ConsolePost,
    /** Injected so the renewal loop can be PROVEN to renew in a test without it
     *  taking a minute. Production always uses [RENEW_INTERVAL_MS]. */
    private val renewIntervalMs: Long = RENEW_INTERVAL_MS,
) {

    /**
     * Everything one announcement needs. [isUsable] is what stands between a
     * half-configured phone and a stream of 400s: without a token there is
     * nothing to announce WITH, and the relay should carry on relaying rather
     * than treat that as a failure.
     */
    data class Config(
        /** The router's console on its own LAN, as host:port. Announcing over
         *  the tailnet would be announcing to a router we are not on the
         *  network of. */
        val consoleHost: String,
        val token: String,
        /** The router's key for this leg. Stable across address changes - it is
         *  what makes a moved phone an update rather than a second leg. */
        val name: String,
        val label: String,
        val listenPort: Int,
    ) {
        val isUsable: Boolean
            get() = consoleHost.isNotBlank() && token.isNotEmpty() &&
                name.isNotEmpty() && listenPort in 1..65535

        /**
         * REDACTED. A data class prints every field, so one day's debug log
         * line - `Log.d(TAG, "$config")` - would put the router's write token
         * in logcat, where any app with READ_LOGS and every bug report picks it
         * up.
         */
        override fun toString(): String =
            "LegAnnouncer.Config(consoleHost=$consoleHost, token=<redacted>, " +
                "name=$name, label=$label, listenPort=$listenPort)"
    }

    /**
     * Three outcomes, not two. A router that is not there and a router that said
     * no need different words on screen, because they send whoever is reading to
     * check completely different things.
     */
    sealed class Outcome {
        data class Announced(val leaseRemainingS: Double) : Outcome()

        /** The router's own words, verbatim. It names exactly which field it
         *  refused, and inventing a friendlier message would lose that. */
        data class Refused(val reason: String) : Outcome()

        data class Unreachable(val reason: String) : Outcome()
    }

    companion object {
        /**
         * The router caps a lease at 300s and defaults to 45s (DEFAULT_LEASE_S
         * in dynamic.py). Renewing at a third of that means two consecutive
         * failures - a locked screen, a bad radio moment - do not drop the leg.
         */
        const val LEASE_S = 45.0
        const val RENEW_INTERVAL_MS = 15_000L

        /**
         * Short. On the router's own LAN this is a sub-millisecond request, and
         * anywhere else it should fail fast rather than hold the renewal
         * cadence open.
         */
        const val TIMEOUT_MS = 4_000

        const val ANNOUNCE_PATH = "/api/legs/announce"
        const val WITHDRAW_PATH = "/api/legs/withdraw"

        private const val MIN_SLEEP_MS = 1_000L
    }

    @Volatile private var running = false
    private var worker: Thread? = null

    /** What the renewal loop is currently announcing. Volatile because
     *  [reconfigure] is called from the connectivity callback's thread and read
     *  by the announce thread. */
    @Volatile private var current: Config = Config("", "", "", "", 0)

    /**
     * One announcement. [address] is this phone's own LAN address; null means we
     * are not on a local network and there is nothing to announce.
     */
    fun announce(config: Config, address: String?): Outcome {
        if (!config.isUsable) return Outcome.Refused("announcer is not configured")
        if (address.isNullOrEmpty()) {
            // NOT AN ERROR. Off a local network there is no address the router
            // could dial, and announcing a wrong one is worse than silence.
            return Outcome.Refused("no local address to announce")
        }
        val body = JSONObject()
            .put("name", config.name)
            .put("host", address)
            .put("port", config.listenPort)
            .put("label", config.label)
            // Asked for explicitly rather than left to the router's default, so
            // the two ends cannot drift apart silently.
            .put("lease_s", LEASE_S)
        return send(ANNOUNCE_PATH, body, config)
    }

    /**
     * An explicit goodbye, so a phone that stops relaying on purpose does not
     * linger for a whole lease.
     */
    fun withdraw(config: Config): Outcome {
        if (!config.isUsable) return Outcome.Refused("announcer is not configured")
        return send(WITHDRAW_PATH, JSONObject().put("name", config.name), config)
    }

    private fun send(path: String, body: JSONObject, config: Config): Outcome {
        val url = "http://${config.consoleHost}$path"
        val reply = try {
            post.post(url, config.token, body.toString(), TIMEOUT_MS)
        } catch (e: IOException) {
            return Outcome.Unreachable(e.message ?: e.javaClass.simpleName)
        } catch (e: RuntimeException) {
            // Covers the SecurityException Android raises for blocked cleartext
            // and a malformed console host. Both are refusals of ours, not the
            // router's, and both are worth reading verbatim: "Cleartext HTTP
            // traffic to 10.20.0.1 not permitted" names a
            // network-security-config mistake no generic message would.
            return Outcome.Unreachable(e.message ?: e.javaClass.simpleName)
        }
        if (reply.code != 200) {
            return Outcome.Refused(errorIn(reply.body) ?: "HTTP ${reply.code}")
        }
        val lease = leaseIn(reply.body) ?: LEASE_S
        return Outcome.Announced(lease)
    }

    /** The router's reason, when it sent one. A body that is not JSON at all
     *  leaves the caller to fall back on the status code rather than reporting
     *  a parse failure that would hide the code. */
    private fun errorIn(body: String): String? = try {
        JSONObject(body).optString("error").ifEmpty { null }
    } catch (e: JSONException) {
        null
    }

    private fun leaseIn(body: String): Double? = try {
        val o = JSONObject(body)
        if (o.has("lease_s")) o.optDouble("lease_s").takeIf { !it.isNaN() } else null
    } catch (e: JSONException) {
        null
    }

    // ---- keeping the lease alive ----------------------------------------

    /**
     * Announce now, then keep renewing until [stop].
     *
     * [address] is re-read every pass rather than captured, because AN
     * ANNOUNCEMENT IS ALSO A RENEWAL OF THE ADDRESS: a phone that picks up a new
     * DHCP lease must not leave the router dialling the old endpoint. The router
     * takes a changed host:port as a move and re-dials (reconcile_dynamic_legs).
     *
     * A plain thread rather than a coroutine, matching the relay's own pumps:
     * this is one blocking HTTP call on a fixed cadence for the life of a
     * foreground service, and a scope to cancel would be more machinery than the
     * job has.
     */
    fun start(
        config: Config,
        address: () -> String?,
        report: (Outcome) -> Unit,
    ) {
        stop()
        current = config
        running = true
        worker = Thread({ loop(address, report) }, "zippie-relay-announce").apply {
            isDaemon = true
            start()
        }
    }

    /**
     * Change what is announced WITHOUT dropping the leg.
     *
     * For the label, which is the field that can change while relaying: the
     * carrier is re-read when the radio hands over a network, and a phone that
     * started in a tunnel would otherwise keep announcing a label with no
     * carrier in it until the service restarted. Restarting the announcer
     * instead would withdraw the leg and re-announce it, and the router would
     * genuinely drop and rebuild the path for a cosmetic change.
     *
     * The NAME is not expected to change here - it is persisted and stable - and
     * changing it would leave the old leg to expire on its lease rather than
     * being withdrawn.
     */
    fun reconfigure(config: Config) {
        if (running) current = config
    }

    private fun loop(address: () -> String?, report: (Outcome) -> Unit) {
        try {
            while (running) {
                val startedAtMs = System.currentTimeMillis()
                report(announce(current, address()))
                // The cadence is measured from the START of the attempt, so a
                // request that spent its whole timeout does not push the next
                // renewal out to 19s. The lease is wall-clock; the router does
                // not care why we were late.
                val elapsed = System.currentTimeMillis() - startedAtMs
                // Floored so a run of slow requests cannot turn the renewal into
                // a spin, and never above the interval itself, which is what a
                // test drives down.
                val floor = minOf(MIN_SLEEP_MS, renewIntervalMs)
                Thread.sleep((renewIntervalMs - elapsed).coerceAtLeast(floor))
            }
        } catch (e: InterruptedException) {
            // The stop signal. Fall through to the withdraw below rather than
            // returning: this is the one chance to say goodbye.
        } finally {
            // ON THIS THREAD, not the caller's. stop() is called from
            // Service.onDestroy, which runs on the main thread, and an HTTP
            // request there is NetworkOnMainThreadException. The 45s lease is
            // the backstop if the process is killed before this lands.
            val goodbye = current
            if (goodbye.isUsable) withdraw(goodbye)
        }
    }

    /**
     * Stop renewing and withdraw. Returns immediately - the withdraw happens on
     * the worker thread (see [loop]).
     */
    fun stop() {
        running = false
        worker?.interrupt()
        worker = null
    }
}

/** A reply from the console: the status code and the body, whichever stream it
 *  arrived on. */
data class ConsoleReply(val code: Int, val body: String)

/**
 * The one network call the announcer makes, behind an interface so the
 * announcer's behaviour can be proven off a device.
 */
fun interface ConsolePost {
    /** @throws IOException when the router could not be reached at all, which is
     *  a different fact from the router refusing. */
    fun post(url: String, token: String, body: String, timeoutMs: Int): ConsoleReply
}

/**
 * HttpURLConnection, like BondStatusClient: this app makes two kinds of request
 * to one small router, and a client library with its own interceptor stack buys
 * nothing here.
 */
class HttpConsolePost(private val wifi: WifiRoute? = null) : ConsolePost {
    override fun post(url: String, token: String, body: String, timeoutMs: Int): ConsoleReply {
        // ALWAYS OVER WIFI IN PRODUCTION. An announcement goes to the LAN console
        // (`http://<consoleHost>`), and on a cold boot that wifi is unvalidated, so
        // the default network is cellular and this request would leave through
        // clat and never arrive (#168). Announcing is also the step that BREAKS
        // the deadlock - it is what puts this phone in the bond and gives the
        // router its uplink - so it is the one that most needs pinning.
        val opened = if (wifi != null) {
            wifi.open(url) ?: throw IOException("no wifi network, so $url cannot be reached")
        } else {
            URL(url).openConnection() as HttpURLConnection
        }
        val connection = opened.apply {
            requestMethod = "POST"
            connectTimeout = timeoutMs
            readTimeout = timeoutMs
            useCaches = false
            doOutput = true
            // AUTHENTICATED, because an announcement adds a leg the router will
            // DIAL. Unauthenticated, anything on this wifi could point the bond
            // at an address it chose - which is why the router answers 401.
            setRequestProperty("Authorization", "Bearer $token")
            setRequestProperty("Content-Type", "application/json")
            setRequestProperty("Accept", "application/json")
        }
        try {
            connection.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            val code = connection.responseCode
            // The refusal body arrives on the ERROR stream, and it is the part
            // worth keeping: the router names the field it refused.
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val text = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
            return ConsoleReply(code, text)
        } finally {
            connection.disconnect()
        }
    }
}

/**
 * The router's write token, as pasted by a person.
 *
 * PURE, AND THE PART WORTH TESTING. `cat console_token` yields a trailing
 * newline and a long-press paste often carries surrounding whitespace; both
 * produce a 401 that reads as "wrong token" and send the reader back to the
 * router to copy the same string again. Stripping a pasted "Bearer " prefix
 * costs nothing and saves the same wasted trip. Mirrors ConsoleWriteToken in
 * LegEdit.swift so the two apps cannot disagree about what a token is.
 */
object ConsoleWriteToken {
    fun normalise(raw: String): String? {
        var value = raw.trim()
        if (value.lowercase().startsWith("bearer ")) {
            value = value.substring("bearer ".length).trim()
        }
        return value.ifEmpty { null }
    }
}
