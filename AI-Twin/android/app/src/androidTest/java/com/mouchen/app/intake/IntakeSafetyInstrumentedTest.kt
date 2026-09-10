package com.mouchen.app.intake

import android.net.Uri
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.io.File
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class IntakeSafetyInstrumentedTest {
    @Test
    fun conversationImporterRejectsDirectFileUrisBeforeReading() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val file = File(context.cacheDir, "must-not-be-opened.txt")

        val result = ConversationTextImporter(context).read(Uri.fromFile(file))

        assertEquals(ConversationImportResult.UnsupportedUri, result)
    }
}
