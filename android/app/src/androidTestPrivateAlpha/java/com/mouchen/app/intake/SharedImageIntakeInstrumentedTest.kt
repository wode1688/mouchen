package com.mouchen.app.intake

import android.content.ContentValues
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import android.provider.MediaStore
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.util.UUID
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class SharedImageIntakeInstrumentedTest {
    @Test
    fun privateAlphaReadsContentUriAndRunsBundledOcr() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        val resolver = context.contentResolver
        val bitmap = Bitmap.createBitmap(1_200, 400, Bitmap.Config.ARGB_8888)
        Canvas(bitmap).apply {
            drawColor(Color.WHITE)
            drawText(
                "HELLO 123",
                50f,
                260f,
                Paint(Paint.ANTI_ALIAS_FLAG).apply {
                    color = Color.BLACK
                    textSize = 180f
                    typeface = Typeface.DEFAULT_BOLD
                },
            )
        }
        val uri = requireNotNull(
            resolver.insert(
                MediaStore.Images.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY),
                ContentValues().apply {
                    put(MediaStore.Images.Media.DISPLAY_NAME, "mouchen-ocr-${UUID.randomUUID()}.png")
                    put(MediaStore.Images.Media.MIME_TYPE, "image/png")
                    put(MediaStore.Images.Media.RELATIVE_PATH, "Pictures/MouchenTests")
                    put(MediaStore.Images.Media.IS_PENDING, 1)
                },
            ),
        )
        try {
            resolver.openOutputStream(uri, "w")!!.use { output ->
                check(bitmap.compress(Bitmap.CompressFormat.PNG, 100, output))
            }
            resolver.update(
                uri,
                ContentValues().apply { put(MediaStore.Images.Media.IS_PENDING, 0) },
                null,
                null,
            )

            val result = SharedImageIntake(context).process(uri, "image/png")

            assertTrue("OCR failed with $result", result is SharedImageIntakeResult.Success)
            assertTrue((result as SharedImageIntakeResult.Success).characterCount >= 3)
        } finally {
            resolver.delete(uri, null, null)
            bitmap.recycle()
        }
    }
}
