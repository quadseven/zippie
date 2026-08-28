package app.zippie.companion

import org.json.JSONException
import org.json.JSONObject

/**
 * A snapshot of the zippie bond, decoded from the router's /api/status.
 *
 * Read-only and defensive, for the same reason BondStatus.swift is: the console
 * is a developer surface that changes shape as the agent evolves, and an app
 * that crashes because a field was renamed is worse than one that says it does
 * not know. EVERY FIELD IS NULLABLE, and null means "the router did not tell
 * us", which the UI must render as unknown rather than as zero.
 *
 * org.json rather than a reflection-based decoder on purpose. Its optInt/optLong
 * accessors return 0 for a missing key, which is precisely the failure this
 * type exists to prevent, so nothing here calls them - see [intOrNull] and
 * friends below.
 */
data class BondStatus(
    val mode: String?,
    val datapath: String?,
    val primary: String?,
    /**
     * Null means the payload had no `paths` key at all - a shape we do not
     * understand. An empty list means the router genuinely lists no links.
     * Those are different facts and the screen says different things about them.
     */
    val paths: List<Path>?,
) {
    data class Path(
        val name: String?,
        /** What a human called this link. `name` is an internal id and a poor
         *  thing to read on a dashboard at speed. */
        val label: String?,
        val state: String?,
        /**
         * The hard failover gate. A leg on a higher tier carries NOTHING while
         * a lower tier is alive - it is held in reserve, not broken, and those
         * two look identical from outside unless the UI is told which.
         */
        val tier: Int?,
        val effectiveWeight: Int?,
        val rttMs: Double?,
        val lossPct: Double?,
        /**
         * The legacy per-leg counters. In `datapath: "packet"` these stay at 0
         * on every leg while traffic flows - see [carriedTxBytes].
         */
        val txBytes: Long?,
        val rxBytes: Long?,
        /**
         * The packet datapath's own per-link counters. Verified against the live
         * router 2026-08-05: hotspot reported tx_bytes 0 / rx_bytes 0 while
         * link_tx_bytes 11018643 and link_rx_bytes 31703754, and the transport's
         * reassembly counter agreed with the latter pair.
         */
        val linkTxBytes: Long?,
        val linkRxBytes: Long?,
        /** Absent when the leg is configured but not physically present - an
         *  unplugged dongle. */
        val interfaceName: String?,
        /** The router's own explanation, e.g. "awaiting transport". Worth
         *  surfacing verbatim: it is the difference between a diagnosis and a
         *  red dot. */
        val lastError: String?,
        /** The leg has transmitted and has NEVER been answered (#204). Not the
         *  same as degraded: that is a leg which used to work and got worse.
         *  This one is pointed at something that is not listening. */
        val neverHandshaked: Boolean?,
        /** Usable uplinks this leg's pattern matched and no leg took (#212) -
         *  a link that is working and invisible. */
        val shadowedInterfaces: List<String>?,
        /**
         * For companion legs only: the address:port the router dials to reach
         * that phone. A phone matches this against its own wifi address and
         * listen port to know which leg is itself - see LegIdentity.
         */
        val relayEndpoint: String?,
        /** A deliberate ceiling in kbit/s; 0 or absent means uncapped. Without
         *  this a throttled leg reads as a slow one. */
        val maxKbps: Int?,
        val costClass: String?,
        val monthlyCapGb: Double?,
        val usageGb: Double?,
        val overSoftLimit: Boolean?,
        /**
         * Whether the transport actually holds a link for this leg.
         *
         * NOT THE SAME AS HAVING WEIGHT, and that difference is why this field
         * exists. A tier-gated leg keeps whatever weight the policy last
         * computed - the number is real, it is simply not being used - so
         * deciding "carrying" from weight showed four legs carrying while the
         * transport held exactly one. That was a real bug on iOS.
         */
        val inBond: Boolean?,
    ) {
        /**
         * Bytes this leg has actually moved, or null when nothing measured it.
         *
         * Prefers the packet datapath's link counters because the older
         * tx_bytes/rx_bytes pair is hard 0 in that mode: reading those would
         * draw a leg that has carried 30 MB as having carried nothing, which is
         * the exact class of lie this app exists to avoid.
         */
        val carriedTxBytes: Long? get() = linkTxBytes ?: txBytes
        val carriedRxBytes: Long? get() = linkRxBytes ?: rxBytes

        /**
         * Carrying means the transport holds a link for it AND it has weight.
         *
         * Weight alone is not enough - see [inBond]. A router too old to publish
         * membership falls back to weight, which is the previous behaviour and
         * wrong only in the tier-gated case that older agent could not produce.
         */
        val isCarrying: Boolean
            get() = (effectiveWeight ?: 0) > 0 && (inBond ?: true)

        /**
         * Held back by the tier gate rather than failed.
         *
         * THE DISTINCTION THE WHOLE STATUS SCREEN TURNS ON. A reserve leg
         * reports no traffic because it is DOING ITS JOB - a cheap SIM kept for
         * the day everything else is down. Drawing it the same as a broken leg
         * trains the reader to ignore the one signal that matters.
         *
         * Decided against the ACTIVE tier, not against tier 1: if the bond has
         * already fallen to tier 2, a tier-2 leg is live and only tier 3 is
         * still in reserve.
         */
        fun isHeldInReserve(activeTier: Int?): Boolean {
            if (tier == null || activeTier == null) return false
            return tier > activeTier
        }

        /** True when this leg is a phone rather than a physical uplink. */
        val isCompanion: Boolean get() = !relayEndpoint.isNullOrEmpty()

        /** Present in the bond at all. A path with neither an interface nor a
         *  relay endpoint is configuration, not a connection. */
        val isPresent: Boolean get() = !interfaceName.isNullOrEmpty() || isCompanion

        /**
         * The state in ONE WORD, because "is it degraded, or is it one of two"
         * was not answerable from the screen otherwise.
         */
        val stateWord: String
            get() {
                if (isCarrying) return if (state == "degraded") "carrying, degraded" else "carrying"
                if (inBond == false) return "not in the bond"
                return when (state) {
                    "up" -> "up, not carrying"
                    "degraded" -> "degraded"
                    "down" -> "down"
                    else -> "idle"
                }
            }
    }

    /**
     * The lowest tier with a carrying leg - the tier the bond is running on.
     * Null when nothing carries at all, which is a different and much worse
     * state than "running on a lower tier".
     */
    val activeTier: Int?
        get() = (paths ?: emptyList()).filter { it.isCarrying }.mapNotNull { it.tier }.minOrNull()

    val carryingCount: Int get() = (paths ?: emptyList()).count { it.isCarrying }
    val totalCount: Int get() = (paths ?: emptyList()).size

    companion object {
        /**
         * Throws [JSONException] on a payload that is not JSON at all. A body
         * that parses but carries none of the fields we expect decodes to a
         * BondStatus full of nulls, which the UI renders as "the router
         * answered with something we do not understand" - still better than a
         * crash, and it cannot be mistaken for a healthy bond.
         */
        @Throws(JSONException::class)
        fun decode(payload: String): BondStatus {
            val root = JSONObject(payload)
            val pathsArray = if (root.isNull("paths")) null else root.optJSONArray("paths")
            val paths = pathsArray?.let { array ->
                (0 until array.length()).mapNotNull { i ->
                    array.optJSONObject(i)?.let(::decodePath)
                }
            }
            return BondStatus(
                mode = root.stringOrNull("mode"),
                datapath = root.stringOrNull("datapath"),
                primary = root.stringOrNull("primary"),
                paths = paths,
            )
        }

        private fun decodePath(o: JSONObject) = Path(
            name = o.stringOrNull("name"),
            label = o.stringOrNull("label"),
            state = o.stringOrNull("state"),
            tier = o.intOrNull("tier"),
            effectiveWeight = o.intOrNull("effective_weight"),
            rttMs = o.doubleOrNull("rtt_ms"),
            lossPct = o.doubleOrNull("loss_pct"),
            txBytes = o.longOrNull("tx_bytes"),
            rxBytes = o.longOrNull("rx_bytes"),
            linkTxBytes = o.longOrNull("link_tx_bytes"),
            linkRxBytes = o.longOrNull("link_rx_bytes"),
            interfaceName = o.stringOrNull("interface"),
            lastError = o.stringOrNull("last_error"),
            // ABSENT-SAFE. A router that predates these fields simply does not
            // send them, and must render exactly as it does today.
            neverHandshaked = if (o.has("never_handshaked")) o.optBoolean("never_handshaked") else null,
            shadowedInterfaces = o.optJSONArray("shadowed_interfaces")?.let { arr ->
                (0 until arr.length()).mapNotNull { arr.optString(it).takeIf { s -> s.isNotEmpty() } }
            },
            relayEndpoint = o.stringOrNull("relay_endpoint"),
            maxKbps = o.intOrNull("max_kbps"),
            costClass = o.stringOrNull("cost_class"),
            monthlyCapGb = o.doubleOrNull("monthly_cap_gb"),
            usageGb = o.doubleOrNull("usage_gb"),
            overSoftLimit = o.booleanOrNull("over_soft_limit"),
            inBond = o.booleanOrNull("in_bond"),
        )
    }
}

// The accessors below all answer "absent" with null instead of with a zero or a
// false. org.json's opt* family answers with 0 / false / "" and that is how an
// unmeasured RTT becomes a confident "0 ms" on screen.

internal fun JSONObject.stringOrNull(key: String): String? {
    if (!has(key) || isNull(key)) return null
    val s = optString(key, "")
    return s.ifEmpty { null }
}

internal fun JSONObject.intOrNull(key: String): Int? {
    if (!has(key) || isNull(key)) return null
    return try {
        getInt(key)
    } catch (e: JSONException) {
        null
    }
}

internal fun JSONObject.longOrNull(key: String): Long? {
    if (!has(key) || isNull(key)) return null
    return try {
        getLong(key)
    } catch (e: JSONException) {
        null
    }
}

internal fun JSONObject.doubleOrNull(key: String): Double? {
    if (!has(key) || isNull(key)) return null
    return try {
        getDouble(key)
    } catch (e: JSONException) {
        null
    }
}

internal fun JSONObject.booleanOrNull(key: String): Boolean? {
    if (!has(key) || isNull(key)) return null
    return try {
        getBoolean(key)
    } catch (e: JSONException) {
        null
    }
}
