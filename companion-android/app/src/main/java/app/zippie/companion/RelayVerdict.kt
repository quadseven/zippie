package app.zippie.companion

/**
 * What the relay screen and notification may honestly say, and nothing more.
 *
 * PORTED FROM ZippieCompanionKit/RelayVerdict.swift (quadseven/zippie#44), same
 * reasoning even though the syntax differs: a sentence about the ROUTER is only
 * available once something has actually ARRIVED from the router.
 *
 * THE DEFECT THIS REPLACES. [RelayStats.summary] used to decide the sentence
 * from counts:
 *
 *     upDatagrams == 0L && downDatagrams == 0L -> "Listening..."
 *     else -> "Carrying for the bond"
 *
 * `upDatagrams`/`downDatagrams` never decrease, so once a single packet had
 * crossed, that rule read "Carrying for the bond" forever - including an hour
 * after the router's leg for this phone went dark. iOS hit exactly this shape
 * of bug from a different local fact (`cellularReady`) and rejected a
 * count-based fix for the same reason: see RelayVerdict.swift and
 * quadseven/zippie#44, "a count alone would not do: upDatagrams never
 * decreases, so one packet an hour ago read as carrying forever."
 *
 * The fix, ported here: [RelayStats.lastRouterInboundAtMs], a TIMESTAMP of the
 * last inbound packet, recorded when it ARRIVES and before any forwarding
 * decision (RelayService.pumpUp) - so a router dialling a phone whose cellular
 * has died still counts as "the router is talking to me".
 *
 * WHAT THIS DOES NOT MODEL. iOS's `RelayRun` (off/starting/running/stopping)
 * comes from `NEVPNStatus`, read in the SwiftUI view and passed into
 * `RelayVerdict.evaluate`. Android has no equivalent signal reaching this
 * layer today - `RelayService`'s foreground-service lifecycle is not threaded
 * through `BondViewModel`/`BondUiState` - and wiring one is out of scope here
 * (those files belong to other in-flight work). So "off" below covers both
 * "never started" and "starting"/"stopping" alike: the only signal available
 * is whether a [RelayReport] exists at all.
 */
sealed class RelayVerdict {
    /** No report exists. Either the relay has never been started, or it was
     *  just stopped and the store was cleared (RelayStatusStore.clear). */
    object Off : RelayVerdict()

    /** A report exists and has gone quiet - the relay was killed (most likely
     *  by the system, for memory) and its counters are a corpse
     *  (RelayReport.STALENESS_MS). */
    object NotReporting : RelayVerdict()

    data class Paused(val reason: String) : RelayVerdict()

    /** The LAN socket never bound. Nothing the router could ever reach, so
     *  nothing below this can honestly be blamed on the router.
     *
     *  Named `cause`, not `detail` - a constructor property cannot reuse the
     *  name of [RelayVerdict.detail] below without clashing with it. */
    data class NotListening(val cause: String?) : RelayVerdict()

    data class NoCellular(val cause: String?) : RelayVerdict()

    /** Cellular is up and the listener is bound, and NOTHING has ever arrived
     *  from the router. */
    object Listening : RelayVerdict()

    /** The router is sending, and nothing has left this phone over cellular. */
    data class NotForwarding(val cause: String?) : RelayVerdict()

    /** The router sent before and has been silent since. */
    data class RouterQuiet(val silentForMs: Long) : RelayVerdict()

    /**
     * This phone is sending, and the ROUTER says nothing has ever arrived from
     * it.
     *
     * THE STATE THAT DID NOT EXIST ON 2026-08-23, and the one the phone was
     * actually in for hours. At the same moment, this screen read:
     *
     *     Carrying
     *     This phone's cellular is part of the bond.
     *     0.2 MB down in 536 datagrams
     *
     * and the router said of that same leg: `never_handshaked: true`,
     * `link_rx_bytes: 0`, `loss_pct: 100`. Both were honest. The phone counted
     * what it SENT; the router counted what ARRIVED; the reply was leaving by
     * the wrong interface (#278).
     *
     * The phone cannot know it is carrying - carrying is a claim about a remote
     * outcome, and the only evidence for it lives at the other end. When the
     * other end is in hand and contradicts the local counters, the contradiction
     * is the more useful thing to draw.
     */
    object RouterSeesNothing : RelayVerdict()

    object Carrying : RelayVerdict()

    /** The line at the top of the screen and the notification title. */
    val headline: String
        get() = when (this) {
            Off -> "Off"
            NotReporting -> "Not reporting"
            is Paused -> "Paused"
            is NotListening -> "Not listening"
            is NoCellular -> "No cellular"
            // NOT "Standing by" - that reads as "in position, ready to go", a
            // claim about the pair. This phone is listening and has heard
            // nothing; that is all it knows (quadseven/zippie#44).
            Listening -> "Ready"
            is NotForwarding -> "Not relaying"
            is RouterQuiet -> "Router quiet"
            // NOT "Carrying, maybe" or "Degraded". The router is not reporting
            // a quality problem - it is reporting that nothing has EVER arrived
            // from this phone, which is a different and more actionable thing.
            RouterSeesNothing -> "Not arriving"
            Carrying -> "Carrying"
        }

    /** The sentence under it, naming which router when a name is known.
     *
     * TAKES A ROUTER NAME FOR A SEPARATE PROBLEM FROM THE ONE #44 WAS FILED
     * FOR. The original bug was claiming a connection the evidence did not
     * support; that stays fixed by [evaluate] reading a timestamp regardless
     * of this parameter. This is about a claim that IS supported still being
     * unclear: "the router" reads as the wifi router this phone is joined
     * to, a different device entirely (#44 operator follow-up, 2026-08-08:
     * "it says connected to the router still but... it should be connected
     * to a zippie router or something"). Ported from
     * ZippieCompanionKit/RelayVerdict.swift's `detail(router:)`, same
     * reasoning: naming is not a peer-relationship claim, so it carries none
     * of the evidentiary bar "connected" did.
     *
     * NOT WIRED TO ANY ANDROID SCREEN YET. Unlike iOS's
     * `Settings.routerSSID`, nothing on this platform reads or stores a
     * router display name - see the note on [RouterProximity] in
     * BondMode.kt for why this app deliberately does not treat SSID as
     * evidence for the mode decision. A display-only name is a different
     * question and could still be read (with ACCESS_FINE_LOCATION) for this
     * parameter alone, but that is unbuilt; wiring it is left for whenever
     * Android grows a source for it, so this parameter exists only to keep
     * this file's copy logic in lockstep with the iOS twin it is ported
     * from - defaulting to null everywhere reproduces the exact text this
     * function always returned. */
    fun detail(router: String? = null): String {
        val who = router?.takeIf { it.isNotEmpty() } ?: "Your zippie router"
        return when (this) {
            Off ->
                "This phone is not contributing. Start the relay to lend its " +
                    "cellular to the bond."
            NotReporting ->
                "The relay stopped checking in. It was most likely killed by " +
                    "the system for using too much memory."
            is Paused -> reason
            is NotListening -> cause ?: "The relay could not bind its listening socket."
            is NoCellular -> cause ?: "Cellular is not usable right now."
            // THE SENTENCE quadseven/zippie#44 IS ABOUT. It used to be derived
            // from cellularReady alone and claimed a connection nothing proved.
            Listening -> "$who has not sent anything to this phone yet."
            is NotForwarding ->
                if (cause == null) {
                    "$who is sending, but nothing has gone out over cellular yet."
                } else {
                    // Dashed, not colon-joined: the errors this carries are
                    // themselves shaped like "up: no route to host", and a
                    // sentence with two colons reads as a parsing accident.
                    "$who is sending, but nothing has gone out over cellular - $cause"
                }
            is RouterQuiet ->
                "$who stopped sending. Last packet ${ago(silentForMs)} ago."
            RouterSeesNothing ->
                "$who has never had anything arrive from this phone. The " +
                    "counters here are real - they count what left, not what " +
                    "landed."
            Carrying -> "This phone's cellular is part of the bond."
        }
    }

    companion object {
        /**
         * How long the router may be silent before the silence is reported.
         *
         * PAIRED WITH THE ROUTER'S KEEPALIVE, not picked. `persistent_keepalive`
         * is 15s (configs/examples/zippie.toml), so a live leg proves itself at
         * least that often even with no user traffic at all; the packet
         * datapath sprays keepalives on top of that every tick
         * (`probe_interval_ms`, 500ms). A threshold under one keepalive
         * interval would report a healthy idle bond as broken. 25s is the same
         * pairing ZippieCompanionKit/RelayVerdict.swift uses for the same
         * keepalive=15 - the cost of being late here is a sentence, not a
         * blackholed flow.
         */
        const val ROUTER_QUIET_AFTER_MS = 25_000L

        /**
         * Decide from the evidence.
         *
         * ORDER IS THE ARGUMENT. Local faults come first because they are
         * things this phone genuinely knows and they explain the absence of
         * traffic; only once cellular is usable and the listener is bound does
         * anything get said about the far end.
         */
        fun evaluate(
            report: RelayReport?,
            nowMs: Long = System.currentTimeMillis(),
            quietAfterMs: Long = ROUTER_QUIET_AFTER_MS,
            /**
             * The router's own verdict on THIS phone's leg: it has never had
             * anything arrive. Read from `never_handshaked` on the leg the
             * console publishes for this phone (BondLegs.Leg.neverAnswered).
             *
             * DEFAULTS FALSE so every caller that has no router view keeps the
             * behaviour it had. Absence of the router's opinion is not evidence
             * against the phone - only a contradiction is, and this flag is
             * only ever true when the router actively said so.
             */
            routerSeesNothing: Boolean = false,
        ): RelayVerdict {
            if (report == null) return Off
            if (report.isStale(nowMs)) return NotReporting

            val stats = report.stats
            stats.budgetExhausted?.let { return Paused(it) }
            if (!stats.cellularReady) return NoCellular(stats.lastError)
            if (!stats.listening) return NotListening(stats.lastError)

            val inbound = stats.lastRouterInboundAtMs
            if (inbound == null) {
                // No timestamp. Either nothing has ever arrived - the #44
                // case - or the report came from an older build that did not
                // record one yet, in which case the forwarded count still
                // PROVES the router sent something and only the WHEN is
                // unknown. Saying "the router has not sent anything" there
                // would be a flat lie, so the count decides until every
                // reporting process has the field.
                if (stats.upDatagrams > 0 && routerSeesNothing) return RouterSeesNothing
                return if (stats.upDatagrams > 0) Carrying else Listening
            }
            val silence = nowMs - inbound
            if (silence > quietAfterMs) return RouterQuiet(silence)
            // Inbound proves the router is talking. It does NOT prove this
            // phone got anything out over cellular, and "Carrying" while every
            // upstream send fails is the same fabrication in the other
            // direction.
            // THE CONTRADICTION OUTRANKS THE LOCAL COUNTERS, and only here.
            // upDatagrams > 0 means this phone forwarded something; the router
            // saying nothing ever arrived means it did not land. Reporting the
            // phone's half alone is what produced "Carrying" over a household
            // outage (#281).
            if (stats.upDatagrams > 0 && routerSeesNothing) return RouterSeesNothing
            return if (stats.upDatagrams > 0) Carrying else NotForwarding(stats.lastError)
        }

        /** A duration a person reads at a glance. Same bucketing as
         *  ZippieCompanionKit/RelayVerdict.swift's `ago(_:)` so the same
         *  silence reads the same on both phones. */
        fun ago(ms: Long): String {
            val s = Math.round(ms / 1000.0)
            if (s < 120) return "${s}s"
            val m = s / 60
            if (m < 120) return "${m}m"
            return "${m / 60}h"
        }

        /** Every case, with representative payloads, so a copy rule can be
         *  asserted across all of them at once. */
        val ALL_CASES_FOR_COPY_REVIEW: List<RelayVerdict> = listOf(
            Off,
            NotReporting,
            Paused("Daily cap of 2 GB reached."),
            NotListening(null),
            NotListening("cannot listen on 51999: address in use"),
            NoCellular(null),
            NoCellular("cellular unavailable (interface not usable)"),
            Listening,
            RouterSeesNothing,
            NotForwarding(null),
            NotForwarding("up: no route to host"),
            RouterQuiet(40_000),
            Carrying,
        )
    }
}
