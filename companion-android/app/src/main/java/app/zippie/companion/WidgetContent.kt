package app.zippie.companion

/**
 * What a home-screen widget renders, already decided (quadseven/zippie#244).
 *
 * THE WIDGET DERIVES NOTHING. Everything below comes from [RelayVerdict], the
 * same state machine the app's own screens read, and this file is the ONE place
 * that turns a report into a renderable shape. The Glance composable's whole job
 * is to lay these fields out.
 *
 * That rule is not style. #44 shipped a screen that computed its own sentence
 * from `cellularReady` - a fact about this phone's own radio - and told an
 * operator it was "Connected to the router" while the router had never dialled
 * it. Two surfaces deriving one claim was already one too many; a widget would
 * be the worst third, because it is the surface people believe without opening
 * anything.
 *
 * STALENESS IS HANDLED BY CONSTRUCTION, which is the property most likely to be
 * broken by a later edit. A widget redraws on WidgetKit's - or the launcher's -
 * schedule, not ours, so the default failure is showing "Carrying" long after
 * the relay died. [RelayVerdict.evaluate] already returns [RelayVerdict.NotReporting]
 * for a report past [RelayReport.STALENESS_MS], so going through it on every
 * build is what makes a stale widget honest. Do not cache a previous result and
 * do not shortcut around evaluate - `WidgetContentTest` fails if either happens.
 */
data class WidgetContent(
    val headline: String,
    val detail: String,
    val tone: Tone,
    val legs: List<Leg>,
) {
    /** The three things a status colour may mean, and no more.
     *
     *  Maps onto the design tokens' `live` / `secondary` / `down`. The palette
     *  has exactly one accent and it means TRAFFIC IS MOVING RIGHT NOW; a
     *  widget that spent it on decoration would throw away the only
     *  unambiguous signal the product has. */
    enum class Tone { LIVE, IDLE, DOWN }

    /**
     * One row in the medium widget's leg list.
     *
     * DELIBERATELY THREE STATES, not the five [LegState] has. `RESERVE` and
     * `DEGRADED` are properties of the ROUTER's view of the bond - tier and
     * weight, which live on BondStatus - and a widget process cannot see them:
     * BondStatus is fetched on the app's foreground timer, so anything
     * persisted from it would usually be many minutes old by the time a widget
     * is asked to redraw, and the staleness rule above would correctly suppress
     * it on nearly every render. Shipping it anyway would be code that is
     * tested and never exercised.
     *
     * So the list carries only what [RelayStats] can back with evidence written
     * continuously by the relay itself. Wiring the router's full bond view in is
     * real follow-on work.
     */
    data class Leg(val label: String, val state: State) {
        enum class State { CARRYING, DOWN, IDLE }
    }

    companion object {
        /** What this phone's own leg is called when there is nothing better.
         *  The router names legs; a widget on the phone is looking at itself. */
        const val THIS_PHONE = "This phone"

        /**
         * Build from the only evidence a widget process can read on its own:
         * the persisted report ([RelayStatusStore.readPersisted]).
         *
         * `report == null` is NOT an error and not a zeroed reading - it is
         * "nothing is running", which [RelayStatusStore] documents as
         * deliberately different from a report full of zeroes.
         */
        fun from(
            report: RelayReport?,
            nowMs: Long = System.currentTimeMillis(),
            routerName: String? = null,
        ): WidgetContent {
            val verdict = RelayVerdict.evaluate(report, nowMs)
            return WidgetContent(
                headline = verdict.headline,
                detail = verdict.detail(routerName),
                tone = toneOf(verdict),
                legs = legsOf(verdict),
            )
        }

        /** Exhaustive on purpose - `when` over a sealed class with no `else`
         *  means a new verdict is a COMPILE error here rather than a silent
         *  fallback to whichever tone seemed safest. */
        private fun toneOf(verdict: RelayVerdict): Tone = when (verdict) {
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
        }

        /**
         * The leg list.
         *
         * EMPTY WHEN NOTHING IS RUNNING, rather than a row saying "down". Off
         * means there is no leg to describe, and inventing one to fill the
         * space would state something unmeasured - the rule `tokens.json` calls
         * neverStateUnmeasured.
         */
        private fun legsOf(verdict: RelayVerdict): List<Leg> = when (verdict) {
            RelayVerdict.Off, RelayVerdict.NotReporting -> emptyList()
            RelayVerdict.Carrying -> listOf(Leg(THIS_PHONE, Leg.State.CARRYING))
            RelayVerdict.Listening, is RelayVerdict.RouterQuiet ->
                listOf(Leg(THIS_PHONE, Leg.State.IDLE))
            else -> listOf(Leg(THIS_PHONE, Leg.State.DOWN))
        }
    }
}
