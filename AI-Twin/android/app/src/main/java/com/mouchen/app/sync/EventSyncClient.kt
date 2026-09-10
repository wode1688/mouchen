package com.mouchen.app.sync

import android.content.Context
import com.mouchen.app.data.AdviceEntity
import com.mouchen.app.data.GoalEntity
import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.data.MouchenDao
import com.mouchen.app.data.PendingAnalysisEntity
import com.mouchen.app.data.ServerGoalMergeResult
import com.mouchen.app.network.MouchenDns
import java.time.Instant
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject

data class SyncReport(
    val attempted: Int,
    val synced: Int,
    val failed: Int,
    val goalsAttempted: Int = 0,
    val goalsSynced: Int = 0,
    val goalsFailed: Int = 0,
    val goalPullAttempted: Int = 0,
    val goalPullSynced: Int = 0,
    val goalPullFailed: Int = 0,
    val goalsReceived: Int = 0,
    val goalsApplied: Int = 0,
    val adviceAttempted: Int = 0,
    val adviceSynced: Int = 0,
    val adviceFailed: Int = 0,
    val adviceReceived: Int = 0,
    val adviceInserted: Int = 0,
    val adviceStatusUpdated: Int = 0,
    val advicePullRequired: Boolean = false,
)

class EventSyncClient(
    context: Context,
    private val dao: MouchenDao,
    private val connectionStore: BackendConnectionStore,
    private val timingContextSource: TimingContextSource = AndroidTimingContextProvider(context.applicationContext),
    private val client: OkHttpClient = OkHttpClient.Builder()
        .dns(MouchenDns)
        .enforceSessionAuthorization(context.applicationContext)
        .connectTimeout(20, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .readTimeout(6, TimeUnit.MINUTES)
        .build(),
) {
    private val appContext = context.applicationContext

    private fun effectiveConnection(): BackendConnection {
        val stored = connectionStore.load()
        val connection = authenticatedConnection(appContext, stored)
            ?: return stored.copy(enabled = false, userId = "", bearerToken = null)
        val consent = AccountConsentStore(appContext).load(connection.userId, connection.sessionOrigin)
        return applyAccountCloudConsent(connection, consent)
    }

    private fun isCurrent(connection: BackendConnection): Boolean =
        authenticatedConnectionIsCurrent(appContext, connection)

    suspend fun sync(limit: Int = 200): SyncReport = withContext(Dispatchers.IO) {
        val connection = effectiveConnection()
        if (!connection.enabled || !isCurrent(connection)) return@withContext SyncReport(0, 0, 0)
        val now = System.currentTimeMillis()
        val goals = dao.unsyncedGoals(now, limit)
        var goalsSynced = 0
        goals.forEach { goal ->
            if (!isCurrent(connection)) return@withContext SyncReport(0, 0, 0)
            if (sendGoal(connection, goal)) {
                if (!isCurrent(connection)) return@withContext SyncReport(0, 0, 0)
                dao.markGoalSynced(goal.id)
                goalsSynced += 1
            } else {
                dao.markGoalDeliveryFailed(
                    goal.id,
                    nextDeliveryAttemptAt(now, goal.deliveryAttempts),
                )
            }
        }
        val goalsFailed = goals.size - goalsSynced
        // A poison goal must never prevent unrelated event evidence from being delivered.
        val events = dao.unsyncedEvents(now, limit)
        val timingContext = timingContextSource.snapshot()
        var synced = 0
        var advicePullRequired = false
        events.forEach { event ->
            if (!isCurrent(connection)) return@withContext SyncReport(0, 0, 0)
            val outcome = deliverEventOutcome(connection, event.id, timingContext)
            advicePullRequired = advicePullRequired || outcome.advicePullRequired
            when (outcome.result) {
                SingleEventSyncResult.DELIVERED,
                SingleEventSyncResult.ALREADY_SYNCED,
                SingleEventSyncResult.NOT_FOUND,
                -> synced += 1

                SingleEventSyncResult.FAILED,
                SingleEventSyncResult.DISABLED,
                -> Unit
            }
        }
        SyncReport(
            attempted = events.size,
            synced = synced,
            failed = events.size - synced,
            goalsAttempted = goals.size,
            goalsSynced = goalsSynced,
            goalsFailed = goalsFailed,
            advicePullRequired = advicePullRequired,
        )
    }

    /**
     * Sends only the requested event. The event-specific lock prevents the bulk and urgent workers
     * from producing duplicate local advice/notification side effects for the same evidence_ref.
     */
    internal suspend fun syncSingleEvent(
        eventId: String,
        semanticFastLane: Boolean = false,
    ): SingleEventSyncOutcome = withContext(Dispatchers.IO) {
        val connection = effectiveConnection()
        if (!connection.enabled || !isCurrent(connection)) {
            return@withContext SingleEventSyncOutcome(SingleEventSyncResult.DISABLED)
        }
        deliverEventOutcome(connection, eventId, timingContextSource.snapshot(), semanticFastLane)
    }

    suspend fun pullAdvice(limit: Int = 200): SyncReport = withContext(Dispatchers.IO) {
        val connection = effectiveConnection()
        if (!connection.enabled || !isCurrent(connection)) return@withContext SyncReport(0, 0, 0)
        var received = 0
        var inserted = 0
        var statusUpdated = 0
        try {
            val advice = fetchAdvice(connection, limit)
            check(isCurrent(connection)) { "Authenticated response is obsolete" }
            received = advice.size
            advice.forEach { incoming ->
                check(isCurrent(connection)) { "Authenticated response is obsolete" }
                val result = persistAdvice(incoming)
                inserted += result.inserted
                statusUpdated += result.statusUpdated
            }
            SyncReport(
                attempted = 0,
                synced = 0,
                failed = 0,
                adviceAttempted = 1,
                adviceSynced = 1,
                adviceReceived = received,
                adviceInserted = inserted,
                adviceStatusUpdated = statusUpdated,
            )
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            SyncReport(
                attempted = 0,
                synced = 0,
                failed = 0,
                adviceAttempted = 1,
                adviceFailed = 1,
                adviceReceived = received,
                adviceInserted = inserted,
                adviceStatusUpdated = statusUpdated,
            )
        }
    }

    /** Restores the server-owned goal history after an install or local database reset. */
    suspend fun pullGoals(): SyncReport = withContext(Dispatchers.IO) {
        val connection = effectiveConnection()
        if (!connection.enabled || !isCurrent(connection)) return@withContext SyncReport(0, 0, 0)
        var received = 0
        var applied = 0
        try {
            val goals = fetchGoals(connection)
            check(isCurrent(connection)) { "Authenticated response is obsolete" }
            received = goals.size
            goals.forEach { incoming ->
                check(isCurrent(connection)) { "Authenticated response is obsolete" }
                when (dao.mergeServerGoal(incoming)) {
                    ServerGoalMergeResult.INSERTED,
                    ServerGoalMergeResult.UPDATED,
                    -> applied += 1

                    ServerGoalMergeResult.LOCAL_PENDING -> Unit
                    ServerGoalMergeResult.CONFLICT -> error("Server goal conflicts with local history")
                }
            }
            SyncReport(
                attempted = 0,
                synced = 0,
                failed = 0,
                goalPullAttempted = 1,
                goalPullSynced = 1,
                goalsReceived = received,
                goalsApplied = applied,
            )
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            SyncReport(
                attempted = 0,
                synced = 0,
                failed = 0,
                goalPullAttempted = 1,
                goalPullFailed = 1,
                goalsReceived = received,
                goalsApplied = applied,
            )
        }
    }

    suspend fun runGoalReview(): Boolean = withContext(Dispatchers.IO) {
        val connection = effectiveConnection()
        if (!connection.enabled || !isCurrent(connection)) return@withContext true
        val timingContext = timingContextSource.snapshot()
        runCatching {
            val request = Request.Builder()
                .url(appendTimingContextQuery("${connection.baseUrl}/v1/reviews/run", timingContext))
                .header("X-User-Id", connection.userId)
                .applyCloudApprovalHeaders(connection)
                .apply {
                    connection.bearerToken?.let { header("Authorization", "Bearer $it") }
                }
                .post("{}".toRequestBody(JSON))
                .build()
            client.newCall(request).execute().use { response ->
                isCurrent(connection) && response.isSuccessful
            }
        }.getOrDefault(false)
    }

    private fun fetchAdvice(connection: BackendConnection, limit: Int): List<AdviceEntity> {
        val boundedLimit = limit.coerceIn(1, 500)
        val request = Request.Builder()
            .url("${connection.baseUrl}/v1/advice?limit=$boundedLimit")
            .header("X-User-Id", connection.userId)
            .applyCloudApprovalHeaders(connection)
            .apply {
                connection.bearerToken?.let { header("Authorization", "Bearer $it") }
            }
            .get()
            .build()
        return client.newCall(request).execute().use { response ->
            check(response.isSuccessful) { "Advice pull failed with HTTP ${response.code}" }
            check(isCurrent(connection)) { "Authenticated response is obsolete" }
            parseAdviceRecords(response.body?.string().orEmpty())
        }
    }

    private fun fetchGoals(connection: BackendConnection): List<GoalEntity> {
        val request = Request.Builder()
            .url("${connection.baseUrl}/v1/goals")
            .header("X-User-Id", connection.userId)
            .applyCloudApprovalHeaders(connection)
            .apply {
                connection.bearerToken?.let { header("Authorization", "Bearer $it") }
            }
            .get()
            .build()
        return client.newCall(request).execute().use { response ->
            check(response.isSuccessful) { "Goal pull failed with HTTP ${response.code}" }
            check(isCurrent(connection)) { "Authenticated response is obsolete" }
            parseGoalRecords(response.body?.string().orEmpty())
        }
    }

    private suspend fun persistAdvice(
        incoming: AdviceEntity,
        scheduleAttention: Boolean = true,
    ): AdviceWriteResult {
        val result = ADVICE_PERSISTENCE_MUTEX.withLock {
            val result = when (val action = adviceMergeAction(dao.advice(incoming.id), incoming)) {
                is AdviceMergeAction.Insert -> {
                    dao.insertAdvice(action.advice)
                    AdviceFollowUpScheduler.reconcile(appContext, action.advice)
                    AdviceWriteResult(inserted = 1)
                }

                is AdviceMergeAction.UpdateStatus -> {
                    check(dao.updateAdviceStatus(action.advice.id, action.advice.status) == 1) {
                        "Advice disappeared while updating status"
                    }
                    AdviceFollowUpScheduler.reconcile(appContext, action.advice)
                    AdviceWriteResult(statusUpdated = 1)
                }
            }
            result
        }
        if (scheduleAttention) AdviceAttentionScheduler.enqueueNow(appContext)
        return result
    }

    /** Persists the payload carried by a central claim without recursively claiming again. */
    internal suspend fun persistClaimedAdvice(incoming: AdviceEntity) {
        persistAdvice(incoming, scheduleAttention = false)
    }

    private suspend fun deliverEventOutcome(
        connection: BackendConnection,
        eventId: String,
        timingContext: TimingContextSnapshot,
        semanticFastLane: Boolean = false,
    ): SingleEventSyncOutcome = EVENT_DELIVERY_LOCKS.withLock(eventId) {
        if (!isCurrent(connection)) return@withLock SingleEventSyncOutcome(SingleEventSyncResult.DISABLED)
        val outcome = deliverSingleEventOutcome(
            eventId = eventId,
            loadEvent = dao::event,
            sendEvent = { event -> send(connection, event, timingContext, semanticFastLane) },
            persistIncomingAdvice = { incoming -> persistAdvice(incoming) },
            markDelivered = { id, queued ->
                check(isCurrent(connection)) { "Authenticated response is obsolete" }
                if (queued) {
                    dao.markEventSyncedWithPendingAnalysis(PendingAnalysisEntity(eventId = id))
                } else {
                    dao.markEventSyncedWithoutPendingAnalysis(id)
                    false
                }
            },
        )
        if (outcome.result == SingleEventSyncResult.FAILED) {
            if (!isCurrent(connection)) return@withLock SingleEventSyncOutcome(SingleEventSyncResult.DISABLED)
            dao.event(eventId)?.takeIf { !it.synced }?.let { event ->
                dao.markEventDeliveryFailed(
                    event.id,
                    nextDeliveryAttemptAt(System.currentTimeMillis(), event.deliveryAttempts),
                )
            }
        }
        outcome
    }

    private fun sendGoal(connection: BackendConnection, goal: GoalEntity): Boolean {
        if (!isCurrent(connection)) return false
        val target = runCatching { JSONObject(goal.targetJson) }.getOrDefault(JSONObject())
        val body = JSONObject()
            .put("domain", goal.domain)
            .put("title", goal.title)
            .put("quote", goal.quote)
            .put("target", target)
            .put("is_redline", goal.isRedline)
            .toString()
        return runCatching {
            val request = Request.Builder()
                .url("${connection.baseUrl}/v1/goals/${goal.id}")
                .header("X-User-Id", connection.userId)
                .applyCloudApprovalHeaders(connection)
                .apply {
                    connection.bearerToken?.let { header("Authorization", "Bearer $it") }
                }
                .put(body.toRequestBody(JSON))
                .build()
            client.newCall(request).execute().use { response ->
                isCurrent(connection) && response.isSuccessful
            }
        }.getOrDefault(false)
    }

    private fun send(
        connection: BackendConnection,
        event: LocalEventEntity,
        timingContext: TimingContextSnapshot,
        semanticFastLane: Boolean,
    ): SyncDelivery {
        if (!isCurrent(connection)) return SyncDelivery(false)
        val outbound = OutboundEventPolicy.prepare(
            event = event,
            minimizedContextOnly = connection.minimizedContextOnly,
            proactiveCloudEnabled = connection.proactiveCloudEnabled,
        )
        val body = JSONObject()
            .put("source", outbound.source.take(80))
            .put("type", outbound.type)
            .put("occurred_at", Instant.ofEpochMilli(event.occurredAt).toString())
            .put("facts", outbound.facts)
            .put("entities", JSONArray())
            .put("confidence", 1.0)
            .put("sensitivity", normalizedSensitivity(outbound.sensitivity))
            .put("consent_scope", outbound.consentScope)
            .put("evidence_ref", eventEvidenceRef(event.id))
            .toString()
        return runCatching {
            val request = Request.Builder()
                .url(appendTimingContextQuery("${connection.baseUrl}/v1/events", timingContext))
                .header("X-User-Id", connection.userId)
                .applyCloudApprovalHeaders(
                    connection,
                    proactiveCloudAnalysisAllowed = eventAllowsProactiveCloudAnalysis(outbound.type),
                )
                .applySemanticFastLane(semanticFastLane)
                .apply {
                    connection.bearerToken?.let { header("Authorization", "Bearer $it") }
                }
                .post(body.toRequestBody(JSON))
                .build()
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return@use SyncDelivery(false)
                if (!isCurrent(connection)) return@use SyncDelivery(false)
                val responseBody = response.body?.string().orEmpty()
                val advice = parsePublishedAdvice(responseBody)
                SyncDelivery(
                    accepted = true,
                    advice = advice,
                    advicePullRequired = advice == null && isQueuedDelivery(response.code, responseBody),
                )
            }
        }.getOrDefault(SyncDelivery(false))
    }

    private fun normalizedSensitivity(value: String): String = when (value) {
        "public", "personal", "sensitive", "restricted" -> value
        else -> "sensitive"
    }

    private companion object {
        val JSON = "application/json; charset=utf-8".toMediaType()
        val ADVICE_PERSISTENCE_MUTEX = Mutex()
        val EVENT_DELIVERY_LOCKS = EventDeliveryLockRegistry()
    }
}

internal enum class SingleEventSyncResult {
    DELIVERED,
    ALREADY_SYNCED,
    NOT_FOUND,
    FAILED,
    DISABLED,
}

internal data class SingleEventSyncOutcome(
    val result: SingleEventSyncResult,
    val advicePullRequired: Boolean = false,
)

internal fun eventEvidenceRef(eventId: String): String = "android:$eventId"

internal fun Request.Builder.applySemanticFastLane(enabled: Boolean): Request.Builder = apply {
    if (enabled) header("X-Semantic-Fast-Lane", "true")
}

internal fun Request.Builder.applyCloudApprovalHeaders(
    connection: BackendConnection,
    proactiveCloudAnalysisAllowed: Boolean = true,
): Request.Builder = apply {
    val proactiveApproved = connection.proactiveCloudEnabled && proactiveCloudAnalysisAllowed
    header("X-Proactive-Cloud-Approved", proactiveApproved.toString())
    header("X-Raw-Cloud-Approved", (rawCloudApproved(connection) && proactiveCloudAnalysisAllowed).toString())
}

internal fun eventAllowsProactiveCloudAnalysis(eventType: String): Boolean =
    eventType != "screen.ocr_diagnostic"

internal fun rawCloudApproved(connection: BackendConnection): Boolean =
    connection.proactiveCloudEnabled && !connection.minimizedContextOnly

internal fun nextDeliveryAttemptAt(now: Long, attemptsBeforeFailure: Int): Long {
    val exponent = attemptsBeforeFailure.coerceIn(0, MAX_DELIVERY_BACKOFF_EXPONENT)
    val delay = (INITIAL_DELIVERY_BACKOFF_MS shl exponent).coerceAtMost(MAX_DELIVERY_BACKOFF_MS)
    return if (now > Long.MAX_VALUE - delay) Long.MAX_VALUE else now + delay
}

/** The testable durable boundary shared by normal and urgent delivery. */
internal suspend fun deliverSingleEvent(
    eventId: String,
    loadEvent: suspend (String) -> LocalEventEntity?,
    sendEvent: suspend (LocalEventEntity) -> SyncDelivery,
    persistIncomingAdvice: suspend (AdviceEntity) -> Unit,
    markDelivered: suspend (String, Boolean) -> Boolean,
): SingleEventSyncResult = deliverSingleEventOutcome(
    eventId = eventId,
    loadEvent = loadEvent,
    sendEvent = sendEvent,
    persistIncomingAdvice = persistIncomingAdvice,
    markDelivered = markDelivered,
).result

internal suspend fun deliverSingleEventOutcome(
    eventId: String,
    loadEvent: suspend (String) -> LocalEventEntity?,
    sendEvent: suspend (LocalEventEntity) -> SyncDelivery,
    persistIncomingAdvice: suspend (AdviceEntity) -> Unit,
    markDelivered: suspend (String, Boolean) -> Boolean,
): SingleEventSyncOutcome {
    return try {
        val event = loadEvent(eventId)
            ?: return SingleEventSyncOutcome(SingleEventSyncResult.NOT_FOUND)
        if (event.synced) return SingleEventSyncOutcome(SingleEventSyncResult.ALREADY_SYNCED)
        val delivery = sendEvent(event)
        if (!delivery.accepted) return SingleEventSyncOutcome(SingleEventSyncResult.FAILED)
        delivery.advice?.let { persistIncomingAdvice(it) }
        // The queued implementation inserts pending_analysis and closes local_events in one Room
        // transaction. Direct advice removes only a stale same-event pending row, never the queue.
        val queuedPollScheduleRequired = markDelivered(event.id, delivery.advicePullRequired)
        SingleEventSyncOutcome(
            result = SingleEventSyncResult.DELIVERED,
            advicePullRequired = queuedPollScheduleRequired,
        )
    } catch (cancelled: CancellationException) {
        throw cancelled
    } catch (_: Exception) {
        SingleEventSyncOutcome(SingleEventSyncResult.FAILED)
    }
}

/** Reference-counted locks avoid both an unbounded ID map and an unlock/remove race. */
internal class EventDeliveryLockRegistry {
    private data class Entry(val mutex: Mutex = Mutex(), var references: Int = 0)

    private val monitor = Any()
    private val entries = mutableMapOf<String, Entry>()

    suspend fun <T> withLock(eventId: String, action: suspend () -> T): T {
        val entry = synchronized(monitor) {
            entries.getOrPut(eventId, ::Entry).also { it.references += 1 }
        }
        var locked = false
        try {
            entry.mutex.lock()
            locked = true
            return action()
        } finally {
            if (locked) entry.mutex.unlock()
            synchronized(monitor) {
                entry.references -= 1
                if (entry.references == 0) entries.remove(eventId, entry)
            }
        }
    }

    internal fun referenceCount(eventId: String): Int = synchronized(monitor) {
        entries[eventId]?.references ?: 0
    }

    internal fun activeEntryCount(): Int = synchronized(monitor) { entries.size }
}

internal fun appendTimingContextQuery(url: String, timingContext: TimingContextSnapshot): String =
    url.toHttpUrl().newBuilder()
        .setQueryParameter("sleeping", timingContext.sleeping.toString())
        .setQueryParameter("driving", timingContext.driving.toString())
        .setQueryParameter("in_meeting", timingContext.inMeeting.toString())
        .setQueryParameter("quiet_hours", timingContext.quietHours.toString())
        .build()
        .toString()

internal data class SyncDelivery(
    val accepted: Boolean,
    val advice: AdviceEntity? = null,
    val advicePullRequired: Boolean = false,
)

internal fun isQueuedDelivery(responseCode: Int, responseBody: String): Boolean {
    if (responseCode == 202) return true
    val root = runCatching { JSONObject(responseBody) }.getOrNull() ?: return false
    return root.optBoolean("queued", false) ||
        root.optString("status").equals("queued", ignoreCase = true) ||
        root.optString("decision").equals("queued", ignoreCase = true)
}

private const val INITIAL_DELIVERY_BACKOFF_MS = 15_000L
private const val MAX_DELIVERY_BACKOFF_MS = 6L * 60 * 60 * 1000
private const val MAX_DELIVERY_BACKOFF_EXPONENT = 16

internal sealed interface AdviceMergeAction {
    data class Insert(val advice: AdviceEntity) : AdviceMergeAction
    data class UpdateStatus(val advice: AdviceEntity) : AdviceMergeAction
}

private data class AdviceWriteResult(
    val inserted: Int = 0,
    val statusUpdated: Int = 0,
)

internal fun adviceMergeAction(existing: AdviceEntity?, incoming: AdviceEntity): AdviceMergeAction =
    if (existing == null) {
        AdviceMergeAction.Insert(advice = incoming)
    } else {
        val merged = existing.copy(status = incoming.status)
        AdviceMergeAction.UpdateStatus(advice = merged)
    }

internal fun parseAdviceRecords(responseBody: String): List<AdviceEntity> {
    val records = JSONArray(responseBody)
    return List(records.length()) { index -> parseAdviceRecord(records.getJSONObject(index)) }
}

internal fun parseGoalRecords(responseBody: String): List<GoalEntity> {
    val records = JSONArray(responseBody)
    return List(records.length()) { index ->
        val goal = records.getJSONObject(index)
        GoalEntity(
            id = goal.getString("id"),
            domain = goal.getString("domain"),
            title = goal.getString("title"),
            quote = goal.getString("quote"),
            version = goal.getInt("version"),
            isRedline = goal.optBoolean("is_redline", false),
            targetJson = goal.optJSONObject("target")?.toString() ?: "{}",
            createdAt = Instant.parse(goal.getString("created_at")).toEpochMilli(),
            synced = true,
        )
    }
}

internal fun parsePublishedAdvice(responseBody: String): AdviceEntity? = runCatching {
    val root = JSONObject(responseBody)
    val evaluation = root.optJSONObject("evaluation") ?: return null
    if (evaluation.optString("decision") != "publish") return null
    parseAdviceRecord(evaluation.getJSONObject("advice"))
}.getOrNull()

internal fun parseAdviceRecord(advice: JSONObject): AdviceEntity {
    val id = advice.getString("id")
    val prediction = advice.optJSONObject("prediction")?.let { JSONObject(it.toString()) } ?: JSONObject()
    advice.optString("adopted_expected_result").trim().takeIf(String::isNotEmpty)?.let {
        prediction.put("adopted_expected_result", it)
    }
    if (advice.has("adopted_confidence") && !advice.isNull("adopted_confidence")) {
        prediction.put("adopted_confidence", advice.optDouble("adopted_confidence"))
    }
    return AdviceEntity(
        id = id,
        domain = advice.optString("domain", "general"),
        level = adviceLevel(advice).coerceIn(1, 4),
        goalQuote = advice.optString("goal_quote"),
        evidenceJson = advice.optJSONArray("evidence")?.toString() ?: "[]",
        action = advice.optString("action"),
        firstStep = advice.optString("first_step"),
        alternative = advice.optString("alternative"),
        predictionJson = prediction.toString(),
        dedupeKey = advice.optString("dedupe_key", id),
        delivery = advice.optString("delivery", "immediate"),
        status = advice.optString("status", "active"),
        createdAt = runCatching {
            Instant.parse(advice.getString("created_at")).toEpochMilli()
        }.getOrDefault(System.currentTimeMillis()),
    )
}

private fun adviceLevel(advice: JSONObject): Int {
    val raw = when {
        advice.has("effective_level") -> advice.opt("effective_level")
        else -> advice.opt("requested_level")
    }
    return when (raw) {
        is Number -> raw.toInt()
        is String -> raw.removePrefix("L").removePrefix("l").toIntOrNull() ?: 1
        else -> 1
    }
}
