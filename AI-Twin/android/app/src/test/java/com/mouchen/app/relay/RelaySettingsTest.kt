package com.mouchen.app.relay

import org.junit.Assert.*
import org.junit.Test

class RelaySettingsTest {
    private fun settings() = RelaySettings("https://relay.example.com", "phone-a", "computer-a", "synthetic-device-token-only")
    @Test fun legacyPrivateConfigCanBeImportedWithoutAssumingTarget() {
        val value = decodeImportedSettings("""{"url":"https://relay.example.com/","device":"phone-a","token":"synthetic-device-token-only"}""")
        assertEquals("phone-a", value.device)
        assertEquals("", value.peer)
        assertFalse(value.enabled)
    }
    @Test fun portableConfigImportsDeviceIdAndTarget() {
        val value = decodeImportedSettings("""{"url":"https://relay.example.com","device_id":"phone-a","targets":["computer-a"],"token":"synthetic-device-token-only"}""")
        assertEquals("computer-a", value.peer)
        validateSettings(value)
    }
    @Test fun unsafeDestinationsAndSelfTargetAreRejected() {
        for (url in listOf("http://relay.example.com", "https://user:pass@relay.example.com", "https://relay.example.com/path", "https://relay.example.com?x=1")) {
            assertThrows(IllegalArgumentException::class.java) { validateSettings(settings().copy(url = url)) }
        }
        assertThrows(IllegalArgumentException::class.java) { validateSettings(settings().copy(peer = "phone-a")) }
    }
    @Test fun differentPeerOrOriginNeverSharesHistoryPartition() {
        val first = settings()
        assertNotEquals(first.partition, first.copy(peer = "computer-b").partition)
        assertNotEquals(first.partition, first.copy(url = "https://other.example.com").partition)
        assertEquals(first.partition, first.copy(token = "replacement-synthetic-token").partition)
        assertEquals(first.partition, first.copy(url = "https://RELAY.example.com:443/").partition)
        assertFalse(first.toString().contains(first.token))
    }
    @Test fun tokenFitsServerAuthorizationLimit() {
        validateSettings(settings().copy(token = "x".repeat(249)))
        assertThrows(IllegalArgumentException::class.java) { validateSettings(settings().copy(token = "x".repeat(250))) }
        assertThrows(IllegalArgumentException::class.java) { validateSettings(settings().copy(token = "非ASCII令牌不能写入HTTP头")) }
    }
}
