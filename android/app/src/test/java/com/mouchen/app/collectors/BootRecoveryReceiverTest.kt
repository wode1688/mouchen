package com.mouchen.app.collectors

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class BootRecoveryReceiverTest {
    @Test
    fun supportedSystemActionsRestoreCollection() {
        assertEquals("boot_completed", recoveryTrigger("android.intent.action.BOOT_COMPLETED"))
        assertEquals("package_replaced", recoveryTrigger("android.intent.action.MY_PACKAGE_REPLACED"))
        assertEquals("user_unlocked", recoveryTrigger("android.intent.action.USER_UNLOCKED"))
        assertEquals("user_present", recoveryTrigger("android.intent.action.USER_PRESENT"))
    }

    @Test
    fun unrelatedBroadcastDoesNothing() {
        assertNull(recoveryTrigger("android.intent.action.TIME_SET"))
        assertNull(recoveryTrigger(null))
    }
}
