package app.zippie.companion

import android.content.Context
import android.net.ConnectivityManager
import android.net.DnsService
import android.os.Build
import androidx.annotation.RequiresApi

/**
 * The system DNS controller for Android Private DNS.
 *
 * Android 9 (API 28) introduced Private DNS mode, which uses DoT (DNS over TLS).
 * Android 11 (API 30) added setApplicationDnsService for per-app DoH.
 *
 * This uses ConnectivityManager.setApplicationDnsService (API 30+) because:
 * - It survives VPN restarts (unlike VPN DNS config)
 * - It works on both WiFi and cellular
 * - It's per-application, not system-wide
 *
 * The fallback is LegacyDNSSettingsManager which uses the older Private DNS hostname mode.
 */
interface DNSSettingsManaging {
    var isEnabled: Boolean
    var serverURL: String?

    suspend fun load()
    suspend fun save(serverURL: String)
    suspend fun remove()
}

/**
 * Android implementation using ConnectivityManager.setApplicationDnsService (API 30+).
 *
 * This is the primary implementation because it works on both WiFi and cellular
 * and survives VPN restarts.
 */
@RequiresApi(30)
class AndroidDNSSettingsManager(private val context: Context) : DNSSettingsManaging {
    private val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager

    override var isEnabled: Boolean = false
        private set

    override var serverURL: String? = null
        private set

    init {
        // Verify the API is available at runtime
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            throw UnsupportedOperationException("setApplicationDnsService requires API 30+")
        }
    }

    override suspend fun load() {
        val url = try {
            cm.applicationDnsService?.let { service ->
                service.hostname?.let { hostname ->
                    service.path?.let { path ->
                        if (path.isNotEmpty()) {
                            "https://$hostname$path"
                        } else {
                            "https://$hostname"
                        }
                    } ?: "https://$hostname"
                }
            }
        } catch (e: SecurityException) {
            null
        }
        serverURL = url
        isEnabled = url != null
    }

    override suspend fun save(serverURL: String) {
        val hostname = extractHostname(serverURL)
        if (hostname != null) {
            try {
                val service = object : DnsService {
                    override fun getHostname(): String = hostname
                    override fun getPort(): Int = 443
                    override fun getPath(): String? = "/dns-query"
                }
                cm.allNetworks.forEach { network ->
                    cm.bindProcessToNetwork(network)
                    try {
                        cm.setApplicationDnsService(service)
                    } finally {
                        cm.bindProcessToNetwork(null)
                    }
                }
                this.serverURL = serverURL
                isEnabled = true
            } catch (e: Exception) {
                isEnabled = false
                throw e
            }
        }
    }

    override suspend fun remove() {
        try {
            cm.allNetworks.forEach { network ->
                cm.bindProcessToNetwork(network)
                try {
                    cm.setApplicationDnsService(null)
                } finally {
                    cm.bindProcessToNetwork(null)
                }
            }
            serverURL = null
            isEnabled = false
        } catch (e: Exception) {
            isEnabled = false
            throw e
        }
    }

    private fun extractHostname(url: String): String? {
        return try {
            if (url.startsWith("https://")) {
                url.substring("https://".length).split("/").firstOrNull { it.isNotEmpty() }
            } else {
                null
            }
        } catch (e: Exception) {
            null
        }
    }
}

/**
 * Fallback implementation using Private DNS hostname mode (API 28+).
 *
 * This sets the system-wide Private DNS mode to "Custom hostname" with the
 * NextDNS profile hostname. This is the older approach but works on all
 * Android 9+ devices.
 */
@RequiresApi(28)
class LegacyDNSSettingsManager(private val context: Context) : DNSSettingsManaging {
    private val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager

    override var isEnabled: Boolean = false
        private set

    override var serverURL: String? = null
        private set

    init {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P) {
            throw UnsupportedOperationException("dnsNetwork requires API 28+")
        }
    }

    override suspend fun load() {
        val hostname = try {
            cm.dnsNetwork?.hostName
        } catch (e: SecurityException) {
            null
        }
        if (hostname != null && hostname.endsWith(".dns.nextdns.io")) {
            serverURL = "https://$hostname"
            isEnabled = true
        } else {
            isEnabled = false
        }
    }

    override suspend fun save(serverURL: String) {
        val hostname = extractHostnameForDot(serverURL)
        if (hostname != null) {
            try {
                cm.dnsNetwork?.let { network ->
                    cm.bindProcessToNetwork(network)
                    try {
                        cm.dnsNetwork = network
                    } finally {
                        cm.bindProcessToNetwork(null)
                    }
                }
                this.serverURL = serverURL
                isEnabled = true
            } catch (e: Exception) {
                isEnabled = false
                throw e
            }
        }
    }

    override suspend fun remove() {
        try {
            cm.dnsNetwork = null
            serverURL = null
            isEnabled = false
        } catch (e: Exception) {
            isEnabled = false
            throw e
        }
    }

    private fun extractHostnameForDot(url: String): String? {
        return try {
            if (url.startsWith("https://")) {
                val hostname = url.substring("https://".length).split("/").firstOrNull { it.isNotEmpty() }
                if (hostname?.startsWith("dns.nextdns.io/") == true) {
                    hostname.substring("dns.nextdns.io/".length) + ".dns.nextdns.io"
                } else {
                    hostname
                }
            } else {
                null
            }
        } catch (e: Exception) {
            null
        }
    }
}

/**
 * Test-friendly manager that doesn't require Android context.
 * Used for unit tests on the JVM.
 */
class MockDNSSettingsManager : DNSSettingsManaging {
    override var isEnabled: Boolean = false
        private set

    override var serverURL: String? = null
        private set

    override suspend fun load() {
        // No-op for tests
    }

    override suspend fun save(serverURL: String) {
        this.serverURL = serverURL
        isEnabled = serverURL.isNotEmpty()
    }

    override suspend fun remove() {
        serverURL = null
        isEnabled = false
    }
}
