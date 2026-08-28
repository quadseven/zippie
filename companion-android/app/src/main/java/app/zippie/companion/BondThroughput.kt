package app.zippie.companion

/**
 * The live picture of the whole bond: every leg's throughput, stacked, moving.
 *
 * Ported from ZippieCompanionApp/Design/BondThroughput.swift, which the Android
 * app had no equivalent of at all. The decisions below are that file's, and the
 * comments say why rather than what because every one of them is a bug somebody
 * already shipped.
 *
 * WHY THIS IS ON THE MAIN SCREEN AND PER-LEG HISTORY IS NOT. The question it
 * answers - "is it working, and which links are doing the work" - is the one
 * someone has while glancing at a dashboard. Per-leg RTT and loss are for after
 * you already know something is wrong.
 *
 * STACKED, NOT OVERLAID. Overlaid lines answer "how fast is each leg", which
 * nobody asks. Stacked answers "how much are we getting, and who is providing
 * it" in one shape, and the total is the top edge.
 *
 * WHERE THE NUMBERS COME FROM, AND THE ONE WAY THIS DIFFERS FROM iOS. iOS reads
 * tx_bps/rx_bps out of the router's /api/series, a second endpoint with its own
 * decoder, ring buffer and incremental fetch. This app already polls /api/status
 * every few seconds and that payload already carries the transport's own
 * per-link byte counters (link_tx_bytes / link_rx_bytes, the pair BondStatus
 * prefers because the legacy tx_bytes/rx_bytes are hard 0 in the packet
 * datapath). A rate is the difference between two of those readings over the
 * time between them, so the chart is built from polls this app was making
 * anyway rather than from a second endpoint and a second failure mode.
 *
 * That substitution brings ONE hazard iOS does not have, and it is handled
 * explicitly below: a counter that goes BACKWARDS. The router restarting resets
 * link_tx_bytes to zero, and a naive subtraction turns that into a negative
 * bar - or, worse, into an enormous one if the arithmetic is done unsigned. A
 * backwards counter is not a measurement of anything and is drawn as nothing.
 *
 * NOTHING IS EVER INTERPOLATED, CARRIED FORWARD OR AVERAGED ACROSS A GAP. The
 * same rule BondSeries.swift enforces on iOS: a missing sample is not a
 * measurement, and a null is not a zero.
 */
object BondThroughput {

    /** One leg as the router described it at one instant. */
    data class LegSample(
        /** The router's internal name - the identity the chart keys and colours
         *  by, because a hand-written label can change under it. */
        val name: String,
        /** What a human called it. Null when the router published none. */
        val label: String?,
        val txBytes: Long?,
        val rxBytes: Long?,
        /**
         * From the router's own membership, never derived from how the row is
         * drawn - see the note on [Leg.isCarrying]. A leg that is degraded AND
         * carrying belongs in this chart.
         */
        val isCarrying: Boolean,
    )

    /** Every leg the router listed at one poll, in the router's own order. */
    data class Snapshot(val atMs: Long, val legs: List<LegSample>)

    /** One leg's contribution to one bar. Only ever created for a rate that was
     *  actually measured and is greater than zero. */
    data class Slice(val leg: String, val bps: Double)

    /**
     * One interval between two polls.
     *
     * [measured] is a field rather than `slices.isNotEmpty()` on purpose. An
     * empty bar means two completely different things - "we watched and nothing
     * moved" and "we were not watching" - and collapsing them is the exact
     * habit this app exists to refuse. A measured empty bar is drawn as a mark
     * on the baseline; an unmeasured one is drawn as nothing at all, because
     * nothing is what is known about it.
     */
    data class Bar(val atMs: Long, val slices: List<Slice>, val measured: Boolean) {
        val total: Double get() = slices.sumOf { it.bps }
    }

    /**
     * Everything the chart needs, computed once per poll rather than per
     * redraw - and, more usefully, computable and assertable without a
     * rendering framework anywhere near it.
     */
    data class Chart(
        val bars: List<Bar>,
        /**
         * Draw and colour order: the router's priority order, so the leg most
         * likely to be carrying sits at the bottom of the stack.
         *
         * Colour is by POSITION in this list, never by hashing the name - a
         * hash gives two legs the same colour often enough to matter with five
         * of them.
         */
        val order: List<String>,
        /**
         * Router name -> human label. The bars key by the internal name; the
         * leg list directly below the chart shows labels, and a legend reading
         * "companion-co-operator" under a row reading "Co-operator iPhone (Verizon)" makes
         * the two look like different things.
         */
        val labels: Map<String, String>,
        /** The legs that actually carried something in this window, in [order].
         *  The legend lists these and nothing else: a key entry for a leg that
         *  contributed no pixels is decoration. */
        val carrying: List<String>,
        val peakBps: Double,
        /** How many polls are held. Distinguishes "no history yet" from "a
         *  window in which nothing moved", which read very differently. */
        val snapshotCount: Int,
    ) {
        val hasTraffic: Boolean get() = peakBps > 0

        fun label(leg: String): String = labels[leg] ?: leg

        /**
         * NOT AN EMPTY CHART. A blank axis reads as "broken"; words read as
         * "nothing is moving", which is a different and often correct state.
         */
        val emptyMessage: String
            get() = if (snapshotCount < 2) {
                "No history from the router yet."
            } else {
                "No traffic across the bond in this window."
            }

        val accessibilitySummary: String
            get() {
                if (!hasTraffic) return emptyMessage
                val links = if (carrying.size == 1) "1 link" else "${carrying.size} links"
                return "Throughput over time. $links carrying, peak ${Fmt.rate(peakBps)}."
            }

        companion object {
            val EMPTY = Chart(emptyList(), emptyList(), emptyMap(), emptyList(), 0.0, 0)
        }
    }

    /**
     * Bars for a window of polls, oldest first.
     *
     * @param maxIntervalMs how long a silence may be before the interval stops
     *   counting as a measurement. A phone whose screen was off for an hour
     *   comes back with two readings an hour apart; the byte difference between
     *   them is real, but drawing it as one bar the same width as a five-second
     *   one claims a shape for that hour that nobody observed.
     */
    fun chart(snapshots: List<Snapshot>, maxIntervalMs: Long): Chart {
        if (snapshots.size < 2) {
            return Chart.EMPTY.copy(
                order = order(snapshots),
                labels = labels(snapshots),
                snapshotCount = snapshots.size,
            )
        }
        val order = order(snapshots)
        val bars = ArrayList<Bar>(snapshots.size - 1)
        for (i in 1 until snapshots.size) {
            bars.add(bar(snapshots[i - 1], snapshots[i], maxIntervalMs))
        }
        val carried = bars.flatMap { it.slices }.filter { it.bps > 0 }.map { it.leg }.toSet()
        return Chart(
            bars = bars,
            order = order,
            labels = labels(snapshots),
            carrying = order.filter { it in carried },
            peakBps = bars.maxOfOrNull { it.total } ?: 0.0,
            snapshotCount = snapshots.size,
        )
    }

    private fun bar(before: Snapshot, after: Snapshot, maxIntervalMs: Long): Bar {
        val elapsed = after.atMs - before.atMs
        // A stamp that did not advance is not an interval. The phone's clock can
        // step - it is the phone's clock, not the router's - and dividing by a
        // zero or negative span produces an infinity, not a reading.
        if (elapsed <= 0 || elapsed > maxIntervalMs) {
            return Bar(after.atMs, emptyList(), measured = false)
        }
        val previous = before.legs.associateBy { it.name }
        val slices = ArrayList<Slice>(after.legs.size)
        var readings = 0
        var resets = 0
        for (leg in after.legs) {
            val was = previous[leg.name] ?: continue // first sighting: no interval to measure
            if (wentBackwards(was.txBytes, leg.txBytes) || wentBackwards(was.rxBytes, leg.rxBytes)) {
                resets++
                continue
            }
            val tx = bps(was.txBytes, leg.txBytes, elapsed)
            val rx = bps(was.rxBytes, leg.rxBytes, elapsed)
            if (tx == null && rx == null) continue // the router measured neither end
            readings++
            // WEIGHT ZERO MEANS KEEPALIVES, NOT TRAFFIC. A companion leg whose
            // phone has left still receives a probe every 500ms, which is real
            // bytes on a real socket and charted as ~50 kbps of throughput - so
            // a black hole appeared to be contributing. The scheduler gives a
            // leg outside the bond no data, so anything it moves is overhead.
            //
            // Judged on the LATER reading, the one that closed the interval: a
            // leg that has dropped out of the bond by now is moving probes, and
            // a leg that has just joined moved real bytes to get here.
            if (!leg.isCarrying) continue
            // Both directions, because a bond's value is total capacity and an
            // upload-heavy leg is doing just as much work as a download-heavy
            // one.
            val total = (tx ?: 0.0) + (rx ?: 0.0)
            if (total > 0) slices.add(Slice(leg.name, total))
        }
        // Every leg that reported reset its counters and none produced a
        // reading: the router restarted under us. That is a hole in the record,
        // not a quiet moment on the network.
        val measured = !(readings == 0 && resets > 0)
        return Bar(after.atMs, slices, measured)
    }

    private fun wentBackwards(before: Long?, after: Long?): Boolean =
        before != null && after != null && after < before

    private fun bps(before: Long?, after: Long?, elapsedMs: Long): Double? {
        if (before == null || after == null) return null
        if (after < before) return null
        return (after - before) * 8.0 * 1_000.0 / elapsedMs
    }

    /**
     * The newest poll's order first, then anything that has since left the
     * bond, so a leg that disappears mid-window keeps its colour and its bars
     * instead of handing them to whoever took its index.
     */
    private fun order(snapshots: List<Snapshot>): List<String> {
        val out = LinkedHashSet<String>()
        // Newest first, so the current bond decides the order and history only
        // appends what it no longer contains.
        for (snapshot in snapshots.asReversed()) {
            snapshot.legs.forEach { out.add(it.name) }
        }
        return out.toList()
    }

    /** The most recent non-empty label the router published for each leg. A leg
     *  renamed mid-window is shown under the name it has now. */
    private fun labels(snapshots: List<Snapshot>): Map<String, String> {
        val out = LinkedHashMap<String, String>()
        for (snapshot in snapshots.asReversed()) {
            for (leg in snapshot.legs) {
                val label = leg.label
                if (!label.isNullOrEmpty() && !out.containsKey(leg.name)) out[leg.name] = label
            }
        }
        return out
    }
}

/**
 * The rolling window of polls the chart is drawn from.
 *
 * Immutable, because it lives inside the UI state object that the screen
 * collects: a mutable buffer shared between the poll loop and a recomposition
 * is a race with no upside here, and copying a few hundred small objects every
 * five seconds is not a cost worth thinking about.
 *
 * KEPT ACROSS CONSOLE FAILURES, deliberately, while BondViewModel drops the leg
 * rows. The rows claim to be current and a failed poll makes that a lie; the
 * chart claims to be history, and an outage is part of the history. It shows up
 * as an interval too long to measure - a break in the bars - which is the true
 * shape of "nobody was watching", and considerably more useful than a flat line
 * or a blank screen.
 */
class ThroughputHistory private constructor(
    val snapshots: List<BondThroughput.Snapshot>,
    private val window: Int,
    private val maxIntervalMs: Long,
) {
    constructor(window: Int = DEFAULT_WINDOW, maxIntervalMs: Long) :
        this(emptyList(), window.coerceAtLeast(2), maxIntervalMs)

    /**
     * Fold one poll in.
     *
     * A stamp that does not advance DISCARDS the window rather than being
     * dropped on its own. The phone's clock stepping backwards - which is what
     * this means - makes every interval spanning the step arithmetic rather
     * than measurement, and simply ignoring the new sample would wedge the
     * chart until the clock caught up again. A restarted window costs a few
     * minutes of a live chart; a wedged one is wrong and looks fine.
     */
    fun plus(snapshot: BondThroughput.Snapshot): ThroughputHistory {
        val last = snapshots.lastOrNull()
        if (last != null && snapshot.atMs <= last.atMs) {
            return ThroughputHistory(listOf(snapshot), window, maxIntervalMs)
        }
        val next = snapshots + snapshot
        return ThroughputHistory(next.takeLast(window), window, maxIntervalMs)
    }

    fun chart(): BondThroughput.Chart = BondThroughput.chart(snapshots, maxIntervalMs)

    companion object {
        /**
         * Polls held, not seconds: at BondViewModel's five-second console
         * cadence this is about twenty minutes, and a phone that polls slower
         * holds proportionally longer rather than silently holding less.
         */
        const val DEFAULT_WINDOW = 240
    }
}
