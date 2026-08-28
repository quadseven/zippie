package app.zippie.companion

/**
 * What a leg has actually carried, in the three states a status screen must be
 * able to tell apart.
 *
 * Ported from the TrafficBar in ZippieCompanionApp/Design/LegRow.swift, with
 * one state iOS does not have. Swift's Leg carries non-optional byte counts, so
 * an empty bar there can only mean "measured, and nothing moved". Android's
 * BondStatus keeps every counter nullable on purpose - a router that did not
 * publish `link_tx_bytes` said nothing, and saying nothing is not the same as
 * saying zero - so this type has to carry the difference.
 *
 * The split is REAL DATA - up against down, both measured - not a decorative
 * progress track. It REPLACED A SHARE BAR THAT WAS DECORATION on iOS: with one
 * leg the share is always 100%, so the bar rendered as a full-width slab that
 * said nothing while being the loudest thing on the screen. Up against down
 * differs run to run and answers a question someone might have: is this phone
 * uploading for the bond, or pulling down?
 */
sealed class TrafficReading {

    /**
     * Neither direction was measured. Drawn as NO TRACK AT ALL plus a sentence
     * saying so - an empty track would claim a measurement of zero, which is
     * the one thing this reading is not.
     */
    data object Unmeasured : TrafficReading()

    /**
     * Measured, and nothing has moved. Drawn as an EMPTY TRACK, and that
     * distinction is the whole point: a leg that is up but has carried nothing
     * is the failure this system keeps having, and a filled bar would hide it.
     */
    data object Nothing : TrafficReading()

    /**
     * @param upFraction the share of the track the sent capsule takes, in 0..1.
     *   NOTHING CARRIED IS NOT ONE OF THESE - see [Nothing]. Left to a
     *   proportional layout, a leg with zero bytes drew the "received" capsule
     *   across the FULL width, because it has no width of its own and simply
     *   expands, so a leg that had carried nothing at all rendered as a full
     *   bar. That is the precise failure the component was built to expose,
     *   reintroduced by the layout that was supposed to expose it.
     */
    data class Split(
        val upBytes: Long?,
        val downBytes: Long?,
        val upFraction: Float,
    ) : TrafficReading()
}

object LegTraffic {

    /**
     * A null counter is folded in as zero FOR THE SPLIT ONLY, never for the
     * words. A leg the router reported 40 MB of receive for and nothing at all
     * about sending is genuinely 100% down-heavy on the evidence available, and
     * the caption says "unknown sent" so the bar is not read as a claim that it
     * sent zero.
     */
    fun read(up: Long?, down: Long?): TrafficReading {
        if (up == null && down == null) return TrafficReading.Unmeasured
        val u = (up ?: 0L).coerceAtLeast(0)
        val d = (down ?: 0L).coerceAtLeast(0)
        val total = u + d
        if (total == 0L) return TrafficReading.Nothing
        return TrafficReading.Split(up, down, u.toFloat() / total.toFloat())
    }

    /** The line under the bar. Only rendered for a [TrafficReading.Split]: an
     *  empty track needs no caption to say it is empty, and an unmeasured leg
     *  gets [unmeasuredCaption] instead. */
    fun caption(reading: TrafficReading.Split): String {
        val sent = reading.upBytes?.let { Fmt.bytes(it) } ?: "unknown"
        val received = reading.downBytes?.let { Fmt.bytes(it) } ?: "unknown"
        return "$sent sent, $received received"
    }

    const val UNMEASURED_CAPTION = "traffic not measured"
}
