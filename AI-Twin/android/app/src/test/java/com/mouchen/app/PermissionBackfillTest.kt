package com.mouchen.app

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PermissionBackfillTest {
    @Test
    fun firstResumeReliesOnStartupCollection() {
        assertFalse(shouldBackfillAfterPermissionRefresh(null, 0b001))
    }

    @Test
    fun newlyGrantedAccessTriggersBackfill() {
        assertTrue(shouldBackfillAfterPermissionRefresh(0b001, 0b101))
    }

    @Test
    fun revocationOrNoChangeDoesNotTriggerBackfill() {
        assertFalse(shouldBackfillAfterPermissionRefresh(0b101, 0b001))
        assertFalse(shouldBackfillAfterPermissionRefresh(0b101, 0b101))
    }

    @Test
    fun anyPermissionChangeTriggersCollectionForBackfillOrRevocationCleanup() {
        assertFalse(shouldCollectAfterPermissionRefresh(null, 0b001))
        assertTrue(shouldCollectAfterPermissionRefresh(0b001, 0b101))
        assertTrue(shouldCollectAfterPermissionRefresh(0b101, 0b001))
        assertFalse(shouldCollectAfterPermissionRefresh(0b101, 0b101))
    }
}
