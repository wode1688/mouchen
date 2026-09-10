package com.mouchen.app.security

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import org.junit.Assert.assertEquals
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class EncryptedBlobStoreTest {
    @Test
    fun retentionDeletesExpiredBlobsBeforeApplyingCapacity() {
        val entries = listOf(
            BlobRetentionEntry("expired", modifiedAt = 10, sizeBytes = 1),
            BlobRetentionEntry("newest", modifiedAt = 30, sizeBytes = 6),
            BlobRetentionEntry("older", modifiedAt = 20, sizeBytes = 5),
        )

        assertEquals(
            setOf("expired", "older"),
            selectBlobsForDeletion(entries, cutoff = 15, maxBytes = 10),
        )
    }

    @Test
    fun retentionKeepsNewestBlobsWithinCapacity() {
        val entries = listOf(
            BlobRetentionEntry("first", modifiedAt = 100, sizeBytes = 4),
            BlobRetentionEntry("second", modifiedAt = 90, sizeBytes = 4),
            BlobRetentionEntry("third", modifiedAt = 80, sizeBytes = 4),
        )

        assertEquals(
            setOf("third"),
            selectBlobsForDeletion(entries, cutoff = 0, maxBytes = 8),
        )
    }

    @Test
    fun chunkReaderReassemblesDecryptedBytesWithoutPlainFile() {
        val encoded = ByteArrayOutputStream().also { bytes ->
            DataOutputStream(bytes).use { output ->
                output.writeInt(ENCRYPTED_CHUNK_MAGIC)
                listOf("enc-one", "enc-two").forEach { value ->
                    val chunk = value.encodeToByteArray()
                    output.writeInt(chunk.size)
                    output.write(chunk)
                }
            }
        }.toByteArray()

        val decoded = decodeEncryptedChunks(ByteArrayInputStream(encoded), 32) { encrypted ->
            encrypted.decodeToString().removePrefix("enc-").encodeToByteArray()
        }

        assertArrayEquals("onetwo".encodeToByteArray(), decoded)
    }

    @Test
    fun chunkReaderRejectsOversizedPlainContent() {
        val encoded = ByteArrayOutputStream().also { bytes ->
            DataOutputStream(bytes).use { output ->
                output.writeInt(ENCRYPTED_CHUNK_MAGIC)
                output.writeInt(1)
                output.writeByte(1)
            }
        }.toByteArray()

        assertThrows(IllegalArgumentException::class.java) {
            decodeEncryptedChunks(ByteArrayInputStream(encoded), 2) { byteArrayOf(1, 2, 3) }
        }
    }
}
