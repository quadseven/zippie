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
) {
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
            else -> "The router is not answering from here."
        }

    val label: String
        get() = when {
            undetermined -> "Checking"
            mode == BondMode.CONTRIBUTE -> "Contributing"
            else -> "Client"
        }
}
