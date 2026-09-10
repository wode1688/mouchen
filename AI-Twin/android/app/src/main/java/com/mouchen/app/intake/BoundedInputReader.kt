package com.mouchen.app.intake

import java.io.ByteArrayOutputStream
import java.io.InputStream

internal sealed interface BoundedByteRead {
    data class Success(val bytes: ByteArray) : BoundedByteRead
    data object TooLarge : BoundedByteRead
}

/** Reads an untrusted stream without ever retaining more than [maxBytes] in memory. */
internal fun readBoundedBytes(input: InputStream, maxBytes: Int): BoundedByteRead {
    require(maxBytes > 0)
    val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
    val output = ByteArrayOutputStream(minOf(maxBytes, DEFAULT_BUFFER_SIZE))
    return try {
        while (true) {
            val count = input.read(buffer)
            if (count < 0) break
            if (count == 0) continue
            if (output.size() > maxBytes - count) {
                output.reset()
                return BoundedByteRead.TooLarge
            }
            output.write(buffer, 0, count)
        }
        BoundedByteRead.Success(output.toByteArray())
    } finally {
        buffer.fill(0)
    }
}
