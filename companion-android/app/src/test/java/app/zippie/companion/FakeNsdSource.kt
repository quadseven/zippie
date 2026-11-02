package app.zippie.companion

/**
 * In-memory NSD source for unit tests. It records what the discovery asked
 * of it (discovery running, resolves in flight) and lets a test play NSD
 * events back through the listener the discovery registered.
 */
class FakeNsdSource : NsdSource {

    var listener: NsdSource.Listener? = null
        private set
    var discoverCalls = 0
        private set
    var stopCalls = 0
        private set
    val activeResolves = mutableSetOf<String>()
    val cancelledResolves = mutableListOf<String>()

    val isDiscovering: Boolean
        get() = listener != null

    override fun discover(serviceType: String, listener: NsdSource.Listener) {
        check(this.listener == null) { "discover called while a listener is still registered" }
        this.listener = listener
        discoverCalls++
    }

    override fun stopDiscovery() {
        listener = null
        stopCalls++
    }

    override fun resolve(serviceName: String) {
        activeResolves.add(serviceName)
    }

    override fun cancelResolve(serviceName: String) {
        if (activeResolves.remove(serviceName)) cancelledResolves.add(serviceName)
    }

    fun found(name: String) {
        listener?.onServiceFound(name)
    }

    fun resolved(name: String, txt: Map<String, String>) {
        activeResolves.remove(name)
        listener?.onServiceResolved(name, txt.mapValues { it.value.toByteArray(Charsets.UTF_8) })
    }

    fun resolveFailed(name: String, errorCode: Int) {
        activeResolves.remove(name)
        listener?.onResolveFailed(name, errorCode)
    }

    fun lost(name: String) {
        listener?.onServiceLost(name)
    }

    fun discoveryFailed(errorCode: Int) {
        listener?.onDiscoveryFailed(errorCode)
    }

    /** Found then resolved in one step, the common happy path. */
    fun announce(name: String, txt: Map<String, String>) {
        found(name)
        resolved(name, txt)
    }
}
