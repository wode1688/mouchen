package com.mouchen.app.relay

import androidx.room.Room
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test

class RelayReceiptPersistenceTest {
    private lateinit var database: RelayDatabase
    private lateinit var sync: RelaySync
    private val request = RelayRequest("11111111-1111-4111-8111-111111111111", "synthetic-partition", "snapshot.get", "{}")
    @Before fun prepare() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        database = Room.inMemoryDatabaseBuilder(context, RelayDatabase::class.java).build()
        sync = RelaySync(context, database)
    }
    @After fun close() { database.close() }
    private fun response() = ValidatedResponse("22222222-2222-4222-8222-222222222222", request.id, 1000L, true,
        JSONArray().put(JSONObject().put("id", "33333333-3333-4333-8333-333333333333").put("title", "Synthetic goal")), JSONArray(), "")
    @Test fun receiptAndRecordsCommitTogetherAndDuplicateIsIdempotent() = runBlocking {
        database.dao().insertRequest(request)
        sync.persistResponse(request.partition, response(), "synthetic-digest")
        sync.persistResponse(request.partition, response(), "synthetic-digest")
        assertEquals("completed", database.dao().request(request.partition, request.id)?.state)
        assertNotNull(database.dao().receipt(request.partition, response().id))
        assertNotNull(database.dao().record(request.partition, "goal", "33333333-3333-4333-8333-333333333333"))
    }
    @Test fun wrongPartitionCannotApplyOrEraseRequest() = runBlocking {
        database.dao().insertRequest(request)
        try { sync.persistResponse("other-partition", response(), "digest"); fail("Expected rejection") } catch (_: IllegalStateException) { }
        assertEquals("pending", database.dao().request(request.partition, request.id)?.state)
        assertNull(database.dao().receipt("other-partition", response().id))
    }
    @Test fun changedDuplicateCannotOverwriteReceipt() = runBlocking {
        database.dao().insertRequest(request)
        sync.persistResponse(request.partition, response(), "first-digest")
        try { sync.persistResponse(request.partition, response(), "changed-digest"); fail("Expected rejection") } catch (_: IllegalArgumentException) { }
        assertEquals("first-digest", database.dao().receipt(request.partition, response().id)?.digest)
    }
    @Test fun delayedOlderSnapshotCannotOverwriteNewerRecords() = runBlocking {
        val later = request.copy(id = "44444444-4444-4444-8444-444444444444")
        database.dao().insertRequest(request)
        database.dao().insertRequest(later)
        val newerGoal = JSONArray().put(JSONObject().put("id", "33333333-3333-4333-8333-333333333333").put("title", "Newer synthetic goal"))
        sync.persistResponse(request.partition, response().copy(id = "55555555-5555-4555-8555-555555555555", requestId = later.id, createdAt = 2000L, goals = newerGoal), "newer-digest")
        sync.persistResponse(request.partition, response(), "older-digest")
        val saved = database.dao().record(request.partition, "goal", "33333333-3333-4333-8333-333333333333")!!
        assertEquals("Newer synthetic goal", JSONObject(saved.payload).getString("title"))
        assertEquals("completed", database.dao().request(request.partition, request.id)?.state)
    }
}
