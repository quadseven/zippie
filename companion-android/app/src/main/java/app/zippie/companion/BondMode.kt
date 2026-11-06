package app.zippie.companion

/**
 * Which job this phone is doing right now.
 *
 * ONE APP, TWO DIRECTIONS (ADR 0022), and the phone chooses rather than the
 * person:
 *
 *   contribute - on the router's own network, LEND this phone's cellular to the
 *                bond. The phone's own traffic is untouched.
 *   client     - anywhere else, BOND this phone's wifi and cellular back home.
 *
 * These are close to opposites, and running the wrong one is not cosmetic:
 * contributing while away from the router holds a cellular socket open for a
 * bond that cannot hear it, and client mode on the router's own wifi would bond
 * a link that is already part of the bond.
 */
enum class BondMode { CONTRIBUTE, CLIENT }

/**
 * How the router was found, which is what decides the mode.
 *
 * THE DISTINCTION THAT MATTERS: reachable and NEARBY are different facts. The
 * console answers over the tailnet from anywhere in the world, so "the router
 * replied" proves the router is alive and proves nothing about where this phone
 * is. Only an answer on the LOCAL address means we are on its network.
 *
 * Android could instead read the current SSID, which is what the iOS app uses
 * for its on-demand rule. It is not used here: SSID visibility is gated behind
 * ACCESS_FINE_LOCATION, a name is not evidence (any cafe can call its wifi
 * anything), and the console probe answers the question directly.
 */
enum class RouterProximity { LOCAL, REMOTE, UNREACHABLE }

/**
 * Why the router did not answer (#154). Mirrors `UnreachableReason` in the iOS
 * Kit.
 *
 * OFF THE ROUTER'S LAN THE ONLY PATH IS THE TAILNET. The tailnet name resolves
 * publicly to a CGNAT tailnet address that only Tailscale can route, so a phone
 * without Tailscale gets no DNS error and no refusal, just a timeout. That
 * timeout looked exactly like a router that is down, and the two have different
 * fixes.
 */
enum class UnreachableReason(val logName: String) {
    /** A tailnet console is configured and this phone holds no tailnet address. */
    NO_TAILNET_ROUTE("no_tailnet_route"),

    /** The phone had a route, or no tailnet console was configured, and still
     *  nothing answered. */
    NOT_ANSWERING("console_timeout"),
    ;

    companion object {
        /**
         * Decide from this device's own addresses. Range-checked with
         * [DiagnosticsMeasurer.isTailnetV4], so a carrier CGNAT address just
         * outside the tailnet range is never read as a route. With no tailnet
         * console configured the tailnet is irrelevant, and blaming it would
         * send the reader to the wrong fix.
         */
        fun decide(hasRemoteCandidate: Boolean, ownAddresses: List<String>): UnreachableReason = when {
            !hasRemoteCandidate -> NOT_ANSWERING
            ownAddresses.any { DiagnosticsMeasurer.isTailnetV4(it) } -> NOT_ANSWERING
            else -> NO_TAILNET_ROUTE
        }
    }
}

/**
 * The mode decision, with the reason attached.
 *
 * The reason travels WITH the verdict rather than being reconstructed by the
 * UI. "Why is this phone in client mode" is the hardest question this app has
 * to answer, and an enum on its own cannot answer it.
 */
data class ModeDecision(
    val proximity: RouterProximity,
    /** True while the phone has not yet looked. Distinct from "looked and found
     *  nothing", because showing a confident mode before the first probe is a
     *  claim made without evidence. */
    val undetermined: Boolean = false,
    /** Defaults to the meaning UNREACHABLE had before #154, so a caller that has
     *  not checked the tailnet never claims the phone is off it. Read it through
     *  [reason], which is null whenever the router was reached. */
    val unreachableReason: UnreachableReason = UnreachableReason.NOT_ANSWERING,
) {
    /** Why the router did not answer, or null when it did or before the first
     *  look. */
    val reason: UnreachableReason?
        get() = if (proximity == RouterProximity.UNREACHABLE && !undetermined) unreachableReason else null

    /**
     * CLIENT IS THE DEFAULT, and that asymmetry is deliberate. Contributing
     * requires positive proof that the router is on this network; being wrong
     * there spends metered data on a bond that cannot hear us. Being wrong the
     * other way just means the phone bonds its own links home.
     */
    val mode: BondMode
        get() = if (proximity == RouterProximity.LOCAL) BondMode.CONTRIBUTE else BondMode.CLIENT

    val summary: String
        get() = when {
            undetermined -> "Working out which network this is."
            proximity == RouterProximity.LOCAL ->
                "On the router's network - lending this phone's cellular to the bond."
            proximity == RouterProximity.REMOTE ->
                "Away from the router - this phone is not contributing to the bond."
            reason == UnreachableReason.NO_TAILNET_ROUTE ->
                "This phone is not on the tailnet, so the router cannot be reached from here."
            else -> "The router is not answering from here."
        }

    val label: String
        get() = when {
            undetermined -> "Checking"
            mode == BondMode.CONTRIBUTE -> "Contributing"
            else -> "Client"
        }
}
