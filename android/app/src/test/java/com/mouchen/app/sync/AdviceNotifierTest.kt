package com.mouchen.app.sync

import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AdviceNotifierTest {
    @Test
    fun android13RequiresRuntimePermission() {
        assertFalse(
            notificationEligibility(
                sdkInt = 33,
                postPermissionGranted = false,
                appNotificationsEnabled = true,
                channelImportance = 3,
            ),
        )
    }

    @Test
    fun globallyDisabledNotificationsAreIneligible() {
        assertFalse(
            notificationEligibility(
                sdkInt = 35,
                postPermissionGranted = true,
                appNotificationsEnabled = false,
                channelImportance = 3,
            ),
        )
    }

    @Test
    fun disabledOrMissingTargetChannelIsIneligible() {
        assertFalse(notificationEligibility(35, true, true, channelImportance = 0))
        assertFalse(notificationEligibility(35, true, true, channelImportance = null))
    }

    @Test
    fun enabledTargetChannelIsEligible() {
        assertTrue(notificationEligibility(35, true, true, channelImportance = 3))
    }

    @Test
    fun preChannelAndroidOnlyNeedsGlobalNotificationAccess() {
        assertTrue(notificationEligibility(25, false, true, channelImportance = null))
    }

    @Test
    fun followUpUsesANotificationIdThatCannotReplaceOriginalAdvice() {
        val adviceId = "7b240867-736a-489d-8367-3151dd446c20"

        assertNotEquals(adviceNotificationId(adviceId), adviceFollowUpNotificationId(adviceId))
        assertNotEquals(adviceNotificationId(adviceId, 1), adviceNotificationId(adviceId, 2))
        assertNotEquals(adviceNotificationId(adviceId, 2), adviceFollowUpNotificationId(adviceId))
    }
}
