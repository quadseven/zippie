package app.zippie.companion

/**
 * The last boot stand-down, as the home screen shows it (zippie#180 AC3).
 *
 * KEPT ANDROID-FREE ON PURPOSE, like BootRelayDecision: the display contract
 * is the part of this that can be proven in the unit suite, so it lives here
 * rather than inline in the screen. Everything that touches a Context -
 * persisting the record, reading it back - is in BootStanddownStore.
 */
data class BootStanddown(
    /** Why the relay did not start, verbatim from the decision. */
    val reason: String,
    /** Whether a retry was armed when it stood down. */
    val retryArmed: Boolean,
    /**
     * How long after the stand-down the retry fires, when armed. Null means
     * the normal schedule applies rather than an override.
     */
    val retryAfterMs: Long?,
    /** Wall clock (System.currentTimeMillis) when the stand-down was recorded. */
    val stoodDownAtMs: Long,
) {
    /**
     * The line the home screen shows for a phone that is not contributing
     * because it stood down: the reason, and when it will reconsider.
     */
    fun displayLine(nowMs: Long): String {
        val head = "Standing down: ${reason.trimEnd().removeSuffix(".")}."
        if (!retryArmed) return "$head Will not check again on its own."
        if (retryAfterMs == null) return "$head Will check again automatically."
        val remainingMs = stoodDownAtMs + retryAfterMs - nowMs
        return "$head Will check again in ${approxDuration(remainingMs)}."
    }

    /**
     * One record per line file. The reason travels last and may contain any
     * characters except a newline, so it is never split.
     */
    fun serialize(): String = listOf(
        "v1",
        retryArmed.toString(),
        retryAfterMs?.toString() ?: "",
        stoodDownAtMs.toString(),
        reason,
    ).joinToString("\n")

    companion object {
        fun deserialize(raw: String): BootStanddown? {
            return try {
                val lines = raw.split("\n")
                if (lines.size < 5 || lines[0] != "v1") return null
                BootStanddown(
                    reason = lines.drop(4).joinToString("\n"),
                    retryArmed = lines[1].toBooleanStrict(),
                    retryAfterMs = lines[2].ifEmpty { null }?.toLong(),
                    stoodDownAtMs = lines[3].toLong(),
                )
            } catch (e: Exception) {
                null
            }
        }

        private fun approxDuration(ms: Long): String = when {
            ms <= 0 -> "a moment"
            ms < 90_000 -> "about a minute"
            ms < 3_600_000 -> {
                val m = maxOf(1L, (ms + 30_000) / 60_000)
                "about $m minutes"
            }
            else -> {
                val h = (ms + 1_800_000) / 3_600_000
                if (h <= 1) "about an hour" else "about $h hours"
            }
        }
    }
}
