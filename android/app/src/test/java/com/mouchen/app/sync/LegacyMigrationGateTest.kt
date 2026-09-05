package com.mouchen.app.sync

import org.junit.After
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LegacyMigrationGateTest {
    @After
    fun resetProcessGate() {
        LegacyMigrationRuntimeGate.reset()
    }

    @Test
    fun commercialSessionNeverNeedsLegacyDecision() {
        val commercial = session(legacy = false)

        assertTrue(LegacyMigrationRuntimeGate.allows(commercial))
        assertFalse(LegacyMigrationRuntimeGate.requiresDecision(commercial))
    }

    @Test
    fun temporaryCompatibilityApprovalIsExactAndProcessLocal() {
        val legacy = session(legacy = true)

        assertTrue(LegacyMigrationRuntimeGate.requiresDecision(legacy))
        assertTrue(LegacyMigrationRuntimeGate.allowForThisProcess(legacy))
        assertTrue(LegacyMigrationRuntimeGate.allows(legacy))
        assertFalse(LegacyMigrationRuntimeGate.allows(legacy.copy(epoch = legacy.epoch + 1)))
        assertFalse(LegacyMigrationRuntimeGate.allows(legacy.copy(serverOrigin = "https://other.example.com")))

        LegacyMigrationRuntimeGate.reset()
        assertTrue(LegacyMigrationRuntimeGate.requiresDecision(legacy))
    }

    @Test
    fun invalidLegacySessionCannotReceiveTemporaryApproval() {
        val invalid = session(legacy = true).copy(serverOrigin = "http://unsafe.example.com")

        assertFalse(LegacyMigrationRuntimeGate.allows(invalid))
        assertFalse(LegacyMigrationRuntimeGate.allowForThisProcess(invalid))
    }

    @Test
    fun claimAcceptsOnlyTheExistingOwnerIdentityOnTheFixedOrigin() {
        val legacy = session(legacy = true)
        val canonicalOwner = session(legacy = false).copy(accessToken = "new-token", epoch = 0, serverOrigin = "")

        assertTrue(legacyClaimMatchesOwner(legacy, canonicalOwner))
        assertFalse(legacyClaimMatchesOwner(legacy, canonicalOwner.copy(userId = "another-user")))
        assertFalse(
            legacyClaimMatchesOwner(
                legacy,
                canonicalOwner.copy(serverOrigin = "https://other.example.com"),
            ),
        )
    }

    @Test
    fun claimTransactionRejectsAnyStaleLegacyState() {
        val expected = session(legacy = true)

        assertTrue(legacyClaimStateIsCurrent(expected, expected, expected.epoch))
        assertFalse(legacyClaimStateIsCurrent(expected, expected.copy(accessToken = "changed"), expected.epoch))
        assertFalse(legacyClaimStateIsCurrent(expected, expected.copy(userId = "another-user"), expected.epoch))
        assertFalse(legacyClaimStateIsCurrent(expected, expected.copy(epoch = expected.epoch + 1), expected.epoch + 1))
        assertFalse(legacyClaimStateIsCurrent(expected, expected, expected.epoch + 1))
        assertFalse(
            legacyClaimStateIsCurrent(
                expected,
                expected.copy(serverOrigin = "https://other.example.com"),
                expected.epoch,
            ),
        )
    }

    private fun session(legacy: Boolean): AuthSession = AuthSession(
        userId = "legacy-owner-id",
        username = "owner",
        accessToken = "legacy-token",
        sessionId = "legacy-session",
        deviceId = "android-device",
        legacy = legacy,
        serverOrigin = "https://mouchen.example.com",
        epoch = 9L,
    )
}
