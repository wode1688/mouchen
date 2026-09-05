package com.mouchen.app.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase
import com.mouchen.app.security.KeyVault
import net.zetetic.database.sqlcipher.SupportOpenHelperFactory

@Database(
    entities = [
        LocalEventEntity::class,
        GoalEntity::class,
        AdviceEntity::class,
        ActionDraftEntity::class,
        AudioTranscriptionJobEntity::class,
        UrgentEventQueueEntity::class,
        PendingAnalysisEntity::class,
    ],
    version = 9,
    exportSchema = true,
)
abstract class MouchenDatabase : RoomDatabase() {
    abstract fun dao(): MouchenDao

    companion object {
        @Volatile private var instance: MouchenDatabase? = null

        fun get(context: Context): MouchenDatabase {
            return instance ?: synchronized(this) {
                instance ?: Room.databaseBuilder(
                    context.applicationContext,
                    MouchenDatabase::class.java,
                    "mouchen-local.db",
                ).openHelperFactory(SupportOpenHelperFactory(KeyVault(context).databasePassphrase()))
                    .addMigrations(
                        MIGRATION_1_2,
                        MIGRATION_2_3,
                        MIGRATION_3_4,
                        MIGRATION_4_5,
                        MIGRATION_5_6,
                        MIGRATION_6_7,
                        MIGRATION_7_8,
                        MIGRATION_8_9,
                    )
                    .build()
                    .also { instance = it }
            }
        }

        private val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE goals ADD COLUMN synced INTEGER NOT NULL DEFAULT 0")
            }
        }

        private val MIGRATION_2_3 = object : Migration(2, 3) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE advice ADD COLUMN alternative TEXT NOT NULL DEFAULT ''")
                db.execSQL("ALTER TABLE advice ADD COLUMN delivery TEXT NOT NULL DEFAULT 'immediate'")
            }
        }

        private val MIGRATION_3_4 = object : Migration(3, 4) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    """
                    CREATE TABLE IF NOT EXISTS `audio_transcription_jobs` (
                        `blobName` TEXT NOT NULL,
                        `segmentStartedAt` INTEGER NOT NULL,
                        `segmentEndedAt` INTEGER NOT NULL,
                        `status` TEXT NOT NULL,
                        `recoveryCycles` INTEGER NOT NULL,
                        `nextAttemptAt` INTEGER NOT NULL,
                        `lastFailure` TEXT NOT NULL,
                        `updatedAt` INTEGER NOT NULL,
                        PRIMARY KEY(`blobName`)
                    )
                    """.trimIndent(),
                )
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS `index_audio_transcription_jobs_status_nextAttemptAt` " +
                        "ON `audio_transcription_jobs` (`status`, `nextAttemptAt`)",
                )
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS `index_audio_transcription_jobs_updatedAt` " +
                        "ON `audio_transcription_jobs` (`updatedAt`)",
                )
            }
        }

        private val MIGRATION_4_5 = object : Migration(4, 5) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE advice ADD COLUMN notifiedAt INTEGER")
            }
        }

        private val MIGRATION_5_6 = object : Migration(5, 6) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE advice ADD COLUMN followUpNotifiedAt INTEGER")
            }
        }

        internal val MIGRATION_6_7 = object : Migration(6, 7) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE local_events ADD COLUMN deliveryAttempts INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE local_events ADD COLUMN nextDeliveryAttemptAt INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE goals ADD COLUMN deliveryAttempts INTEGER NOT NULL DEFAULT 0")
                db.execSQL("ALTER TABLE goals ADD COLUMN nextDeliveryAttemptAt INTEGER NOT NULL DEFAULT 0")
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS `index_local_events_synced_nextDeliveryAttemptAt_occurredAt` " +
                        "ON `local_events` (`synced`, `nextDeliveryAttemptAt`, `occurredAt`)",
                )
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS `index_goals_synced_nextDeliveryAttemptAt_createdAt` " +
                        "ON `goals` (`synced`, `nextDeliveryAttemptAt`, `createdAt`)",
                )
                db.execSQL(
                    """
                    CREATE TABLE IF NOT EXISTS `urgent_event_queue` (
                        `eventId` TEXT NOT NULL,
                        `category` TEXT NOT NULL,
                        `attempts` INTEGER NOT NULL,
                        `nextAttemptAt` INTEGER NOT NULL,
                        `enqueuedAt` INTEGER NOT NULL,
                        `updatedAt` INTEGER NOT NULL,
                        `lastFailure` TEXT NOT NULL,
                        PRIMARY KEY(`eventId`)
                    )
                    """.trimIndent(),
                )
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS `index_urgent_event_queue_nextAttemptAt` " +
                        "ON `urgent_event_queue` (`nextAttemptAt`)",
                )
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS `index_urgent_event_queue_enqueuedAt` " +
                        "ON `urgent_event_queue` (`enqueuedAt`)",
                )
            }
        }

        internal val MIGRATION_7_8 = object : Migration(7, 8) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    """
                    CREATE TABLE IF NOT EXISTS `pending_analysis` (
                        `eventId` TEXT NOT NULL,
                        `attempts` INTEGER NOT NULL,
                        `enqueuedAt` INTEGER NOT NULL,
                        `nextAttemptAt` INTEGER NOT NULL,
                        `updatedAt` INTEGER NOT NULL,
                        PRIMARY KEY(`eventId`)
                    )
                    """.trimIndent(),
                )
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS `index_pending_analysis_nextAttemptAt` " +
                        "ON `pending_analysis` (`nextAttemptAt`)",
                )
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS `index_pending_analysis_enqueuedAt` " +
                        "ON `pending_analysis` (`enqueuedAt`)",
                )
            }
        }

        /** Existing queued audio has no trustworthy destination binding and therefore stays blank. */
        internal val MIGRATION_8_9 = object : Migration(8, 9) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "ALTER TABLE audio_transcription_jobs " +
                        "ADD COLUMN destinationFingerprint TEXT NOT NULL DEFAULT ''",
                )
            }
        }
    }
}
