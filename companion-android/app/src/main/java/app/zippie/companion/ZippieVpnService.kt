package app.zippie.companion

import android.content.Intent
import android.net.VpnService
import android.os.ParcelFileDescriptor
import android.util.Log

/**
 * CLIENT mode: capture THIS phone's traffic and bond its own wifi + cellular
 * back to the home lab. The iOS twin is NEPacketTunnelProvider configured to
 * capture (ADR 0022) - the opposite of the inert contributor tunnel.
 *
 * SKELETON. The datapath this needs is the gomobile build of the Go core
 * (#2246), which has never been produced on the authoring machine. The tunnel
 * lifecycle below is real; the packet loop is deliberately absent rather than
 * faked, because a loop that compiles and drops every packet is worse than one
 * that is obviously missing.
 *
 * Notes that are decisions, not placeholders:
 *
 *  - NO BYPASS/SPLIT-TUNNEL LIST. Zippie exits on the home lab's RESIDENTIAL
 *    address, so services do not flag it as a VPN the way they flag exits
 *    that land in a datacenter. addDisallowedApplication stays available if
 *    one service ever proves the exception.
 *  - setMetered(false): the bond is the phone's primary path in this mode, and
 *    marking it metered makes Android throttle background sync against a link
 *    that may be sitting on unmetered wifi.
 *  - Address space must not collide with the tailnet (100.64.0.0/10) OR with
 *    carrier CGNAT, which lives in the same range. See ADR 0022.
 */
class ZippieVpnService : VpnService() {

    companion object {
        private const val TAG = "ZippieVpn"
        /** TEST-NET-3. Chosen for the same reason iOS uses TEST-NET-1: an
         *  RFC1918 address here could collide with the very LAN the phone is
         *  joined to, and the router's own subnet is the likeliest collision. */
        private const val TUNNEL_ADDRESS = "203.0.113.2"
    }

    private var tunnel: ParcelFileDescriptor? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Validated and then deliberately not used: the tunnel below carries no
        // traffic until the datapath exists, so the configuration has nowhere to
        // go yet. Refusing an invalid one anyway keeps the failure in the same
        // place it will be once there IS a datapath to hand it to.
        AndroidManagedConfig.effectiveConfig(this).validated().getOrElse {
            Log.e(TAG, "refusing to start: ${it.message}")
            stopSelf()
            return START_NOT_STICKY
        }
        tunnel = establish()
        if (tunnel == null) {
            Log.e(TAG, "VpnService.establish returned null; consent may be missing")
            stopSelf()
            return START_NOT_STICKY
        }
        Log.w(TAG, "tunnel up, but NO DATAPATH IS ATTACHED - see #2246")
        return START_STICKY
    }

    private fun establish(): ParcelFileDescriptor? {
        val builder = Builder()
            .setSession("Zippie")
            .addAddress(TUNNEL_ADDRESS, 32)
            // Capture everything. This is what makes it CLIENT mode.
            .addRoute("0.0.0.0", 0)
            // The tailnet and the home site bands must resolve through home,
            // which is what lets a phone reach tailnet nodes while zippie holds
            // Android's (and iOS's) single tunnel slot - see #2252.
            .addRoute("100.64.0.0", 10)
            .setMtu(1280 - 29)   // smallest leg MTU minus the v3 header
            .setBlocking(false)
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.Q) {
            builder.setMetered(false)
        }
        return builder.establish()
    }

    override fun onDestroy() {
        tunnel?.close()
        tunnel = null
        super.onDestroy()
    }
}
