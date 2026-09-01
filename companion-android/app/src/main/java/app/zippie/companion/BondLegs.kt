package app.zippie.companion

/**
 * Turns the router's view of the bond into the rows the screen draws.
 *
 * THE HARD PART IS NOT THE MAPPING, IT IS SAYING WHICH LEG IS YOURS - see
 * LegIdentity. The rest of this file exists to keep three facts apart that a
 * status screen naturally blurs together: a leg that is FAILING, a leg that is
 * HELD IN RESERVE, and a leg that is up but NOT CARRYING. All three show no
 * traffic; only one of them is a problem.
 */
enum class LegState {
    CARRYING,
    DEGRADED,
    DOWN,
    IDLE,

    /** Held back by the tier gate, not broken. A cheap SIM kept for the day
     *  everything else fails reports no traffic because it is DOING ITS JOB,
     *  and drawing that as a failure trains the reader to ignore the one signal
     *  on this screen that matters. */
    RESERVE,
}

/**
 * A leg as the UI needs it. Deliberately a view model rather than the raw
 * status type: the app must be able to render "unknown" honestly, and nullable
 * counters are how it does that. A null here means the router did not measure
 * it, and the screen says so instead of printing a zero.
 */
data class Leg(
    val id: String,
    val name: String,
    val state: LegState,
    /** One word for what this leg is doing, carried from BondStatus rather than
     *  re-derived here so the app and the router cannot disagree about it. */
    val stateWord: String,
    val upBytes: Long?,
    val downBytes: Long?,
    val rttMs: Double?,
    val isYou: Boolean,
    /** The router's own words where it has any. "awaiting transport" tells the
     *  reader more than any sentence this app could invent. */
    val note: String?,
    /** Cap and spend, when the router publishes them. */
    val usageNote: String?,
    /**
     * The router says this leg has NEVER been answered - it has sent and had
     * nothing back, ever.
     *
     * Carried as a fact rather than left inside [note], because something other
     * than a leg row now has to read it: the relay verdict at the top of the
     * screen used to say "Carrying" while this row said "Never answered", both
     * on the same screen, for hours (#281). A caller that had to string-match a
     * human sentence to find that out would be a worse bug than the one it fixed.
     */
    val neverAnswered: Boolean = false,
    /** A usable uplink this leg's pattern matched and nobody took (#212).
     *
     *  SEPARATE FROM `note`, not folded into it: the problem is not with this
     *  leg, which may be perfectly healthy - it is that a neighbour is
     *  missing. Putting it in the note chain would mean a healthy leg either
     *  hides it or pretends to be unwell. */
    val shadowNote: String? = null,
    /**
     * WHETHER IT CARRIES, which is NOT the same question as whether it is
     * healthy, and must never be derived from [state].
     *
     * CARRYING AND HEALTH ARE ORTHOGONAL. [LegState] has one slot and has to
     * spend it on how the row is DRAWN, so a leg that is degraded AND carrying
     * is drawn DEGRADED - correctly, it has 12% loss. Counting membership from
     * that slot then reported it as not carrying, which is how the iOS screen
     * came to say "Nothing carrying" and "0 of 3 carrying" directly above a row
     * reading "carrying, degraded" with 402 MB sent.
     *
     * This file used to dodge that by making the drawing lossy instead: a
     * degraded leg with weight became LegState.CARRYING, so the count was right
     * and the ROW was wrong - a leg losing 12% of its packets painted in the
     * healthy accent, which is the same lie pointed the other way.
     *
     * Taken from the router's own `isCarrying` - the same value
     * BondStatus.carryingCount and `stateWord` use - so the headline, the
     * subhead, the row, the chart and the widget cannot disagree.
     */
    val isCarrying: Boolean,
) {
    /** The measured round trip, or null. The row draws no RTT column at all in
     *  that case: a "--" placeholder implies a value that exists and is being
     *  withheld, and a 0 would be a measurement nobody took. */
    val rttShort: String? get() = rttMs?.let { "${Math.round(it)} ms" }

    /** The same fact in words, for the places that cannot leave a gap - a
     *  screen reader has no empty column to read. Rounded in ONE place so the
     *  spoken value and the drawn one can never disagree. */
    val rttText: String get() = rttShort ?: "rtt not measured"

    /**
     * One sentence for a screen reader, which cannot scan a row.
     *
     * Says CARRYING and HEALTH separately, for the same reason the row draws
     * them separately: "degraded" alone would hide that this leg is doing the
     * work, and "carrying" alone would hide that it is struggling.
     */
    val accessibilityDescription: String
        get() = buildString {
            append(name)
            if (isYou) append(", this phone")
            append(", ").append(stateWord)
            append(", ").append(rttText)
            when (val traffic = LegTraffic.read(upBytes, downBytes)) {
                is TrafficReading.Split -> append(", ").append(LegTraffic.caption(traffic))
                TrafficReading.Nothing -> append(", nothing carried")
                TrafficReading.Unmeasured -> append(", ").append(LegTraffic.UNMEASURED_CAPTION)
            }
            note?.let { append(". ").append(it) }
            shadowNote?.let { append(" ").append(it) }
        }
}

object BondLegs {

    /** Rows for the whole bond, in the router's own order - which is priority
     *  order, so the leg most likely to be carrying is nearest the top. */
    fun rows(status: BondStatus, listenPort: Int, localIp: String?): List<Leg> {
        val active = status.activeTier
        // PARITY WITH iOS, which has never drawn these rows: BondLegs.swift
        // filters on `\.isPresent` for exactly this reason. A path with
        // neither an interface nor a relay endpoint is CONFIGURATION, not a
        // connection - an unplugged WAN port, or a station radio associated to
        // nothing. Drawing it made the same bond read as two different bonds
        // depending on which handset you picked up, which is the drift this
        // app exists to remove (operator, 2026-09-01, holding both).
        return (status.paths ?: emptyList()).filter { it.isPresent }.map { path ->
            val name = path.label?.takeIf { it.isNotEmpty() } ?: path.name ?: "unnamed link"
            Leg(
                id = path.name ?: name,
                name = name,
                state = state(path, active),
                stateWord = path.stateWord,
                upBytes = path.carriedTxBytes,
                downBytes = path.carriedRxBytes,
                rttMs = path.rttMs,
                isYou = LegIdentity.identifies(path.relayEndpoint, listenPort, localIp),
                note = note(path, active),
                shadowNote = shadowNote(path),
                usageNote = usageNote(path),
                // STRAIGHT FROM THE ROUTER, never re-derived from `state` above
                // - see the note on Leg.isCarrying for the screen this exact
                // shortcut got wrong.
                isCarrying = path.isCarrying,
                neverAnswered = path.neverHandshaked == true,
            )
        }
    }

    /**
     * The same translation, for the throughput chart: one poll's worth of legs
     * and their byte counters, stamped with when it was read.
     *
     * The stamp is the PHONE'S clock at the moment of the read, not anything
     * the router published, because it is the interval between two of this
     * app's own polls that the rate is computed over. A router clock that steps
     * (NTP settling on a travel router that boots without a network is the
     * normal case, not the exotic one) would otherwise put a step into the
     * denominator of every bar around it.
     */
    fun snapshot(status: BondStatus, atMs: Long): BondThroughput.Snapshot =
        BondThroughput.Snapshot(
            atMs = atMs,
            // SAME FILTER AS rows(), and that is the invariant this function
            // is tested against: the chart cannot show a leg the list below it
            // does not. An absent path has no counters to plot anyway.
            legs = (status.paths ?: emptyList()).filter { it.isPresent }.mapNotNull { path ->
                val name = path.name ?: return@mapNotNull null
                BondThroughput.LegSample(
                    name = name,
                    label = path.label,
                    txBytes = path.carriedTxBytes,
                    rxBytes = path.carriedRxBytes,
                    isCarrying = path.isCarrying,
                )
            },
        )

    /**
     * HOW THE ROW IS DRAWN. Not whether the leg is in the bond - that is
     * Leg.isCarrying, it comes from the router, and the two are orthogonal.
     *
     * `up` with weight 0 is the anti-flap gate holding a recovered leg out of
     * the bond while it proves itself. It is genuinely not carrying, and drawing
     * it as carrying would contradict the router's own console.
     *
     * `degraded` is DEGRADED whether or not it is carrying. This line used to
     * read `if (p.isCarrying) CARRYING else DEGRADED`, which made the count
     * come out right by throwing the health away: a leg losing 12% of its
     * packets while doing all the work was painted in the healthy accent with
     * nothing on the row to say otherwise. The state word underneath still
     * reads "carrying, degraded" because BondStatus builds it from both facts,
     * and now the colour agrees with it.
     */
    private fun state(p: BondStatus.Path, activeTier: Int?): LegState {
        // RESERVE BEATS EVERY OTHER READING. A tier-3 leg with no traffic is not
        // idle, degraded or down - it is being deliberately withheld, and any of
        // those three words would send someone looking for a fault that does not
        // exist.
        if (isReserve(p, activeTier)) return LegState.RESERVE
        return when (p.state) {
            "up" -> if (p.isCarrying) LegState.CARRYING else LegState.IDLE
            "degraded" -> LegState.DEGRADED
            "down" -> LegState.DOWN
            else -> LegState.IDLE
        }
    }

    /**
     * Held in reserve AND actually able to serve.
     *
     * Diverges from the iOS reading, which lets the tier gate win outright. A
     * leg that is DOWN, or that has no interface at all - the router's own
     * "no interface matched" for an unplugged dongle - is not a fallback
     * waiting to be called on, and calling it "held in reserve" would be a
     * reassuring lie about a leg that cannot help anyone. Being gated is only
     * good news when the leg would work if it were called.
     */
    private fun isReserve(p: BondStatus.Path, activeTier: Int?): Boolean =
        p.isHeldInReserve(activeTier) && p.state != "down" && p.isPresent

    private fun shadowNote(p: BondStatus.Path): String? {
        val hidden = p.shadowedInterfaces?.filter { it.isNotEmpty() } ?: return null
        if (hidden.isEmpty()) return null
        val which = hidden.joinToString(", ")
        return if (hidden.size == 1) {
            "$which is a working uplink that no leg is using."
        } else {
            "$which are working uplinks that no leg is using."
        }
    }

    private fun note(p: BondStatus.Path, activeTier: Int?): String? {
        if (isReserve(p, activeTier)) {
            // The router's own last_error for a reserve leg describes the
            // anti-flap gate - true, and completely beside the point when the
            // leg was never going to be used anyway.
            var why = "Held in reserve - only used if everything above it fails."
            val cap = p.maxKbps
            if (cap != null && cap > 0) why += " Capped at $cap kbit/s."
            return why
        }
        // OUTRANKS last_error AND loss, deliberately. The router's own
        // last_error for this leg reads "no reply yet", which is true and
        // describes a moment; "has never been answered" describes its whole
        // life and is the one that says what to go and fix - the endpoint it
        // dials, not the quality of the link.
        if (p.neverHandshaked == true) {
            return "Never answered. This leg has sent traffic and had none " +
                "back, ever - check the address it is dialling rather than " +
                "the signal."
        }
        val e = p.lastError
        if (!e.isNullOrEmpty()) return e
        val loss = p.lossPct
        if (loss != null && loss > 0) return "${loss.toInt()} percent of packets lost."
        return null
    }

    /** Only what the router actually published. A cap of 0 means uncapped, which
     *  is a fact, and an absent usage figure means nobody measured it, which is
     *  a different one - neither is rendered as "0 GB of 0 GB". */
    private fun usageNote(p: BondStatus.Path): String? {
        val cap = p.monthlyCapGb
        val used = p.usageGb
        val over = p.overSoftLimit == true
        val suffix = if (over) " Over its soft limit." else ""
        return when {
            used != null && cap != null && cap > 0 ->
                "${trim(used)} GB used of a ${trim(cap)} GB cap.$suffix"
            used != null -> "${trim(used)} GB used, no cap set.$suffix"
            else -> null
        }
    }

    private fun trim(v: Double): String {
        val rounded = Math.round(v * 100.0) / 100.0
        return if (rounded == Math.floor(rounded)) rounded.toInt().toString() else rounded.toString()
    }

    /**
     * The sentence at the top, derived from the bond rather than from this phone
     * alone.
     *
     * Counts CARRYING legs, not configured ones. "5 connections" when four are
     * down is the reassuring-but-wrong answer; "1 connection" is the true one
     * and takes the same space.
     */
    fun headline(rows: List<Leg>): String {
        val carrying = rows.count { it.isCarrying }
        if (carrying == 0) return "Nothing carrying"
        return if (carrying == 1) "1 connection" else "$carrying connections"
    }

    /**
     * The heading above the connection rows.
     *
     * MUST TELL THE TRUTH ABOUT SCOPE, matching BondModel.swift's
     * `legsHeading`. When the router answers, these rows are the whole bond;
     * when it does not, the only row on screen is [thisPhoneFallback] and a
     * fixed "Connections" heading would claim a count nobody measured.
     *
     * "one out of two" was literally the question on the iOS screen this was
     * ported from - answered here rather than left for someone to count rows.
     */
    fun legsHeading(rows: List<Leg>, bondReachable: Boolean): String {
        if (!bondReachable) return "What this phone carried"
        val carrying = rows.count { it.isCarrying }
        return "Connections - $carrying of ${rows.size} carrying"
    }

    /**
     * The one row this phone can vouch for on its own, when the router's
     * console cannot be reached from here.
     *
     * Ported from BondModel.swift's `rebuild()` fallback: with no console,
     * only THIS phone's own relay counters are knowable, so the screen shows
     * one honest row rather than inventing siblings it cannot see. An
     * invented leg would be exactly the fabrication `tokens.json` calls
     * `neverStateUnmeasured`.
     *
     * NO REPORT AT ALL - or a report older than [RelayReport.STALENESS_MS] -
     * draws NO ROW, matching the iOS twin: a stale relay report says nothing
     * has reported currently, and drawing a row from a corpse's counters
     * would present a stopped relay's last count as current.
     *
     * The state comes from [WidgetContent.toneOf], the same judgement the
     * home-screen widget uses, so this row and the widget's leg list cannot
     * disagree about what "carrying" means for a single phone.
     */
    fun thisPhoneFallback(report: RelayReport?, nowMs: Long): List<Leg> {
        if (report == null || report.isStale(nowMs)) return emptyList()
        val verdict = RelayVerdict.evaluate(report, nowMs)
        val state = when (WidgetContent.toneOf(verdict)) {
            WidgetContent.Tone.LIVE -> LegState.CARRYING
            WidgetContent.Tone.DOWN -> LegState.DOWN
            WidgetContent.Tone.IDLE -> LegState.IDLE
        }
        val stats = report.stats
        return listOf(
            Leg(
                id = "this-phone",
                name = "This phone",
                state = state,
                stateWord = if (state == LegState.CARRYING) "carrying" else "not carrying",
                upBytes = stats.upBytes,
                downBytes = stats.downBytes,
                rttMs = null,
                isYou = false,
                note = stats.budgetExhausted ?: stats.lastError,
                usageNote = null,
                isCarrying = state == LegState.CARRYING,
            ),
        )
    }

    fun subhead(rows: List<Leg>): String {
        val carrying = rows.filter { it.isCarrying }
        if (carrying.isEmpty()) {
            return "The router answered but no link is carrying traffic right now."
        }
        // Whether YOUR phone is in it is the question the person holding it
        // actually has, so it is answered in the sentence rather than left to be
        // inferred from a row further down the page.
        val me = rows.firstOrNull { it.isYou }
        if (me != null) {
            return if (me.isCarrying) "This phone is one of them." else "This phone is not one of them."
        }
        val reserve = rows.count { it.state == LegState.RESERVE }
        val tail = if (reserve > 0) " $reserve held in reserve." else ""
        return if (carrying.size == rows.size) {
            "Every link is carrying.$tail"
        } else {
            "${carrying.size} of ${rows.size} links are carrying.$tail"
        }
    }
}
