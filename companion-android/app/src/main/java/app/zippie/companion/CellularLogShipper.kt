package app.zippie.companion

import android.net.Network
import android.util.Log
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

/**
 * Ships this phone's own account of itself to Datadog, OVER CELLULAR.
 *
 * WHY THIS EXISTS, AND WHY IT IS NOT dd-sdk-android.
 *
 * Every diagnosis of this relay so far has been made from the ROUTER's side,
 * inferring what the phone did from what arrived. That is backwards: the phone
 * is the half nobody can see, and it is the half with its own internet.
 *
 * The reason it stayed invisible is circular. `adb` reaches this device THROUGH
 * the router, so the moment the router is dark - which is the only moment worth
 * observing - the phone is unreachable. logcat is no better: the buffer resets
 * to 256 KiB on every boot (`logcat -G` does not persist, and `persist.logd.size`
 * needs root), and `pixel-thermal` alone fills that in minutes. On 2026-08-17 the
 * app's own log lines for a failed cold boot had rotated away before anyone could
 * read them.
 *
 * dd-sdk-android would not fix it. The SDK sends over the DEFAULT network, and
 * during the failure the default network is the router's wifi with no internet -
 * so it would queue and fail exactly when it matters. `RelayService` already holds
 * a `Network` handle pinned to cellular for the bond itself; this posts through
 * that same handle, so the phone keeps reporting while the router is dark. That
 * is the whole design, and it is why ~150 lines beat the SDK here.
 *
 * WHAT IT COSTS. Log lines are small and batched, and every byte is metered on
 * someone's data plan - so this is deliberately NOT wired into the relay's budget
 * ledger, which meters BOND traffic. Telemetry that could be silenced by a full
 * data cap is telemetry that disappears during the incident it exists to explain.
 * The volume is bounded by MAX_QUEUE instead: drop the OLDEST lines, never the
 * newest, because the end of a failure is more informative than its beginning.
 *
 * FAILURE IS ALWAYS SILENT-ISH. A shipper that throws, blocks, or spams the log
 * would be worse than no shipper. Every send is best-effort on a worker thread,
 * and a failed flush is counted rather than retried forever.
 */
class CellularLogShipper(
    private val clientToken: String,
    private val site: String = DEFAULT_SITE,
    private val service: String = "zippie-companion",
    private val tags: String = "",
) {
    private val queue = ConcurrentLinkedQueue<String>()
    private val sending = AtomicBoolean(false)

    /** Lines dropped because the queue was full - the uplink was down too long. */
    val dropped = AtomicLong()

    /** Flushes that failed to POST. Non-zero means gaps in the Datadog timeline. */
    val failures = AtomicLong()

    /** Lines successfully accepted by the intake. */
    val shipped = AtomicLong()

    @Volatile
    private var network: Network? = null

    /** Point the shipper at the cellular network the relay just bound.
     *
     *  Called from bindCellular, so telemetry follows the same handle the bond
     *  uses. Passing null parks the shipper: lines keep queueing (bounded) and
     *  go out when cellular returns, which is what makes a report about losing
     *  cellular survivable. */
    fun useNetwork(n: Network?) {
        network = n
    }

    /**
     * Record one event. Cheap, non-blocking, safe from any thread.
     *
     * `status` is Datadog's level (info/warn/error). `fields` become top-level
     * JSON attributes so they are facetable without parsing the message - the
     * point is to answer "which branch did it take" with a filter, not a regex.
     */
    fun event(
        message: String,
        status: String = "info",
        fields: Map<String, Any?> = emptyMap(),
    ) {
        val sb = StringBuilder(160)
        sb.append('{')
        sb.append("\"ddsource\":\"android\",")
        sb.append("\"service\":").append(quote(service)).append(',')
        if (tags.isNotEmpty()) sb.append("\"ddtags\":").append(quote(tags)).append(',')
        sb.append("\"status\":").append(quote(status)).append(',')
        sb.append("\"message\":").append(quote(message))
        for ((k, v) in fields) {
            sb.append(',').append(quote(k)).append(':')
            when (v) {
                null -> sb.append("null")
                is Number, is Boolean -> sb.append(v.toString())
                else -> sb.append(quote(v.toString()))
            }
        }
        sb.append('}')

        queue.add(sb.toString())
        // OLDEST first. The tail of an incident explains it; the head rarely does.
        while (queue.size > MAX_QUEUE) {
            queue.poll()
            dropped.incrementAndGet()
        }
    }

    /**
     * Send whatever is queued. Returns immediately if another flush is running
     * or there is nothing to do.
     *
     * MUST NOT be called on the main thread - it does network I/O. The relay
     * calls it from its own worker.
     */
    fun flush() {
        if (clientToken.isEmpty()) return
        val net = network ?: return
        if (queue.isEmpty()) return
        if (!sending.compareAndSet(false, true)) return
        try {
            val batch = ArrayList<String>(BATCH)
            while (batch.size < BATCH) {
                val line = queue.poll() ?: break
                batch.add(line)
            }
            if (batch.isEmpty()) return
            if (!post(net, batch)) {
                failures.incrementAndGet()
                // Put them BACK at the head so a transient failure does not lose
                // the incident, but bounded so a permanently dead uplink cannot
                // grow the queue without limit.
                if (queue.size + batch.size <= MAX_QUEUE) {
                    val rest = ArrayList(queue)
                    queue.clear()
                    queue.addAll(batch)
                    queue.addAll(rest)
                } else {
                    dropped.addAndGet(batch.size.toLong())
                }
            } else {
                shipped.addAndGet(batch.size.toLong())
            }
        } catch (e: Exception) {
            failures.incrementAndGet()
            Log.w(TAG, "log flush failed", e)
        } finally {
            sending.set(false)
        }
    }

    private fun post(net: Network, batch: List<String>): Boolean {
        val url = URL("https://http-intake.logs.$site/api/v2/logs")
        // openConnection ON THE NETWORK, not the default one. This single call is
        // the reason the shipper works while the router is dark.
        val conn = net.openConnection(url) as? HttpURLConnection ?: return false
        return try {
            conn.requestMethod = "POST"
            conn.connectTimeout = TIMEOUT_MS
            conn.readTimeout = TIMEOUT_MS
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            conn.setRequestProperty("DD-API-KEY", clientToken)
            val body = batch.joinToString(",", prefix = "[", postfix = "]")
            conn.outputStream.use { out: OutputStream ->
                out.write(body.toByteArray(Charsets.UTF_8))
            }
            val code = conn.responseCode
            code in 200..299
        } catch (e: Exception) {
            Log.w(TAG, "log POST failed", e)
            false
        } finally {
            try {
                conn.disconnect()
            } catch (e: Exception) {
                // Nothing useful to do; the connection is already going away.
            }
        }
    }

    private fun quote(s: String): String {
        val sb = StringBuilder(s.length + 2)
        sb.append('"')
        for (c in s) {
            when (c) {
                '"' -> sb.append("\\\"")
                '\\' -> sb.append("\\\\")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                else -> if (c < ' ') sb.append(String.format("\\u%04x", c.code)) else sb.append(c)
            }
        }
        sb.append('"')
        return sb.toString()
    }

    companion object {
        const val TAG = "ZippieShip"

        /** Datadog site. US1 unless the config says otherwise. */
        const val DEFAULT_SITE = "datadoghq.com"

        /** Bounded so a long outage cannot grow memory without limit. Roughly an
         *  hour of ordinary relay chatter, which comfortably spans a cold boot. */
        const val MAX_QUEUE = 500

        /** Lines per POST. Small enough to fit a slow LTE uplink in one timeout. */
        const val BATCH = 50

        const val TIMEOUT_MS = 10_000
    }
}
