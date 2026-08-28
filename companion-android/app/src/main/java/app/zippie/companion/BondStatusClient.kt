package app.zippie.companion

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.withContext
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * Reading the router's console.
 *
 * A hand-rolled HttpURLConnection rather than a client library: this app makes
 * exactly one kind of request, to one small router, and a dependency that
 * arrives with its own interceptor stack and a two-page changelog buys nothing
 * here.
 */
sealed class ConsoleResult {
    data class Ok(val status: BondStatus, val fromLocal: Boolean) : ConsoleResult()

    /**
     * The message is shown to the operator verbatim. It is often the only clue
     * available - "Cleartext HTTP traffic to 10.20.0.1 not permitted" names a
     * network-security-config mistake that no generic "could not connect" ever
     * would.
     */
    data class Failed(val message: String) : ConsoleResult()
}

/** A console address, and whether reaching it proves we are ON the router's
 *  network. The flag is not decoration: it is the entire basis for choosing
 *  contribute over client mode, because the tailnet address answers from
 *  anywhere on earth. */
data class ConsoleCandidate(val url: String, val isLocal: Boolean)

object BondStatusClient {

    /**
     * @param wifi non-null for a LOCAL candidate, and null for a remote one.
     *   A LAN address must be dialled over wifi even when wifi is unvalidated and
     *   cellular is therefore the default network (#168); a tailnet address must
     *   NOT be, because reaching it over cellular from anywhere is the point of it.
     */
    suspend fun fetch(
        url: String,
        timeoutMs: Int,
        wifi: WifiRoute? = null,
    ): ConsoleResult = withContext(Dispatchers.IO) {
        var connection: HttpURLConnection? = null
        try {
            val opened = if (wifi != null) {
                wifi.open(url) ?: return@withContext ConsoleResult.Failed(
                    // Named exactly, because it sends the operator somewhere
                    // different from a router that did not answer.
                    "no wifi network, so $url cannot be reached"
                )
            } else {
                URL(url).openConnection() as HttpURLConnection
            }
            connection = opened.apply {
                requestMethod = "GET"
                connectTimeout = timeoutMs
                readTimeout = timeoutMs
                useCaches = false
                setRequestProperty("Accept", "application/json")
            }
            val code = connection.responseCode
            if (code != 200) return@withContext ConsoleResult.Failed("HTTP $code from $url")
            val body = connection.inputStream.bufferedReader().use { it.readText() }
            ConsoleResult.Ok(BondStatus.decode(body), fromLocal = false)
        } catch (e: IOException) {
            ConsoleResult.Failed(e.message ?: e.javaClass.simpleName)
        } catch (e: RuntimeException) {
            // Covers JSONException (a body that is not the console's) and the
            // SecurityException Android raises for blocked cleartext. Both are
            // reported rather than swallowed: a silent empty screen would look
            // exactly like a router that is switched off.
            ConsoleResult.Failed(e.message ?: e.javaClass.simpleName)
        } finally {
            connection?.disconnect()
        }
    }

    /**
     * Ask every candidate at once, but let only the LOCAL answer decide where
     * we are.
     *
     * Started concurrently so the LAN attempt's timeout does not delay the
     * tailnet one on every network that is not the router's - which is most of
     * them.
     *
     * NOT first-past-the-post, though. Taking whichever reply landed first would
     * let a fast tailnet response, received while sitting on the router's own
     * wifi, report REMOTE and drop the phone out of contributor mode on the one
     * network where it should be contributing.
     *
     * The local timeout is short because a router on this LAN answers in
     * milliseconds. If it has not replied by then it is not here, and waiting
     * longer only delays an answer that is already known.
     */
    suspend fun probe(
        candidates: List<ConsoleCandidate>,
        localTimeoutMs: Int = 1_500,
        remoteTimeoutMs: Int = 4_000,
        wifi: WifiRoute? = null,
    ): Pair<ConsoleResult, RouterProximity> = coroutineScope {
        if (candidates.isEmpty()) {
            return@coroutineScope ConsoleResult.Failed("no console address configured") to
                RouterProximity.UNREACHABLE
        }
        val local = candidates.filter { it.isLocal }
        val remote = candidates.filterNot { it.isLocal }

        // THE WIFI ROUTE GOES TO THE LOCAL LEG ONLY. Handing it to the remote leg
        // too would break the tailnet read on cellular, which is the one path that
        // is supposed to work when the phone is nowhere near the router.
        val localJob = async { first(local, localTimeoutMs, fromLocal = true, wifi = wifi) }
        val remoteJob = async { first(remote, remoteTimeoutMs, fromLocal = false, wifi = null) }

        val localResult = localJob.await()
        if (localResult is ConsoleResult.Ok) {
            remoteJob.await()
            return@coroutineScope localResult to RouterProximity.LOCAL
        }
        val remoteResult = remoteJob.await()
        if (remoteResult is ConsoleResult.Ok) {
            return@coroutineScope remoteResult to RouterProximity.REMOTE
        }
        // Every address failed. Report the local failure when there was a local
        // candidate at all, because on the router's network that is the one the
        // operator can act on.
        val reported = if (local.isNotEmpty()) localResult else remoteResult
        reported to RouterProximity.UNREACHABLE
    }

    private suspend fun first(
        candidates: List<ConsoleCandidate>,
        timeoutMs: Int,
        fromLocal: Boolean,
        wifi: WifiRoute?,
    ): ConsoleResult {
        var last: ConsoleResult = ConsoleResult.Failed("no address tried")
        for (c in candidates) {
            when (val r = fetch(c.url, timeoutMs, wifi)) {
                is ConsoleResult.Ok -> return r.copy(fromLocal = fromLocal)
                is ConsoleResult.Failed -> last = r
            }
        }
        return last
    }
}
