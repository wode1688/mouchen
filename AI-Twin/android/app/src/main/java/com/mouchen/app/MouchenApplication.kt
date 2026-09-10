package com.mouchen.app

import android.app.Application
import com.mouchen.app.collectors.CollectionScheduler
import com.mouchen.app.collectors.CollectionWorker
import com.mouchen.app.sync.AdviceAttentionScheduler
import com.mouchen.app.sync.legacyMigrationAllowsAuthenticatedWork
import com.mouchen.app.sync.SessionHealthWorker
import com.mouchen.app.sync.UrgentEventSyncWorker
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

class MouchenApplication : Application() {
    /** Keeps bounded, user-authorized intake alive across Activity recreation. */
    internal val intakeScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onCreate() {
        super.onCreate()
        System.loadLibrary("sqlcipher")
        runCatching { com.mouchen.app.relay.RelaySyncWorker.configure(this) }
        if (!legacyMigrationAllowsAuthenticatedWork(this)) return
        CollectionScheduler.ensurePeriodic(this)
        SessionHealthWorker.enqueueNow(this)
        AdviceAttentionScheduler.enqueueNow(this)
        UrgentEventSyncWorker.recover(this)
        CollectionWorker.enqueueStartup(this, "app_start")
    }
}
