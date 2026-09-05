package com.mouchen.app.network

import com.mouchen.app.BuildConfig
import java.net.InetAddress
import okhttp3.Dns

/**
 * Bypasses unreliable OEM MagicDNS handling for one explicitly pinned private node. The request
 * URL keeps its hostname, so TLS SNI and certificate verification are unchanged. All other names
 * are delegated without alteration.
 */
internal class ExactHostPinnedDns(
    private val systemDns: Dns,
    pinnedHost: String,
    pinnedIpv4: String,
) : Dns {
    private val normalizedPinnedHost = pinnedHost.trim().trimEnd('.').lowercase()
    private val pinnedAddress = strictIpv4Address(pinnedIpv4)

    override fun lookup(hostname: String): List<InetAddress> {
        val normalizedHostname = hostname.trim().trimEnd('.').lowercase()
        if (normalizedPinnedHost.isNotEmpty() &&
            normalizedHostname == normalizedPinnedHost &&
            pinnedAddress != null
        ) {
            return listOf(pinnedAddress)
        }
        return systemDns.lookup(hostname)
    }
}

/** Shared by every private-backend client; unrelated hosts use Android's resolver unchanged. */
internal object MouchenDns : Dns {
    private val delegate: Dns by lazy {
        ExactHostPinnedDns(
            systemDns = Dns.SYSTEM,
            pinnedHost = BuildConfig.PRIVATE_DNS_PINNED_HOST,
            pinnedIpv4 = BuildConfig.PRIVATE_DNS_PINNED_IPV4,
        )
    }

    override fun lookup(hostname: String): List<InetAddress> = delegate.lookup(hostname)
}

private fun strictIpv4Address(value: String): InetAddress? {
    val labels = value.trim().split('.')
    if (labels.size != 4) return null
    val octets = labels.map { label ->
        if (label.isEmpty() || (label.length > 1 && label.startsWith('0'))) return null
        label.toIntOrNull()?.takeIf { it in 0..255 } ?: return null
    }
    return InetAddress.getByAddress(octets.map(Int::toByte).toByteArray())
}
