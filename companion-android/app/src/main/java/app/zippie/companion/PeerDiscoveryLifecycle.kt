package app.zippie.companion

import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner

/**
 * Ties discovery to the app's foreground. Added to the process lifecycle,
 * it starts discovery on onStart and stops it on onStop, so NSD is not
 * browsing, and no listener stays registered, while the app is in the
 * background. The roster is read at each start, so a pairing made while
 * backgrounded is used on resume.
 */
class PeerDiscoveryLifecycle(
    private val discovery: PeerDiscovery,
    private val rosterJson: () -> String,
    private val onVerified: (VerifiedPeer) -> Unit,
    private val onRefused: (String, String) -> Unit,
    private val onLost: (String) -> Unit,
) : DefaultLifecycleObserver {

    override fun onStart(owner: LifecycleOwner) {
        discovery.start(rosterJson(), onVerified, onRefused, onLost)
    }

    override fun onStop(owner: LifecycleOwner) {
        discovery.stop()
    }
}
