package app.zippie.companion

/**
 * Why this phone is, or is not, reachable.
 *
 * WHY THIS TYPE EXISTS (#131). On 2026-08-11 the managed Pixel went silent for
 * half an hour and the app could say nothing useful. Nothing was broken: the
 * phone had moved from the travel router's wifi to the house VLAN to take a 3GB
 * OS update on unmetered bandwidth, which is the correct thing to do. But the
 * MDM lives on the tailnet and the phone has no Tailscale of its own - it had
 * only ever reached the tailnet because the travel router forwards and
 * masquerades for its LAN. On a network that does not do that, the route simply
 * does not exist.
 *
 * The screen said "Standing by". True, useless, and indistinguishable from four
 * other faults seen the same night: a stale write token 401'ing every sixteen
 * seconds in silence, a DHCP server handing out no resolver at all, an
 * `http://` URL that 308-redirected to https with a client that would not
 * follow, and a relay answering nothing on its own port.
 *
 * This mirrors Diagnostics.swift deliberately. The two apps report the same
 * facts in the same words, because the operator debugging at 2am should not
 * have to learn two vocabularies for one bond.
 */
sealed interface DiagnosticFailure {
    /** DHCP gave an address and named no resolver. A real fault here: nextdns
     *  took port 53 and dnsmasq stopped advertising itself, so every client got
     *  an address it could not use. */
    object NoResolverOffered : DiagnosticFailure
    data class NameNotResolved(val name: String) : DiagnosticFailure
    data class TimedOut(val seconds: Int) : DiagnosticFailure
    data class Tls(val detail: String) : DiagnosticFailure
    /** The status is carried because a 401 and a 404 send you to different
     *  places. */
    data class Http(val status: Int) : DiagnosticFailure
    /** The router refused a write and said why. Since the agent started logging
     *  refusals, the phone can repeat the reason instead of guessing. */
    data class Refused(val reason: String) : DiagnosticFailure
    /** Resolved fine, nothing to route to. */
    object NoRoute : DiagnosticFailure

    val summary: String
        get() = when (this) {
            is NoResolverOffered -> "this network offered no DNS server"
            is NameNotResolved -> "could not resolve $name"
            is TimedOut -> "timed out after ${seconds}s"
            is Tls -> "TLS failed: $detail"
            is Http -> "HTTP $status"
            is Refused -> "refused: $reason"
            is NoRoute -> "no route from this network"
        }
}

/**
 * A measured fact, or an honest admission that it was not measured.
 *
 * NotChecked is NOT a failure and must never render as one. A screen that
 * paints unchecked rows red teaches the reader to ignore red; one that paints
 * them green lies. It is a third state because it is a third state.
 */
sealed interface DiagnosticState {
    object NotChecked : DiagnosticState
    data class Ok(val detail: String? = null) : DiagnosticState
    data class Failed(val failure: DiagnosticFailure) : DiagnosticState

    val isOk: Boolean get() = this is Ok
}

/**
 * How this phone reaches the tailnet, which is not a yes/no question.
 *
 * DIRECT AND VIA-ROUTER ARE DIFFERENT STATES WITH DIFFERENT FIXES, and
 * collapsing them into one green dot is how the blackout stayed confusing. A
 * phone reaching the tailnet through a router that forwards for it is one SSID
 * change away from losing everything; a phone with its own Tailscale is not.
 */
sealed interface TailnetPath {
    object NotChecked : TailnetPath
    data class Direct(val nodeName: String? = null) : TailnetPath
    data class ViaRouter(val host: String) : TailnetPath
    data class Unreachable(val failure: DiagnosticFailure) : TailnetPath

    /** Whether losing this network also loses management. The honest answer for
     *  ViaRouter is yes, and it is the most useful thing this screen can tell
     *  somebody about to walk out of the house. */
    val survivesLeavingThisNetwork: Boolean get() = this is Direct
}

/**
 * What DHCP said about DNS, which has THREE answers and not two.
 *
 * A nullable string was wrong here. "This network offered no resolver" is a
 * genuine, serious fault - it is what took the house wifi down. "This platform
 * will not tell us" is not a fault at all. Rendering the second as the first
 * puts a red row on a healthy phone, which is the fastest way to make somebody
 * stop reading the screen.
 *
 * Android CAN answer this (LinkProperties.dnsServers), unlike iOS - which is
 * exactly why the type must be able to express both, or the shared vocabulary
 * breaks at the platform boundary.
 */
sealed interface ResolverFact {
    object Unknown : ResolverFact
    object None : ResolverFact
    data class Address(val value: String) : ResolverFact
}

enum class Tone { GOOD, BAD, UNKNOWN, NOTE }

data class DiagnosticRow(
    val label: String,
    val value: String,
    val tone: Tone,
    /** Present only when there is something to do about it. */
    val hint: String? = null,
)

/** Everything measured, and when. */
data class Diagnostics(
    val legName: String? = null,
    val carrying: Boolean = false,
    val lastAnnounce: DiagnosticState = DiagnosticState.NotChecked,
    val mdm: DiagnosticState = DiagnosticState.NotChecked,
    val tailnet: TailnetPath = TailnetPath.NotChecked,
    val ssid: String? = null,
    val dhcpResolver: ResolverFact = ResolverFact.Unknown,
    val captive: DiagnosticState = DiagnosticState.NotChecked,
    val lastCheckInEpochMs: Long? = null,
    val bytesCarried: Long? = null,
    val measuredAtEpochMs: Long? = null,
) {
    /**
     * The single sentence at the top.
     *
     * ORDERED BY WHAT BLOCKS WHAT. No resolver means nothing else can work, so
     * it is reported first even though the MDM row is also red - reporting the
     * symptom above the cause is how a DNS fault got diagnosed as a wifi fault
     * for several hours.
     */
    val headline: String
        get() {
            if (captive is DiagnosticState.Failed &&
                captive.failure is DiagnosticFailure.NoResolverOffered
            ) return "This network has no DNS"
            if (mdm is DiagnosticState.Failed &&
                mdm.failure is DiagnosticFailure.NoResolverOffered
            ) return "This network has no DNS"
            if (tailnet is TailnetPath.Unreachable) return "Cannot reach the tailnet"
            if (mdm is DiagnosticState.Failed) return "Cannot reach the MDM - ${mdm.failure.summary}"
            if (lastAnnounce is DiagnosticState.Failed) {
                return "The router refused this phone - ${lastAnnounce.failure.summary}"
            }
            if (carrying) return "Carrying"
            if (tailnet is TailnetPath.ViaRouter) return "Reachable, but only on this network"
            return "Standing by"
        }

    fun rows(nowMs: Long = System.currentTimeMillis()): List<DiagnosticRow> {
        val out = mutableListOf<DiagnosticRow>()

        out += DiagnosticRow(
            label = "Bond",
            value = if (carrying) "carrying" else "not carrying",
            tone = if (carrying) Tone.GOOD else Tone.UNKNOWN,
            hint = legName?.let { "known to the router as $it" },
        )
        out += row("Last announce", lastAnnounce, "accepted", announceHint(lastAnnounce))
        out += row("MDM", mdm, "reachable")
        out += tailnetRow()
        out += DiagnosticRow(
            label = "Network",
            value = ssid ?: "unknown",
            tone = resolverTone(),
            hint = resolverHint(),
        )
        out += row("Captive check", captive, "passes")

        if (lastCheckInEpochMs != null) {
            val ageS = ((nowMs - lastCheckInEpochMs) / 1000).toInt()
            out += DiagnosticRow(
                label = "Last check-in",
                value = if (ageS < 90) "just now" else "${ageS / 60} min ago",
                tone = if (ageS < 600) Tone.GOOD else Tone.BAD,
                hint = if (ageS >= 600) "the server has not heard from this phone" else null,
            )
        } else {
            out += DiagnosticRow("Last check-in", "never", Tone.UNKNOWN)
        }

        bytesCarried?.let {
            out += DiagnosticRow("Carried this session", humanBytes(it), Tone.NOTE)
        }

        // Last, and always present when known. A screen of measurements with no
        // measurement time is the thing this type exists to prevent.
        measuredAtEpochMs?.let {
            val ageS = ((nowMs - it) / 1000).toInt()
            out += DiagnosticRow(
                label = "Measured",
                value = if (ageS < 5) "just now" else "${ageS}s ago",
                tone = if (ageS > 60) Tone.UNKNOWN else Tone.NOTE,
                hint = if (ageS > 60) "tap refresh - these may have moved" else null,
            )
        }
        return out
    }

    private fun tailnetRow(): DiagnosticRow = when (tailnet) {
        is TailnetPath.NotChecked ->
            DiagnosticRow("Tailnet", "not checked", Tone.UNKNOWN)
        is TailnetPath.Direct ->
            DiagnosticRow(
                "Tailnet", "direct", Tone.GOOD,
                tailnet.nodeName?.let { "this phone is $it" } ?: "works on any network",
            )
        // Deliberately NOT GOOD. It works, and it stops working the moment this
        // phone changes network - which is exactly what happened.
        is TailnetPath.ViaRouter ->
            DiagnosticRow(
                "Tailnet", "via ${tailnet.host}", Tone.NOTE,
                "only on this network - leaving it loses the MDM",
            )
        is TailnetPath.Unreachable ->
            DiagnosticRow(
                "Tailnet", tailnet.failure.summary, Tone.BAD,
                "install Tailscale on this phone to fix it everywhere",
            )
    }

    private fun resolverHint(): String? = when (dhcpResolver) {
        is ResolverFact.Unknown -> null
        is ResolverFact.None -> "this network offered no DNS server"
        is ResolverFact.Address -> "DNS from DHCP: ${dhcpResolver.value}"
    }

    private fun resolverTone(): Tone = when (dhcpResolver) {
        is ResolverFact.None -> Tone.BAD
        is ResolverFact.Address -> Tone.NOTE
        is ResolverFact.Unknown -> if (ssid == null) Tone.UNKNOWN else Tone.NOTE
    }

    private fun announceHint(s: DiagnosticState): String? =
        if (s is DiagnosticState.Failed && s.failure is DiagnosticFailure.Refused) {
            "store the router's write token in this app"
        } else {
            null
        }

    private fun row(
        label: String,
        state: DiagnosticState,
        okDefault: String,
        hint: String? = null,
    ): DiagnosticRow = when (state) {
        is DiagnosticState.NotChecked -> DiagnosticRow(label, "not checked", Tone.UNKNOWN, hint)
        is DiagnosticState.Ok -> DiagnosticRow(label, state.detail ?: okDefault, Tone.GOOD, hint)
        is DiagnosticState.Failed -> DiagnosticRow(label, state.failure.summary, Tone.BAD, hint)
    }

    companion object {
        fun humanBytes(n: Long): String {
            if (n < 1024) return "$n B"
            val units = listOf("KB", "MB", "GB", "TB")
            var v = n.toDouble() / 1024.0
            var i = 0
            while (v >= 1024 && i < units.size - 1) { v /= 1024; i++ }
            return if (v < 10) String.format("%.1f %s", v, units[i])
            else String.format("%.0f %s", v, units[i])
        }
    }
}
