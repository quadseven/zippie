package app.zippie.companion

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import androidx.annotation.RequiresApi

/**
 * The system DNS controller for Android Private DNS.
 *
 * Android 9 (API 28) introduced Private DNS mode, which uses DoT (DNS over TLS).
 * Android 10 (API 29) added setApplicationDnsService for per-app DoH.
 *
 * This uses ConnectivityManager.setApplicationDnsService (API 29+) because:
 * - It survives VPN restarts (unlike VPN DNS config)
 * - It works on both WiFi and cellular
 * - It's per-application, not system-wide
 *
 * The fallback is SystemDNSManager which uses the older Private DNS hostname mode.
 */
interface DNSSettingsManaging {
    var isEnabled: Boolean
    var serverURL: String?

    suspend fun load()
    suspend fun save(serverURL: String)
    suspend fun remove()
}

/**
 * Android implementation using ConnectivityManager.setApplicationDnsService (API 29+).
 *
 * This is the primary implementation because it works on both WiFi and cellular
 * and survives VPN restarts.
 */
@RequiresApi(29)
class AndroidDNSSettingsManager(private val context: Context) : DNSSettingsManaging {
    private val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager

    override var isEnabled: Boolean = false
        private set

    override var serverURL: String? = null
        private set

    override suspend fun load() {
        val url = try {
            val service = cm.applicationDnsService
            if (service != null) {
                service.domain?.let { "${service.protocol}://$it" }
            } else null
        } catch (e: SecurityException) {
            null
        }
        serverURL = url
        isEnabled = url != null
    }

    override suspend fun save(serverURL: String) {
        // Parse the URL to extract hostname for DoH
        val hostname = extractHostname(serverURL)
        if (hostname != null) {
            try {
                cm.allNetworks.forEach { network ->
                    cm.bindProcessToNetwork(network)
                    try {
                        cm.setApplicationDnsService(DnsService.createDohService(hostname))
                    } finally {
                        cm.bindProcessToNetwork(null)
                    }
                }
                this.serverURL = serverURL
                isEnabled = true
            } catch (e: Exception) {
                // If we can't set the DNS service, mark as disabled
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
    private val dnsManager = context.getSystemService(Context.DNS_MANAGER_SERVICE)

    override var isEnabled: Boolean = false
        private set

    override var serverURL: String? = null
        private set

    override suspend fun load() {
        // For legacy, we check if Private DNS is in use
        val hostname = dnsManager?.primaryDnsServer?.hostName
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
                // Set Private DNS mode to "Custom hostname"
                dnsManager?.setPrivateDnsMode(hostname)
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
            dnsManager?.setPrivateDnsMode(null)
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
                // Convert doh URL to dot hostname
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
 * Helper to parse and create DNS service for DoH.
 */
object DnsService {
    /**
     * Create a DoH service from a hostname.
     */
    fun createDohService(hostname: String): android.net.DnsService? {
        return android.net.DnsService.Builder(hostname)
            .setPort(443)
            .setProtocol(android.net.DnsService.PROTOCOL_HTTPS)
            .setPath("/dns-query")
            .build()
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
