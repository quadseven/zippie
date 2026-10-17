package app.zippie.companion

/**
 * What a home-screen widget renders, already decided (quadseven/zippie#244).
 *
 * THE WIDGET DERIVES NOTHING. Everything below comes from [RelayVerdict], the
 * same state machine the app's own screens read, and this file is the ONE place
 * that turns a report into a renderable shape. The Glance composable's whole job
 * is to lay these fields out - see `RelayWidget.kt`.
 *
 * That rule is not style. #44 shipped a screen that computed its own sentence
 * from `cellularReady` - a fact about this phone's own radio - and told an
 * operator it was "Connected to the router" while the router had never dialled
 * it. Two surfaces deriving one claim was already one too many; a widget would
 * be the worst third, because it is the surface people believe without opening
 * anything.
 *
 * STALENESS IS HANDLED BY CONSTRUCTION, which is the property most likely to be
 * broken by a later edit. A widget redraws on the launcher's schedule, not ours,
 * so the default failure is showing "Carrying" long after the relay died.
 * [RelayVerdict.evaluate] already returns [RelayVerdict.NotReporting] for a
 * report past [RelayReport.STALENESS_MS], so going through it on every build is
 * what makes a stale widget honest. Do not cache a previous result and do not
 * shortcut around evaluate - `WidgetContentTest` fails if either happens.
 *
 * TWO RECORDS, TWO THRESHOLDS, and the bond's belongs to
 * [WidgetBondSnapshot]. The relay report is written every two seconds by the
 * relay's own heartbeat; the bond is fetched by the APP every five seconds and
 * stops advancing the moment the app is backgrounded. One shared age would be
 * wrong in both directions, so [from] gates each independently and a stale bond
 * degrades to the this-phone row rather than to the legs it happened to hold.
 */
data class WidgetContent(
    val headline: String,
    val detail: String,
    val tone: Tone,
    val legs: List<Leg>,
) {
    /** The three things a status colour may mean, and no more.
     *
     *  Maps onto the design tokens' `live` / `tertiary` / `down`. The palette
     *  has exactly one accent and it means TRAFFIC IS MOVING RIGHT NOW; a
     *  widget that spent it on decoration would throw away the only
     *  unambiguous signal the product has. */
    enum class Tone { LIVE, IDLE, DOWN }

    /**
     * One row in the medium widget's leg list.
     *
     * Two sources reach this and they are genuinely different kinds of claim,
     * which is why [State.IDLE] and [State.PRESENT_NOT_CARRYING] are separate:
     * the router's own legs carry words the ROUTER chose, while the this-phone
     * row is built from counters this phone kept by itself and cannot speak for
     * the far end.
     *
     * The full vocabulary is here rather than [LegState]'s five because a
     * widget draws four rows in a column with no room for a nuance - and
     * `reserveIsNotBroken` cannot be honoured by a list with nowhere to put
     * "held in reserve".
     */
    data class Leg(val label: String, val state: State) {
        enum class State {
            /** Carrying traffic right now. The only state the accent is spent on. */
            CARRYING,

            /**
             * In the bond's own view this leg is up and holding weight, but it
             * is NOT carrying. `weightIsNotMembership`: a leg with weight the
             * transport does not hold is not carrying, and the anti-flap gate
             * holds recovered legs at weight 0 while they prove themselves.
             *
             * KEPT SEPARATE FROM DOWN ON PURPOSE. "It is connected but
             * delivering nothing" is the failure this product exists to surface,
             * and collapsing it into the failure colour destroys it.
             */
            PRESENT_NOT_CARRYING,

            /** The router is explicit that it holds no link for this leg
             *  (`in_bond == false`). Its own decision, not a health verdict. */
            NOT_IN_BOND,

            /** Held back by the tier gate. `reserveIsNotBroken`: a leg withheld
             *  because a cheaper tier is alive is DOING ITS JOB, and drawing
             *  that as a failure trains the reader to ignore the one signal
             *  that matters. */
            RESERVE,

            /** A genuine fault on this leg. */
            DOWN,

            /** This phone's own fallback row, with nothing measured about the
             *  far end. */
            IDLE,
        }

        /**
         * The one word a reader scans, decided HERE rather than in a composable.
         *
         * THE STRINGS ARE THE SHARED VOCABULARY in `design/tokens.json`'s `state`
         * block, emitted by `design/generate.py` as `Tok.StateWord`. Restated
         * rather than imported so this file keeps zero framework imports and can
         * be proven on the JVM; `WidgetPaletteWiringTest` is what holds the two
         * platforms to the same words.
         */
        val word: String
            get() = when (state) {
                State.CARRYING -> "carrying"
                State.PRESENT_NOT_CARRYING -> "up, not carrying"
                State.NOT_IN_BOND -> "not in the bond"
                State.RESERVE -> "held in reserve"
                State.DOWN -> "down"
                State.IDLE -> "not carrying"
            }
    }

    companion object {
        /** What this phone's own leg is called when there is nothing better.
         *  The router names legs; a widget on the phone is looking at itself. */
        const val THIS_PHONE = "This phone"

        /**
         * Build from the only evidence a widget process can read on its own:
         * the persisted report ([RelayStatusStore.readPersisted]) and, when one
         * has been published recently enough to mean anything, the persisted
         * bond ([RelayBondStore.read]).
         *
         * `report == null` is NOT an error and not a zeroed reading - it is
         * "nothing is running", which [RelayStatusStore] documents as
         * deliberately different from a report full of zeroes.
         */
        fun from(
            report: RelayReport?,
            nowMs: Long = System.currentTimeMillis(),
            routerName: String? = null,
            bond: WidgetBondSnapshot? = null,
            routerSeesNothing: Boolean = false,
            routerReachable: Boolean = true,
        ): WidgetContent {
            val verdict = RelayVerdict.evaluate(
                report, nowMs,
                routerSeesNothing = routerSeesNothing,
                routerReachable = routerReachable,
            )
            return WidgetContent(
                headline = verdict.headline,
                detail = verdict.detail(routerName),
                tone = toneOf(verdict),
                legs = legsOf(verdict, bond, nowMs),
            )
        }

        /** Exhaustive on purpose - `when` over a sealed class with no `else`
         *  means a new verdict is a COMPILE error here rather than a silent
         *  fallback to whichever tone seemed safest.
         *
         *  NOT PRIVATE. StatusScreen's this-phone fallback row - the single
         *  leg it draws when the router's console cannot be reached, matching
         *  BondModel.swift's `rebuild()` - needs the exact same judgement.
         *  Re-deriving it there would be a second switch that could drift from
         *  this one, which is how #267 happened to [Leg.isCarrying] in the
         *  first place. */
        fun toneOf(verdict: RelayVerdict): Tone = when (verdict) {
            RelayVerdict.Carrying -> Tone.LIVE
            // Listening and RouterQuiet are not faults. The phone is doing its
            // job and has heard nothing; drawing that red would be crying wolf,
            // which in a car is worse than saying nothing.
            RelayVerdict.Listening -> Tone.IDLE
            is RelayVerdict.RouterQuiet -> Tone.IDLE
            RelayVerdict.Off -> Tone.DOWN
            RelayVerdict.NotReporting -> Tone.DOWN
            is RelayVerdict.Paused -> Tone.DOWN
            is RelayVerdict.NotListening -> Tone.DOWN
            is RelayVerdict.NoCellular -> Tone.DOWN
            is RelayVerdict.NotForwarding -> Tone.DOWN
            // DOWN, and emphatically not LIVE. The accent means TRAFFIC IS
            // MOVING RIGHT NOW, and the whole point of this verdict is that the
            // router says none is arriving. Spending the accent here would
            // reproduce #281 in the one surface a driver glances at rather than
            // reads - the phone's counters are moving, which is exactly what
            // made the claim look true for hours.
            RelayVerdict.RouterSeesNothing -> Tone.DOWN
            RelayVerdict.RouterUnreachable -> Tone.DOWN
            // DOWN, not IDLE. Android may freeze this relay with the screen
            // off, so any "carrying" the counters suggest cannot be trusted.
            is RelayVerdict.BatteryOptimizationRestricted -> Tone.DOWN
        }

        /**
         * The leg list.
         *
         * ORDER IS THE ARGUMENT, and it is not cosmetic. The router's own bond
         * is strictly more informative than the one row this phone could build
         * about itself, so a FRESH snapshot wins outright and REPLACES that row
         * rather than joining it - this phone's own leg is already in the
         * router's list, and printing it twice would be a worse lie than either
         * version alone.
         *
         * The "nothing measured" verdicts come first and short-circuit
         * everything, including a perfectly fresh bond: a widget whose relay has
         * stopped checking in has no standing to describe the bond it was last
         * able to see, and `neverStateUnmeasured` applies to the whole list.
         *
         * EMPTY WHEN NOTHING IS RUNNING, rather than a row saying "down". Off
         * means there is no leg to describe, and inventing one to fill the
         * space would state something unmeasured.
         */
        private fun legsOf(
            verdict: RelayVerdict,
            bond: WidgetBondSnapshot?,
            nowMs: Long,
        ): List<Leg> = when (verdict) {
            RelayVerdict.Off, RelayVerdict.NotReporting -> emptyList()
            else -> {
                val rows = freshBondRows(bond, nowMs)
                if (rows.isNotEmpty()) rows else legsOfThisPhone(verdict)
            }
        }

        /** A bond snapshot only counts while it is inside its own threshold. A
         *  fresh one that resolves to no rows is still an absence, not a licence
         *  to fall through to describing something else. */
        private fun freshBondRows(bond: WidgetBondSnapshot?, nowMs: Long): List<Leg> =
            if (bond == null || bond.isStale(nowMs)) emptyList() else legsOfBond(bond.status)

        /** The one row a widget can build without the router. Matches
         *  `BondModel`'s own single-phone fallback exactly, so the screen and
         *  the widget can never describe this phone differently. */
        private fun legsOfThisPhone(verdict: RelayVerdict): List<Leg> = when (verdict) {
            RelayVerdict.Carrying -> listOf(Leg(THIS_PHONE, Leg.State.CARRYING))
            RelayVerdict.Listening, is RelayVerdict.RouterQuiet ->
                listOf(Leg(THIS_PHONE, Leg.State.IDLE))
            else -> listOf(Leg(THIS_PHONE, Leg.State.DOWN))
        }

        /** The router's legs, in its own order, through the same precedence
         *  [BondLegs] uses on the Status screen. SHARED RATHER THAN RE-DERIVED
         *  is the point: a widget disagreeing with the screen it summarises
         *  about which legs are in reserve would be worse than no widget at all,
         *  because it would be believed. */
        internal fun legsOfBond(status: BondStatus): List<Leg> {
            val activeTier = status.activeTier
            return (status.paths ?: emptyList())
                .filter { it.isPresent }
                .map { Leg(labelOf(it), stateOf(it, activeTier)) }
        }

        /** `weightIsNotMembership` AND `reserveIsNotBroken`, in [BondLegs]'s own
         *  order, because the order is the argument. */
        internal fun stateOf(p: BondStatus.Path, activeTier: Int?): Leg.State {
            // Reserve beats every other reading: a tier-gated leg with no
            // traffic is being deliberately withheld, not idle and not broken.
            if (BondLegs.isReserve(p, activeTier)) return Leg.State.RESERVE
            // Carrying is Path.isCarrying, never weight alone - that getter
            // already encodes both halves of weightIsNotMembership.
            if (p.isCarrying) return Leg.State.CARRYING
            if (p.inBond == false) return Leg.State.NOT_IN_BOND
            if (p.state == "down") return Leg.State.DOWN
            return Leg.State.PRESENT_NOT_CARRYING
        }

        /** What a human called this link. `label` is the display name and `name`
         *  an internal id, a poor thing to read at a glance; a leg with neither
         *  still draws a row, because a gap in the middle of a list reads as a
         *  rendering fault rather than as an absence. */
        internal fun labelOf(p: BondStatus.Path): String =
            p.label?.takeIf { it.isNotEmpty() }
                ?: p.name?.takeIf { it.isNotEmpty() }
                ?: "Unnamed leg"
    }
}
