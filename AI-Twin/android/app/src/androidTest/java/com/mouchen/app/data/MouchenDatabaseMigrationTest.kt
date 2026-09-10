package com.mouchen.app.data

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/** Exercises the production v6 -> v9 migration chain against a real Android SQLite database. */
@RunWith(AndroidJUnit4::class)
class MouchenDatabaseMigrationTest {
    private lateinit var context: Context
    private var migratedDatabase: MouchenDatabase? = null

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        context.deleteDatabase(TEST_DATABASE)
    }

    @After
    fun tearDown() {
        migratedDatabase?.close()
        context.deleteDatabase(TEST_DATABASE)
    }

    @Test
    fun migration6To9PreservesRowsAndCreatesBothDurableQueues() {
        createVersion6Database()

        val database = Room.databaseBuilder(context, MouchenDatabase::class.java, TEST_DATABASE)
            .addMigrations(
                MouchenDatabase.MIGRATION_6_7,
                MouchenDatabase.MIGRATION_7_8,
                MouchenDatabase.MIGRATION_8_9,
            )
            .allowMainThreadQueries()
            .build()
            .also { migratedDatabase = it }
        val sqlite = database.openHelper.writableDatabase

        // Opening the database is itself Room's complete v9 schema validation.
        assertEquals(9, sqlite.version)
        sqlite.query(
            "SELECT source, type, occurredAt, sensitivity, payloadJson, synced, " +
                "deliveryAttempts, nextDeliveryAttemptAt FROM local_events WHERE id = ?",
            arrayOf(EVENT_ID),
        ).use { cursor ->
            assertTrue(cursor.moveToFirst())
            assertEquals("legacy-notification", cursor.getString(0))
            assertEquals("notification.posted", cursor.getString(1))
            assertEquals(1_234L, cursor.getLong(2))
            assertEquals("sensitive", cursor.getString(3))
            assertEquals("{\"text\":\"legacy event\"}", cursor.getString(4))
            assertEquals(0, cursor.getInt(5))
            assertEquals(0, cursor.getInt(6))
            assertEquals(0L, cursor.getLong(7))
        }
        sqlite.query(
            "SELECT domain, title, quote, version, isRedline, targetJson, createdAt, synced, " +
                "deliveryAttempts, nextDeliveryAttemptAt FROM goals WHERE id = ?",
            arrayOf(GOAL_ID),
        ).use { cursor ->
            assertTrue(cursor.moveToFirst())
            assertEquals("work", cursor.getString(0))
            assertEquals("Ship Mouchen", cursor.getString(1))
            assertEquals("本周完成真机闭环", cursor.getString(2))
            assertEquals(3, cursor.getInt(3))
            assertEquals(1, cursor.getInt(4))
            assertEquals("{\"deadline\":\"this-week\"}", cursor.getString(5))
            assertEquals(5_678L, cursor.getLong(6))
            assertEquals(0, cursor.getInt(7))
            assertEquals(0, cursor.getInt(8))
            assertEquals(0L, cursor.getLong(9))
        }
        sqlite.query(
            "SELECT destinationFingerprint FROM audio_transcription_jobs WHERE blobName = ?",
            arrayOf(LEGACY_AUDIO_BLOB),
        ).use { cursor ->
            assertTrue(cursor.moveToFirst())
            assertEquals("", cursor.getString(0))
        }

        val queued = UrgentEventQueueEntity(
            eventId = EVENT_ID,
            category = "deadline",
            enqueuedAt = 6_000L,
            updatedAt = 6_000L,
        )
        runBlocking {
            assertNotEquals(-1L, database.dao().insertUrgentEventIfAbsent(queued))
            assertEquals(1, database.dao().urgentEventCount())
            assertEquals(queued, database.dao().nextDueUrgentEvent(now = 6_000L))
            assertTrue(
                database.dao().markEventSyncedWithPendingAnalysis(
                    PendingAnalysisEntity(
                        eventId = EVENT_ID,
                        enqueuedAt = 7_000L,
                        nextAttemptAt = 12_000L,
                        updatedAt = 7_000L,
                    ),
                ),
            )
            assertEquals(1, database.dao().pendingAnalysisCount())
            assertEquals(EVENT_ID, database.dao().pendingAnalysis(EVENT_ID)?.eventId)
            assertTrue(database.dao().event(EVENT_ID)?.synced == true)
        }

        assertIndex(
            sqlite = sqlite,
            table = "local_events",
            name = "index_local_events_synced_nextDeliveryAttemptAt_occurredAt",
            columns = listOf("synced", "nextDeliveryAttemptAt", "occurredAt"),
            unique = false,
        )
        assertIndex(
            sqlite = sqlite,
            table = "goals",
            name = "index_goals_synced_nextDeliveryAttemptAt_createdAt",
            columns = listOf("synced", "nextDeliveryAttemptAt", "createdAt"),
            unique = false,
        )
        assertIndex(
            sqlite = sqlite,
            table = "goals",
            name = "index_goals_domain_version",
            columns = listOf("domain", "version"),
            unique = true,
        )
        assertIndex(
            sqlite = sqlite,
            table = "urgent_event_queue",
            name = "index_urgent_event_queue_nextAttemptAt",
            columns = listOf("nextAttemptAt"),
            unique = false,
        )
        assertIndex(
            sqlite = sqlite,
            table = "pending_analysis",
            name = "index_pending_analysis_nextAttemptAt",
            columns = listOf("nextAttemptAt"),
            unique = false,
        )
        assertIndex(
            sqlite = sqlite,
            table = "pending_analysis",
            name = "index_pending_analysis_enqueuedAt",
            columns = listOf("enqueuedAt"),
            unique = false,
        )
        assertIndex(
            sqlite = sqlite,
            table = "urgent_event_queue",
            name = "index_urgent_event_queue_enqueuedAt",
            columns = listOf("enqueuedAt"),
            unique = false,
        )
    }

    private fun createVersion6Database() {
        val databaseFile = context.getDatabasePath(TEST_DATABASE)
        databaseFile.parentFile?.let { directory ->
            check(directory.isDirectory || directory.mkdirs()) { "Cannot create database directory" }
        }
        SQLiteDatabase.openOrCreateDatabase(databaseFile, null).use { sqlite ->
            VERSION_6_DDL.forEach(sqlite::execSQL)
            sqlite.execSQL(
                "INSERT INTO local_events " +
                    "(id, source, type, occurredAt, sensitivity, payloadJson, synced) " +
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                arrayOf<Any?>(
                    EVENT_ID,
                    "legacy-notification",
                    "notification.posted",
                    1_234L,
                    "sensitive",
                    "{\"text\":\"legacy event\"}",
                    0,
                ),
            )
            sqlite.execSQL(
                "INSERT INTO goals " +
                    "(id, domain, title, quote, version, isRedline, targetJson, createdAt, synced) " +
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                arrayOf<Any?>(
                    GOAL_ID,
                    "work",
                    "Ship Mouchen",
                    "本周完成真机闭环",
                    3,
                    1,
                    "{\"deadline\":\"this-week\"}",
                    5_678L,
                    0,
                ),
            )
            sqlite.execSQL(
                "INSERT INTO audio_transcription_jobs " +
                    "(blobName, segmentStartedAt, segmentEndedAt, status, recoveryCycles, " +
                    "nextAttemptAt, lastFailure, updatedAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                arrayOf<Any?>(LEGACY_AUDIO_BLOB, 1_000L, 2_000L, "pending", 0, 0L, "", 2_000L),
            )
            sqlite.version = 6
        }
    }

    private fun assertIndex(
        sqlite: androidx.sqlite.db.SupportSQLiteDatabase,
        table: String,
        name: String,
        columns: List<String>,
        unique: Boolean,
    ) {
        var found = false
        sqlite.query("PRAGMA index_list(`$table`)").use { cursor ->
            val nameColumn = cursor.getColumnIndexOrThrow("name")
            val uniqueColumn = cursor.getColumnIndexOrThrow("unique")
            while (cursor.moveToNext()) {
                if (cursor.getString(nameColumn) == name) {
                    found = true
                    assertEquals(unique, cursor.getInt(uniqueColumn) == 1)
                }
            }
        }
        assertTrue("Missing index $name on $table", found)

        val actualColumns = buildList {
            sqlite.query("PRAGMA index_info(`$name`)").use { cursor ->
                val columnName = cursor.getColumnIndexOrThrow("name")
                while (cursor.moveToNext()) add(cursor.getString(columnName))
            }
        }
        assertEquals(columns, actualColumns)
    }

    private companion object {
        const val TEST_DATABASE = "mouchen-migration-6-7-test.db"
        const val EVENT_ID = "legacy-event"
        const val GOAL_ID = "legacy-goal"
        const val LEGACY_AUDIO_BLOB = "audio-1000-segment-0000.pcm.mch"

        val VERSION_6_DDL = listOf(
            "CREATE TABLE IF NOT EXISTS `local_events` (`id` TEXT NOT NULL, `source` TEXT NOT NULL, " +
                "`type` TEXT NOT NULL, `occurredAt` INTEGER NOT NULL, `sensitivity` TEXT NOT NULL, " +
                "`payloadJson` TEXT NOT NULL, `synced` INTEGER NOT NULL, PRIMARY KEY(`id`))",
            "CREATE INDEX IF NOT EXISTS `index_local_events_occurredAt` ON `local_events` (`occurredAt`)",
            "CREATE INDEX IF NOT EXISTS `index_local_events_type` ON `local_events` (`type`)",
            "CREATE INDEX IF NOT EXISTS `index_local_events_synced` ON `local_events` (`synced`)",
            "CREATE TABLE IF NOT EXISTS `goals` (`id` TEXT NOT NULL, `domain` TEXT NOT NULL, " +
                "`title` TEXT NOT NULL, `quote` TEXT NOT NULL, `version` INTEGER NOT NULL, " +
                "`isRedline` INTEGER NOT NULL, `targetJson` TEXT NOT NULL, `createdAt` INTEGER NOT NULL, " +
                "`synced` INTEGER NOT NULL, PRIMARY KEY(`id`))",
            "CREATE UNIQUE INDEX IF NOT EXISTS `index_goals_domain_version` ON `goals` (`domain`, `version`)",
            "CREATE TABLE IF NOT EXISTS `advice` (`id` TEXT NOT NULL, `domain` TEXT NOT NULL, " +
                "`level` INTEGER NOT NULL, `goalQuote` TEXT NOT NULL, `evidenceJson` TEXT NOT NULL, " +
                "`action` TEXT NOT NULL, `firstStep` TEXT NOT NULL, `alternative` TEXT NOT NULL, " +
                "`predictionJson` TEXT NOT NULL, `dedupeKey` TEXT NOT NULL, `delivery` TEXT NOT NULL, " +
                "`status` TEXT NOT NULL, `notifiedAt` INTEGER, `followUpNotifiedAt` INTEGER, " +
                "`createdAt` INTEGER NOT NULL, PRIMARY KEY(`id`))",
            "CREATE INDEX IF NOT EXISTS `index_advice_domain` ON `advice` (`domain`)",
            "CREATE INDEX IF NOT EXISTS `index_advice_status` ON `advice` (`status`)",
            "CREATE INDEX IF NOT EXISTS `index_advice_dedupeKey` ON `advice` (`dedupeKey`)",
            "CREATE TABLE IF NOT EXISTS `action_drafts` (`id` TEXT NOT NULL, `actionType` TEXT NOT NULL, " +
                "`payloadJson` TEXT NOT NULL, `status` TEXT NOT NULL, `confirmedAt` INTEGER, " +
                "`executedAt` INTEGER, `createdAt` INTEGER NOT NULL, PRIMARY KEY(`id`))",
            "CREATE INDEX IF NOT EXISTS `index_action_drafts_status` ON `action_drafts` (`status`)",
            "CREATE TABLE IF NOT EXISTS `audio_transcription_jobs` (`blobName` TEXT NOT NULL, " +
                "`segmentStartedAt` INTEGER NOT NULL, `segmentEndedAt` INTEGER NOT NULL, `status` TEXT NOT NULL, " +
                "`recoveryCycles` INTEGER NOT NULL, `nextAttemptAt` INTEGER NOT NULL, `lastFailure` TEXT NOT NULL, " +
                "`updatedAt` INTEGER NOT NULL, PRIMARY KEY(`blobName`))",
            "CREATE INDEX IF NOT EXISTS `index_audio_transcription_jobs_status_nextAttemptAt` " +
                "ON `audio_transcription_jobs` (`status`, `nextAttemptAt`)",
            "CREATE INDEX IF NOT EXISTS `index_audio_transcription_jobs_updatedAt` " +
                "ON `audio_transcription_jobs` (`updatedAt`)",
        )
    }
}
