package com.mouchen.app.security

import android.content.Context
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.InputStream

class EncryptedBlobStore(context: Context) {
    private val keyVault = KeyVault(context)
    private val root = File(context.noBackupFilesDir, "encrypted_capture").apply { mkdirs() }

    fun write(name: String, content: ByteArray): File {
        val target = File(root, safeName(name))
        val temp = File(root, ".${target.name}.tmp")
        temp.writeBytes(keyVault.encrypt(content))
        check(temp.renameTo(target)) { "Unable to commit encrypted blob" }
        return target
    }

    fun openChunked(name: String): EncryptedChunkWriter {
        return EncryptedChunkWriter(File(root, safeName(name)), keyVault)
    }

    /** Decrypts one bounded capture segment in memory; no plaintext file is created. */
    fun readChunked(name: String, maxPlainBytes: Int): ByteArray {
        require(maxPlainBytes > 0) { "maxPlainBytes must be positive" }
        val source = File(root, safeName(name))
        FileInputStream(source).use { input ->
            return decodeEncryptedChunks(input, maxPlainBytes, keyVault::decrypt)
        }
    }

    fun prune(cutoff: Long, maxBytes: Long): Int {
        require(maxBytes >= 0L) { "maxBytes must not be negative" }
        val files = root.listFiles().orEmpty().filter(File::isFile)
        val byName = files.associateBy(File::getName)
        return selectBlobsForDeletion(
            files.map { BlobRetentionEntry(it.name, it.lastModified(), it.length()) },
            cutoff,
            maxBytes,
        ).count { name -> runCatching { byName[name]?.delete() == true }.getOrDefault(false) }
    }

    /** Account logout removes every bounded capture blob without touching any broader directory. */
    fun clear(): Int = root.listFiles().orEmpty()
        .filter(File::isFile)
        .count { file -> runCatching { file.delete() }.getOrDefault(false) }

    /** Account switching fails closed unless every capture blob is gone. */
    fun clearDurably(): Boolean = root.listFiles().orEmpty()
        .filter(File::isFile)
        .all { file -> runCatching { !file.exists() || file.delete() }.getOrDefault(false) }

    /** Metadata-only inventory used to repair a capture-to-ledger crash window. */
    internal fun inventory(): List<BlobRetentionEntry> = root.listFiles().orEmpty()
        .asSequence()
        .filter(File::isFile)
        .map { BlobRetentionEntry(it.name, it.lastModified(), it.length()) }
        .toList()

    private fun safeName(value: String): String = value.replace(Regex("[^A-Za-z0-9._-]"), "_")
}

internal data class BlobRetentionEntry(
    val name: String,
    val modifiedAt: Long,
    val sizeBytes: Long,
)

internal fun selectBlobsForDeletion(
    entries: List<BlobRetentionEntry>,
    cutoff: Long,
    maxBytes: Long,
): Set<String> {
    require(maxBytes >= 0L) { "maxBytes must not be negative" }
    val expired = entries.filter { it.modifiedAt < cutoff }.mapTo(mutableSetOf(), BlobRetentionEntry::name)
    var retainedBytes = 0L
    entries.asSequence()
        .filterNot { it.name in expired }
        .sortedWith(compareByDescending<BlobRetentionEntry> { it.modifiedAt }.thenByDescending { it.name })
        .forEach { entry ->
            val size = entry.sizeBytes.coerceAtLeast(0L)
            if (size > maxBytes - retainedBytes) {
                expired += entry.name
            } else {
                retainedBytes += size
            }
        }
    return expired
}

class EncryptedChunkWriter(file: File, private val keyVault: KeyVault) : AutoCloseable {
    private val output = DataOutputStream(FileOutputStream(file)).apply { writeInt(ENCRYPTED_CHUNK_MAGIC) }

    @Synchronized
    fun write(plain: ByteArray, length: Int = plain.size) {
        val encrypted = keyVault.encrypt(plain.copyOf(length))
        output.writeInt(encrypted.size)
        output.write(encrypted)
    }

    override fun close() = output.close()

}

internal fun decodeEncryptedChunks(
    input: InputStream,
    maxPlainBytes: Int,
    decrypt: (ByteArray) -> ByteArray,
): ByteArray {
    require(maxPlainBytes > 0) { "maxPlainBytes must be positive" }
    val source = DataInputStream(input)
    require(source.readInt() == ENCRYPTED_CHUNK_MAGIC) { "Invalid encrypted chunk file" }
    val output = ByteArrayOutputStream(minOf(maxPlainBytes, 1024 * 1024))
    var total = 0
    while (true) {
        val firstLengthByte = source.read()
        if (firstLengthByte < 0) break
        val encryptedLength = (firstLengthByte shl 24) or
            (source.readUnsignedByte() shl 16) or
            (source.readUnsignedByte() shl 8) or
            source.readUnsignedByte()
        require(encryptedLength in 1..MAX_ENCRYPTED_CHUNK_BYTES) { "Invalid encrypted chunk length" }
        val encrypted = ByteArray(encryptedLength)
        source.readFully(encrypted)
        val plain = decrypt(encrypted)
        require(plain.size <= maxPlainBytes - total) { "Decrypted segment exceeds limit" }
        output.write(plain)
        total += plain.size
        encrypted.fill(0)
        plain.fill(0)
    }
    return output.toByteArray()
}

internal const val ENCRYPTED_CHUNK_MAGIC = 0x4D434831
private const val MAX_ENCRYPTED_CHUNK_BYTES = 1024 * 1024
