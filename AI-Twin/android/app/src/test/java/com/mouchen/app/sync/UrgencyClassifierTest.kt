package com.mouchen.app.sync

import com.mouchen.app.data.LocalEventEntity
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class UrgencyClassifierTest {
    @Test
    fun severeFirstPersonHealthSignalUsesFastLane() {
        assertEquals(
            UrgencyCategory.URGENT_HEALTH,
            UrgencyClassifier.classify(event("speech.transcript", "transcript", "我突然胸痛，喘不过气了")),
        )
    }

    @Test
    fun negatedHealthSignalDoesNotUseFastLane() {
        assertNull(
            UrgencyClassifier.classify(event("speech.transcript", "transcript", "我没有胸痛，也没有呼吸困难")),
        )
    }

    @Test
    fun suspiciousLoginNotificationUsesFastLane() {
        assertEquals(
            UrgencyCategory.ACCOUNT_SECURITY,
            UrgencyClassifier.classify(
                event("notification.posted", "text", "检测到异地登录，请确认是否为本人操作"),
            ),
        )
    }

    @Test
    fun resolvedSecurityAlertDoesNotUseFastLane() {
        assertNull(
            UrgencyClassifier.classify(
                event("notification.posted", "text", "异常登录已核实为本人操作"),
            ),
        )
    }

    @Test
    fun nearDeadlineInsideVisibleTextArrayUsesFastLane() {
        val event = LocalEventEntity(
            source = "android.accessibility",
            type = "ui.visible_text",
            payloadJson = JSONObject()
                .put("visible_text", JSONArray().put("项目提交").put("明天截止"))
                .toString(),
        )

        assertEquals(UrgencyCategory.DEADLINE, UrgencyClassifier.classify(event))
    }

    @Test
    fun ordinaryLoginAndFutureDateDoNotUseFastLane() {
        assertNull(UrgencyClassifier.classify(event("notification.posted", "text", "登录成功")))
        assertNull(UrgencyClassifier.classify(event("calendar.scheduled", "title", "下月项目评审")))
    }

    @Test
    fun englishDeadlineUsesWordBoundaries() {
        assertEquals(
            UrgencyCategory.DEADLINE,
            UrgencyClassifier.classify(event("notification.posted", "text", "Release deadline tomorrow")),
        )
        assertNull(
            UrgencyClassifier.classify(event("notification.posted", "text", "A deadline helper library")),
        )
    }

    @Test
    fun negationOnAnotherScreenLineDoesNotSuppressCurrentEmergency() {
        val event = LocalEventEntity(
            source = "android.accessibility",
            type = "ui.visible_text",
            payloadJson = JSONObject()
                .put(
                    "visible_text",
                    JSONArray()
                        .put("我没有胸痛")
                        .put("这是页面上的其他说明")
                        .put("我现在胸痛了，呼吸困难"),
                )
                .toString(),
        )

        assertEquals(UrgencyCategory.URGENT_HEALTH, UrgencyClassifier.classify(event))
    }

    @Test
    fun explicitOwnerSelfReportAlwaysUsesFastLane() {
        val event = LocalEventEntity(
            source = "android.self_report",
            type = "ui.visible_text",
            payloadJson = JSONObject()
                .put("visible_text", JSONArray().put("帮我分析今天的问题"))
                .put("context", "owner_self_report")
                .put("analysis_requested", true)
                .toString(),
        )

        assertEquals(UrgencyCategory.OWNER_REQUEST, UrgencyClassifier.classify(event))
    }

    @Test
    fun chineseResolvedClauseCannotSuppressNewProblemAfterTurn() {
        assertEquals(
            UrgencyCategory.URGENT_HEALTH,
            UrgencyClassifier.classify(
                event("speech.transcript", "transcript", "我没有胸痛，但现在呼吸困难"),
            ),
        )
        assertEquals(
            UrgencyCategory.ACCOUNT_SECURITY,
            UrgencyClassifier.classify(
                event(
                    "notification.posted",
                    "text",
                    "上次异常登录已确认是我，本次检测到异地登录",
                ),
            ),
        )
        assertEquals(
            UrgencyCategory.DEADLINE,
            UrgencyClassifier.classify(
                event("notification.posted", "text", "A已完成，B明天截止"),
            ),
        )
    }

    @Test
    fun englishResolvedClauseCannotSuppressNewProblemAfterTurn() {
        assertEquals(
            UrgencyCategory.URGENT_HEALTH,
            UrgencyClassifier.classify(
                event(
                    "speech.transcript",
                    "transcript",
                    "No chest pain, but I have difficulty breathing now",
                ),
            ),
        )
        assertEquals(
            UrgencyCategory.ACCOUNT_SECURITY,
            UrgencyClassifier.classify(
                event(
                    "notification.posted",
                    "text",
                    "The last suspicious login was mine; a new unauthorized login was detected",
                ),
            ),
        )
        assertEquals(
            UrgencyCategory.DEADLINE,
            UrgencyClassifier.classify(
                event("notification.posted", "text", "Task A completed, but Task B deadline tomorrow"),
            ),
        )
    }

    @Test
    fun englishNowIsNotMistakenForNo() {
        assertEquals(
            UrgencyCategory.URGENT_HEALTH,
            UrgencyClassifier.classify(
                event("speech.transcript", "transcript", "Now chest pain and difficulty breathing"),
            ),
        )
    }

    @Test
    fun resolutionTailCannotSuppressAnotherHealthSignalOrTask() {
        assertEquals(
            UrgencyCategory.URGENT_HEALTH,
            UrgencyClassifier.classify(
                event(
                    "speech.transcript",
                    "transcript",
                    "I have difficulty breathing, no chest pain",
                ),
            ),
        )
        assertEquals(
            UrgencyCategory.DEADLINE,
            UrgencyClassifier.classify(
                event("notification.posted", "text", "B deadline tomorrow, A completed"),
            ),
        )
    }

    @Test
    fun aNewTurnWithoutPunctuationEscapesTheOldNegation() {
        assertEquals(
            UrgencyCategory.URGENT_HEALTH,
            UrgencyClassifier.classify(
                event(
                    "speech.transcript",
                    "transcript",
                    "No chest pain and now difficulty breathing",
                ),
            ),
        )
    }

    @Test
    fun resolutionDoesNotCrossScreenLines() {
        val event = LocalEventEntity(
            source = "android.accessibility",
            type = "ui.visible_text",
            payloadJson = JSONObject()
                .put("visible_text", JSONArray().put("检测到异地登录").put("已确认是我本人"))
                .toString(),
        )

        assertEquals(UrgencyCategory.ACCOUNT_SECURITY, UrgencyClassifier.classify(event))
    }

    @Test
    fun shortResolutionTailStillPreventsFalseUrgentRouting() {
        assertNull(
            UrgencyClassifier.classify(
                event("notification.posted", "text", "检测到异常登录，已确认是我本人"),
            ),
        )
        assertNull(
            UrgencyClassifier.classify(
                event("notification.posted", "text", "Release deadline tomorrow, completed"),
            ),
        )
        assertNull(
            UrgencyClassifier.classify(
                event("speech.transcript", "transcript", "I had chest pain, but no chest pain now"),
            ),
        )
    }

    @Test
    fun informationalEnglishHealthTextDoesNotImpersonateFirstPerson() {
        assertNull(
            UrgencyClassifier.classify(
                event("speech.transcript", "transcript", "This is chest pain information"),
            ),
        )
    }

    @Test
    fun explicitNoFactSecurityAndDeadlineMessagesStayOffFastLane() {
        assertNull(
            UrgencyClassifier.classify(
                event("notification.posted", "text", "没有发现异常登录"),
            ),
        )
        assertNull(
            UrgencyClassifier.classify(
                event("notification.posted", "text", "No suspicious login detected"),
            ),
        )
        assertNull(
            UrgencyClassifier.classify(
                event("notification.posted", "text", "没有明天截止的任务"),
            ),
        )
        assertNull(
            UrgencyClassifier.classify(
                event("notification.posted", "text", "No deadline tomorrow"),
            ),
        )
        assertNull(
            UrgencyClassifier.classify(
                event("notification.posted", "text", "This was not a suspicious login"),
            ),
        )
        assertNull(
            UrgencyClassifier.classify(
                event("notification.posted", "text", "这不是异常登录"),
            ),
        )
    }

    @Test
    fun unpunctuatedIndependentTasksKeepTheLiveDeadline() {
        assertEquals(
            UrgencyCategory.DEADLINE,
            UrgencyClassifier.classify(
                event("notification.posted", "text", "Task A completed and Task B deadline tomorrow"),
            ),
        )
        assertEquals(
            UrgencyCategory.DEADLINE,
            UrgencyClassifier.classify(
                event("notification.posted", "text", "A已完成 B明天截止"),
            ),
        )
        assertEquals(
            UrgencyCategory.DEADLINE,
            UrgencyClassifier.classify(
                event("notification.posted", "text", "Project A completed and Project B deadline tomorrow"),
            ),
        )
        assertEquals(
            UrgencyCategory.DEADLINE,
            UrgencyClassifier.classify(
                event("notification.posted", "text", "任务甲已完成 任务乙明天截止"),
            ),
        )
    }

    private fun event(type: String, field: String, value: String) = LocalEventEntity(
        source = "test",
        type = type,
        payloadJson = JSONObject().put(field, value).toString(),
    )
}
