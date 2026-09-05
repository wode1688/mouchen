package com.mouchen.app.sync

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AccountConsentStoreTest {
    @Test
    fun commercialAccountDefaultsToNoCollectionAndNoCloudAnalysis() {
        val consent = commercialAccountConsentDefault()

        assertFalse(consent.collectionEnabled)
        assertFalse(consent.cloudAnalysisEnabled)
        assertFalse(consent.fullContextCloudEnabled)
    }

    @Test
    fun legacyOwnerKeepsExistingCollectionAndCloudChoices() {
        val existing = BackendConnection(
            baseUrl = "https://mouchen.example.com",
            enabled = true,
            minimizedContextOnly = false,
            proactiveCloudEnabled = true,
        )

        val consent = legacyOwnerConsent(existing)

        assertTrue(consent.collectionEnabled)
        assertTrue(consent.cloudAnalysisEnabled)
        assertTrue(consent.fullContextCloudEnabled)
    }

    @Test
    fun accountConsentKeysAreSeparatedAndDoNotExposeUserId() {
        val first = accountConsentKey("server-user-1", "https://one.example.com")
        val second = accountConsentKey("server-user-2", "https://one.example.com")
        val otherOrigin = accountConsentKey("server-user-1", "https://two.example.com")

        assertFalse(first.contains("server-user-1"))
        assertFalse(second.contains("server-user-2"))
        assertTrue(first != second)
        assertTrue(first != otherOrigin)
    }

    @Test
    fun consentCodecKeepsIndependentCollectionAndCloudChoices() {
        val collectionOnly = decodeAccountConsent(
            encodeAccountConsent(AccountConsent(collectionEnabled = true, cloudAnalysisEnabled = false)),
        )
        val cloudOnly = decodeAccountConsent(
            encodeAccountConsent(AccountConsent(collectionEnabled = false, cloudAnalysisEnabled = true)),
        )

        assertTrue(collectionOnly.collectionEnabled)
        assertFalse(collectionOnly.cloudAnalysisEnabled)
        assertFalse(collectionOnly.fullContextCloudEnabled)
        assertFalse(cloudOnly.collectionEnabled)
        assertTrue(cloudOnly.cloudAnalysisEnabled)
    }

    @Test
    fun commercialCloudGateDefaultsOffAndRequiresSeparateFullContextConsent() {
        val globallyFull = BackendConnection(
            baseUrl = "https://mouchen.example.com",
            minimizedContextOnly = false,
            proactiveCloudEnabled = true,
        )

        val defaulted = applyAccountCloudConsent(globallyFull, AccountConsent())
        val minimized = applyAccountCloudConsent(
            globallyFull,
            AccountConsent(cloudAnalysisEnabled = true),
        )
        val full = applyAccountCloudConsent(
            globallyFull,
            AccountConsent(cloudAnalysisEnabled = true, fullContextCloudEnabled = true),
        )

        assertFalse(defaulted.proactiveCloudEnabled)
        assertTrue(defaulted.minimizedContextOnly)
        assertTrue(minimized.proactiveCloudEnabled)
        assertTrue(minimized.minimizedContextOnly)
        assertTrue(full.proactiveCloudEnabled)
        assertFalse(full.minimizedContextOnly)
    }
}
