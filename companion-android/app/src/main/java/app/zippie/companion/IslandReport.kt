package app.zippie.companion

/**
 * What this phone can honestly say about the ROUTER, over a path the router
 * does not own.
 *
 * THE HOLE THIS FILLS (zippie#286). Every other instrument reads a metric the
 * router emits, or reaches it over the tailnet. Both need the router to have
 * internet, and the bond IS the router's internet - so the one failure that
 * matters most, an islanded router with no uplink at all, is invisible to all
 * of them by construction. Two outages this week, 598 minutes and ~90 minutes,
 * were found by a human rather than by an alert.
 *
 * A relay phone is the only observer with both properties needed: it sits
 * OUTSIDE the router's failure domain, and it has its OWN connectivity. During
 * an islanding the router's LAN keeps working perfectly - the console answers
 * on the wifi the whole time - while its WAN is dead. So the phone can read the
 * router's own verdict over wifi and ship it out over cellular. It becomes the
 * router's voice at exactly the moment the router has none.
 *
 * WHY THE PHONE CANNOT JUST USE ITS OWN COUNTERS. It tried, and it was wrong
 * for hours. On 2026-08-23 both handsets had healthy local numbers - 284
 * datagrams forwarded, 536 back down - while the router reported those same
 * legs `never_handshaked` with `rx 0`. The phone counts what it SENT; only the
 * router knows what ARRIVED (#281). So the useful thing to ship is not this
 * phone's opinion, it is the router's, carried by this phone.
 *
 * THE FALSE ALARM THIS MUST NOT PRODUCE, and it is most of the design: the
 * phone is away from the router most of the time. A signal that fires every
 * time somebody leaves the house is worse than no signal and will be muted
 * within a week. [NOT_NEAR_ROUTER] exists for that and is not an error state -
 * it is the honest answer to "what do you know about the router", which when
 * you are three miles away is "nothing".
 */
enum class IslandState {
    /**
     * This phone's relay is not up, so nothing it says about the router is
     * evidence about the router - only about itself.
     */
    RELAY_NOT_READY,

    /**
     * The console did not answer on the LAN. Either this phone is elsewhere,
     * which is the normal case, or the router is off.
     *
     * DELIBERATELY NOT AN ALERT. Those two cannot be told apart from here, and
     * treating them as one is the mistake that makes an alarm unreadable - the
     * same reason the hub's monitors do not alert on `reachable=0`.
     */
    NOT_NEAR_ROUTER,

    /** The router answered on the LAN and says it has a carrying leg. */
    ROUTER_CARRYING,

    /**
     * The router answered on the LAN and says NOTHING is carrying.
     *
     * THIS IS THE ISLANDING SIGNAL. The router is powered, its agent is
     * running, its LAN works well enough to answer this phone - and it has no
     * uplink. That is a household with no internet, reported from a device the
     * outage cannot silence.
     */
    ROUTER_NOT_CARRYING,
    ;

    /** Stable, lowercase, facetable. Shipped as a field rather than parsed out
     *  of a message, so a monitor is a filter and not a regex. */
    val wireValue: String get() = name.lowercase()
}

object IslandReport {

    /**
     * @param relayListening whether this phone holds its listening socket.
     * @param consoleAnsweredOnLan whether the console replied over WIFI. Not
     *   over the tailnet: a tailnet read proves the router has internet, which
     *   is the very thing in question, and would make this signal impossible
     *   exactly when it is needed.
     * @param carryingLegs the router's own count, or null if it did not answer.
     */
    fun evaluate(
        relayListening: Boolean,
        consoleAnsweredOnLan: Boolean,
        carryingLegs: Int?,
    ): IslandState {
        // FIRST, because a phone whose own relay is down is not a witness. It
        // would report NOT_NEAR_ROUTER from the sofa and look like a departure.
        if (!relayListening) return IslandState.RELAY_NOT_READY
        if (!consoleAnsweredOnLan || carryingLegs == null) return IslandState.NOT_NEAR_ROUTER
        return if (carryingLegs > 0) IslandState.ROUTER_CARRYING else IslandState.ROUTER_NOT_CARRYING
    }
}
