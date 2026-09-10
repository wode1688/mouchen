package com.mouchen.app.sync

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AuthSecurityTest {
    @Test
    fun canonicalOriginDropsPathButKeepsPortAndRejectsUnsafeUrls() {
        assertEquals(
            "https://mouchen.example.com",
            canonicalHttpsOrigin("https://mouchen.example.com/api/v1?q=secret#fragment"),
        )
        assertEquals(
            "https://mouchen.example.com:8443",
            canonicalHttpsOrigin("https://mouchen.example.com:8443/path"),
        )
        assertNull(canonicalHttpsOrigin("http://mouchen.example.com"))
        assertNull(canonicalHttpsOrigin("https://owner:secret@mouchen.example.com"))
    }

    @Test
    fun sameOriginRequiresSchemeHostAndEffectivePortEquality() {
        assertTrue(sameHttpsOrigin("https://A.example.com/api", "https://a.example.com/other"))
        assertTrue(sameHttpsOrigin("https://a.example.com:443", "https://a.example.com"))
        assertFalse(sameHttpsOrigin("https://a.example.com", "https://b.example.com"))
        assertFalse(sameHttpsOrigin("https://a.example.com", "https://a.example.com:8443"))
        assertFalse(sameHttpsOrigin("https://a.example.com", "http://a.example.com"))
    }
}
