package app.zippie.companion

/**
 * Number formatting, in one place so two screens cannot round differently.
 *
 * MOVED OUT OF BondLegs.kt, unchanged, when the throughput chart arrived. Not a
 * tidying: BondLegs.kt reaches for BondStatus, which reaches for org.json,
 * which on a bare JVM is the android.jar stub that throws "Stub!". Keeping the
 * formatters in that file meant the chart maths could not be compiled or run
 * outside Gradle at all, and the whole point of splitting the chart into plain
 * Kotlin was that it can be. Same package, so nothing that calls it changed.
 */
object Fmt {
    fun bytes(b: Long): String {
        val mb = b / 1_048_576.0
        return when {
            mb >= 1000 -> String.format("%.2f GB", mb / 1024)
            mb >= 10 -> String.format("%.0f MB", mb)
            mb >= 0.1 -> String.format("%.1f MB", mb)
            else -> "${b / 1024} KB"
        }
    }

    /**
     * Bits per second, in the units people actually say.
     *
     * BITS, not bytes, and the two must never be confused on the same screen: a
     * leg's totals are in bytes ([bytes]) because that is what a data cap is
     * counted in, and its rate is in bits because that is what a link is sold
     * in. Saying "1.4 MB/s" next to "11 Mbps" for the same leg would look like
     * a contradiction rather than like two units.
     */
    fun rate(bps: Double): String = when {
        bps >= 1_000_000 -> String.format("%.1f Mbps", bps / 1_000_000)
        bps >= 1_000 -> String.format("%.0f kbps", bps / 1_000)
        else -> String.format("%.0f bps", bps)
    }

    /** Seconds since a timestamp, in words. Used for freshness, where "4s ago"
     *  and "no answer yet" must not look the same. */
    fun age(nowMs: Long, thenMs: Long): String {
        val seconds = ((nowMs - thenMs) / 1000).coerceAtLeast(0)
        return when {
            seconds < 2 -> "just now"
            seconds < 60 -> "${seconds}s ago"
            else -> "${seconds / 60}m ago"
        }
    }
}
