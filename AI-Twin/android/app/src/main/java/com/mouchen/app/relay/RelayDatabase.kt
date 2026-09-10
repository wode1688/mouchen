package com.mouchen.app.relay

import android.content.Context
import androidx.room.*
import com.mouchen.app.security.KeyVault
import kotlinx.coroutines.flow.Flow
import net.zetetic.database.sqlcipher.SupportOpenHelperFactory

@Entity(tableName = "relay_requests", indices = [Index(value = ["partition", "state"])])
internal data class RelayRequest(
    @PrimaryKey val id: String,
    val partition: String,
    val operation: String,
    val payload: String,
    val createdAt: Long = System.currentTimeMillis(),
    val state: String = "pending",
    val transportKey: String = java.util.UUID.randomUUID().toString(),
    val transportStartedAt: Long = 0L,
    val uploadedAt: Long = 0L,
    val errorCode: String = "",
)

@Entity(tableName = "relay_records", primaryKeys = ["partition", "kind", "id"])
internal data class RelayRecord(val partition: String, val kind: String, val id: String, val payload: String, val responseAt: Long)

@Entity(tableName = "relay_receipts", primaryKeys = ["partition", "id"])
internal data class RelayReceipt(val partition: String, val id: String, val digest: String)

@Dao
internal interface RelayDao {
    @Insert(onConflict = OnConflictStrategy.ABORT) suspend fun insertRequest(request: RelayRequest)
    @Update suspend fun updateRequest(request: RelayRequest)
    @Query("SELECT * FROM relay_requests WHERE id = :id AND partition = :partition")
    suspend fun request(partition: String, id: String): RelayRequest?
    @Query("SELECT * FROM relay_requests WHERE partition = :partition AND state = 'pending' AND (uploadedAt = 0 OR uploadedAt <= :retryBefore) ORDER BY createdAt LIMIT 30")
    suspend fun pending(partition: String, retryBefore: Long): List<RelayRequest>
    @Query("SELECT * FROM relay_requests WHERE partition = :partition ORDER BY createdAt DESC LIMIT 100")
    fun observeRequests(partition: String): Flow<List<RelayRequest>>
    @Query("SELECT * FROM relay_records WHERE partition = :partition ORDER BY responseAt DESC LIMIT 200")
    fun observeRecords(partition: String): Flow<List<RelayRecord>>
    @Query("SELECT * FROM relay_records WHERE partition = :partition AND kind = :kind AND id = :id")
    suspend fun record(partition: String, kind: String, id: String): RelayRecord?
    @Insert(onConflict = OnConflictStrategy.REPLACE) suspend fun putRecord(record: RelayRecord)
    @Query("SELECT * FROM relay_receipts WHERE partition = :partition AND id = :id")
    suspend fun receipt(partition: String, id: String): RelayReceipt?
    @Insert(onConflict = OnConflictStrategy.ABORT) suspend fun insertReceipt(receipt: RelayReceipt)
}

/** Separate encrypted store: this mode never opens another backend account's data. */
@Database(entities = [RelayRequest::class, RelayRecord::class, RelayReceipt::class], version = 1, exportSchema = false)
internal abstract class RelayDatabase : RoomDatabase() {
    abstract fun dao(): RelayDao
    companion object {
        @Volatile private var instance: RelayDatabase? = null
        fun get(context: Context): RelayDatabase = instance ?: synchronized(this) {
            instance ?: Room.databaseBuilder(context.applicationContext, RelayDatabase::class.java, "mouchen-relay-local.db")
                .openHelperFactory(SupportOpenHelperFactory(KeyVault(context.applicationContext).databasePassphrase()))
                .build().also { instance = it }
        }
    }
}
