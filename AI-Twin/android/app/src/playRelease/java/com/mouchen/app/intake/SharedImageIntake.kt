package com.mouchen.app.intake

import android.content.Context
import android.net.Uri

/** Public builds do not expose or process shared-image OCR. */
class SharedImageIntake(context: Context) {
    init {
        context.applicationContext
    }

    suspend fun process(uri: Uri, declaredMimeType: String?): SharedImageIntakeResult {
        uri.scheme
        declaredMimeType?.length
        return SharedImageIntakeResult.Failure(SharedImageFailure.UNAVAILABLE_IN_BUILD)
    }
}
