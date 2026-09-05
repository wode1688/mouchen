package com.mouchen.app.network

import java.net.InetAddress
import java.net.UnknownHostException
import okhttp3.Dns
import okhttp3.Request
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class PrivateNodeDnsTest {
    @Test
    fun exactPinnedHostReturnsLiteralAddressWithoutCallingSystemDns() {
        val systemDns = RecordingDns { throw AssertionError("pinned host reached system DNS") }
        val dns = ExactHostPinnedDns(systemDns, PINNED_HOST, PINNED_IPV4)

        assertEquals(PINNED_IPV4, dns.lookup(PINNED_HOST).single().hostAddress)
        assertTrue(systemDns.lookups.isEmpty())
    }

    @Test
    fun pinnedDnsNameMatchesCaseInsensitivelyWithOptionalRootDot() {
        val systemDns = RecordingDns { throw AssertionError("pinned host reached system DNS") }
        val dns = ExactHostPinnedDns(
            systemDns = systemDns,
            pinnedHost = "Node.Example.Invalid.",
            pinnedIpv4 = PINNED_IPV4,
        )

        assertEquals(PINNED_IPV4, dns.lookup("NODE.EXAMPLE.INVALID.").single().hostAddress)
        assertTrue(systemDns.lookups.isEmpty())
    }

    @Test
    fun publicAndLookalikeHostsAreDelegatedUnchanged() {
        val publicAddress = ipv4(192, 0, 2, 25)
        val lookalikeAddress = ipv4(198, 51, 100, 19)
        val systemDns = RecordingDns { host ->
            when (host) {
                "api.example.com" -> listOf(publicAddress)
                "$PINNED_HOST.evil.example" -> listOf(lookalikeAddress)
                else -> throw AssertionError("unexpected host $host")
            }
        }
        val dns = ExactHostPinnedDns(systemDns, PINNED_HOST, PINNED_IPV4)

        assertEquals(listOf(publicAddress), dns.lookup("api.example.com"))
        assertEquals(listOf(lookalikeAddress), dns.lookup("$PINNED_HOST.evil.example"))
        assertEquals(
            listOf("api.example.com", "$PINNED_HOST.evil.example"),
            systemDns.lookups,
        )
    }

    @Test
    fun delegatedFailureIsPropagatedWithoutReplacement() {
        val failure = UnknownHostException("public DNS failed")
        val systemDns = RecordingDns { throw failure }
        val dns = ExactHostPinnedDns(systemDns, PINNED_HOST, PINNED_IPV4)

        assertSame(failure, assertThrows(UnknownHostException::class.java) {
            dns.lookup("api.example.com")
        })
        assertEquals(listOf("api.example.com"), systemDns.lookups)
    }

    @Test
    fun invalidOrEmptyPinFailsClosedAndDelegatesTargetHost() {
        val invalidPins = listOf(
            "" to PINNED_IPV4,
            PINNED_HOST to "",
            PINNED_HOST to "fallback.example.com",
            PINNED_HOST to "100.064.000.010",
            PINNED_HOST to "100.64.0.999",
            PINNED_HOST to "100.68.131",
        )

        invalidPins.forEach { (host, address) ->
            val delegatedAddress = ipv4(203, 0, 113, 7)
            val systemDns = RecordingDns { listOf(delegatedAddress) }
            val dns = ExactHostPinnedDns(systemDns, host, address)

            assertEquals(listOf(delegatedAddress), dns.lookup(PINNED_HOST))
            assertEquals(listOf(PINNED_HOST), systemDns.lookups)
        }
    }

    @Test
    fun pinnedResolutionLeavesHttpsRequestHostForSniAndCertificateChecks() {
        val request = Request.Builder()
            .url("https://$PINNED_HOST/v1/events")
            .build()
        val systemDns = RecordingDns { throw AssertionError("pinned host reached system DNS") }
        val dns = ExactHostPinnedDns(systemDns, PINNED_HOST, PINNED_IPV4)

        assertEquals(PINNED_IPV4, dns.lookup(request.url.host).single().hostAddress)
        assertEquals(PINNED_HOST, request.url.host)
        assertEquals("https", request.url.scheme)
        assertTrue(systemDns.lookups.isEmpty())
    }

    private class RecordingDns(
        private val resolver: (String) -> List<InetAddress>,
    ) : Dns {
        val lookups = mutableListOf<String>()

        override fun lookup(hostname: String): List<InetAddress> {
            lookups += hostname
            return resolver(hostname)
        }
    }

    private companion object {
        const val PINNED_HOST = "node.example.invalid"
        const val PINNED_IPV4 = "203.0.113.10"

        fun ipv4(a: Int, b: Int, c: Int, d: Int): InetAddress =
            InetAddress.getByAddress(byteArrayOf(a.toByte(), b.toByte(), c.toByte(), d.toByte()))
    }
}
