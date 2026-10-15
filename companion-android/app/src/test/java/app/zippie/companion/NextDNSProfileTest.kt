package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class NextDNSProfileTest {

    @Test
    fun `the dohURL carries profile and device name`() {
        val p = NextDNSProfile("abc123", "operator-iphone")
        assertEquals("https://dns.nextdns.io/abc123/operator-iphone", p.dohURL)
    }

    @Test
    fun `a profile with no device name still resolves`() {
        val p = NextDNSProfile("abc123")
        assertEquals("https://dns.nextdns.io/abc123", p.dohURL)
    }

    @Test
    fun `two users get genuinely different endpoints`() {
        val operatorProfile = NextDNSProfile("aaa111", "operator-iphone")
        val coOperatorProfile = NextDNSProfile("bbb222", "co-operator-iphone")
        assert(operatorProfile.dohURL != coOperatorProfile.dohURL)
    }

    @Test
    fun `an invalid profile id produces no url at all`() {
        for (bad in listOf("", "ab", "has spaces", "WAY-too-long-to-be-a-profile-id", "a/b")) {
            val p = NextDNSProfile(bad, "x")
            assertFalse(p.isValid, "$bad was accepted")
            assertNull(p.dohURL, "$bad produced a URL")
        }
    }

    @Test
    fun `device names are sanitised not escaped`() {
        val p = NextDNSProfile("abc123", "Operator's iPhone 17/Pro")
        val url = p.dohURL ?: ""
        assertFalse(url.contains("%"), "name was percent-escaped: $url")
        assertFalse(url.contains("'"), url)
        assertTrue(url.startsWith("https://dns.nextdns.io/abc123/"), url)
    }

    @Test
    fun `trailing separators are trimmed`() {
        val p = NextDNSProfile("abc123", "!!!")
        assertEquals("https://dns.nextdns.io/abc123", p.dohURL)
    }

    @Test
    fun `dotHostname for Android Private DNS`() {
        assertEquals("abc123.dns.nextdns.io", NextDNSProfile("abc123").dotHostname)
        assertNull(NextDNSProfile("!!").dotHostname)
    }

    @Test
    fun `doHURL round trips the installed identity`() {
        val profile = NextDNSProfile("abc123", "operator-iphone")
        val parsed = NextDNSProfile.fromDohURL(profile.dohURL!!)
        assertEquals(profile, parsed)
    }

    @Test
    fun `another DoH provider is not mislabelled as NextDNS`() {
        val url = "https://dns.example.test/dns-query"
        assertNull(NextDNSProfile.fromDohURL(url))
    }

    @Test
    fun `empty profile ID is invalid`() {
        assertFalse(NextDNSProfile("", "x").isValid)
        assertNull(NextDNSProfile("", "x").dohURL)
    }

    @Test
    fun `profile ID must be 4-16 characters`() {
        for (len in 1..3) {
            val bad = "a".repeat(len)
            assertFalse(NextDNSProfile(bad, "x").isValid)
        }
        assertTrue(NextDNSProfile("abcd", "x").isValid)
        assertTrue(NextDNSProfile("abcdefghijklmnop", "x").isValid)
        assertFalse(NextDNSProfile("abcdefghijklmnopq", "x").isValid)
    }

    @Test
    fun `profile ID must be lowercase alphanumeric`() {
        assertFalse(NextDNSProfile("ABCD", "x").isValid)
        assertFalse(NextDNSProfile("abcd1234", "x").isValid)
        assertTrue(NextDNSProfile("abc123", "x").isValid)
    }
}
