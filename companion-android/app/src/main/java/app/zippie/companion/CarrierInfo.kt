package app.zippie.companion

import android.content.Context
import android.telephony.TelephonyManager

/**
 * Which carrier this phone's cellular leg is actually on.
 *
 * SOMETHING THE iOS APP CANNOT DO. Apple removed CTCarrier's carrier name in
 * iOS 16, so the iPhone companion can say only "cellular", and the bond's two
 * iPhone legs are labelled by hand in the router's config - a label that keeps
 * saying "Verizon" long after a SIM is swapped. Android reports it, so this
 * phone can state the fact instead of repeating a stale configuration.
 *
 * Needs no permission: getNetworkOperatorName and getSimOperatorName are both
 * outside the READ_PHONE_STATE gate. That matters for a phone somebody else
 * owns - a permission prompt to display a carrier name would not be worth it.
 */
data class CarrierInfo(
    /** The network the phone is REGISTERED on right now. Null when it is not
     *  registered anywhere - aeroplane mode, no coverage, no SIM. */
    val serving: String?,
    /** The SIM's home carrier. Present even with no service, so it separates
     *  "no SIM in this phone" from "SIM present but out of coverage" - two very
     *  different reasons for a relay that will not carry. */
    val sim: String?,
) {
    /**
     * One line for the screen, and null when nothing was measured. Never the
     * word "unknown" dressed as a carrier name: an empty operator string means
     * the radio did not answer, and printing it as a name would invent one.
     */
    val summary: String?
        get() = when {
            serving != null && sim != null && !serving.equals(sim, ignoreCase = true) ->
                "$serving (SIM: $sim)"
            serving != null -> serving
            sim != null -> "$sim - no network registered"
            else -> null
        }

    companion object {
        fun read(context: Context): CarrierInfo {
            val tm = context.getSystemService(TelephonyManager::class.java)
                ?: return CarrierInfo(null, null)
            return CarrierInfo(
                serving = tm.networkOperatorName?.trim()?.ifEmpty { null },
                sim = tm.simOperatorName?.trim()?.ifEmpty { null },
            )
        }
    }
}
