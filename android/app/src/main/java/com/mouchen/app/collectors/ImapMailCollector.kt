package com.mouchen.app.collectors

import android.content.Context
import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.data.MouchenDao
import com.mouchen.app.models.SecureSettingsStore
import com.mouchen.app.sync.RealtimeSyncWorker
import java.security.MessageDigest
import java.util.Properties
import javax.mail.Folder
import javax.mail.Message
import javax.mail.Multipart
import javax.mail.Part
import javax.mail.Session
import javax.mail.UIDFolder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

enum class MailAuthMode { APP_PASSWORD, OAUTH2_ACCESS_TOKEN }

data class MailConnectionConfig(
    val id: String,
    val emailAddress: String,
    val domain: String = "事业",
    val host: String,
    val port: Int = 993,
    val username: String,
    val secret: String,
    val authMode: MailAuthMode,
    val useTls: Boolean = true,
    val folder: String = "INBOX",
    val enabled: Boolean = true,
) {
    override fun toString(): String =
        "MailConnectionConfig(id=$id, emailAddress=$emailAddress, host=$host, port=$port, username=$username, " +
        "secret=<redacted>, authMode=$authMode, useTls=$useTls, folder=$folder, enabled=$enabled)"
}

/** The complete account record, including credentials, is encrypted with a Keystore-held key. */
class MailConnectionStore(context: Context) {
    private val secureStore = SecureSettingsStore(context, "mouchen_mail_connections")

    fun save(accounts: List<MailConnectionConfig>) {
        val array = JSONArray()
        accounts.forEach { account ->
            require(account.id.isNotBlank() && account.host.isNotBlank() && account.username.isNotBlank()) { "Mail account fields are required" }
            require(account.secret.isNotBlank()) { "A user-entered app password or OAuth access token is required" }
            require(account.port in 1..65535) { "Invalid IMAP port" }
            array.put(
                JSONObject()
                    .put("id", account.id)
                    .put("email_address", account.emailAddress)
                    .put("domain", account.domain)
                    .put("host", account.host)
                    .put("port", account.port)
                    .put("username", account.username)
                    .put("secret", account.secret)
                    .put("auth_mode", account.authMode.name)
                    .put("use_tls", account.useTls)
                    .put("folder", account.folder)
                    .put("enabled", account.enabled),
            )
        }
        secureStore.put(KEY, array.toString())
    }

    fun load(): List<MailConnectionConfig> = runCatching {
        val array = JSONArray(secureStore.get(KEY) ?: "[]")
        buildList {
            for (index in 0 until array.length()) {
                val json = array.getJSONObject(index)
                add(
                    MailConnectionConfig(
                        id = json.getString("id"),
                        emailAddress = json.optString("email_address"),
                        domain = json.optString("domain", "事业"),
                        host = json.getString("host"),
                        port = json.optInt("port", 993),
                        username = json.getString("username"),
                        secret = json.getString("secret"),
                        authMode = MailAuthMode.valueOf(json.getString("auth_mode")),
                        useTls = json.optBoolean("use_tls", true),
                        folder = json.optString("folder", "INBOX"),
                        enabled = json.optBoolean("enabled", true),
                    ),
                )
            }
        }
    }.getOrDefault(emptyList())

    fun clear() = secureStore.remove(KEY)

    fun clearDurably(): Boolean = secureStore.removeDurably(KEY)

    private companion object {
        const val KEY = "accounts"
    }
}

class ImapMailCollector(
    context: Context,
    private val dao: MouchenDao,
    private val connectionStore: MailConnectionStore,
) {
    private val appContext = context.applicationContext
    private val watermarks = context.applicationContext.getSharedPreferences("mouchen_imap_watermarks", Context.MODE_PRIVATE)

    suspend fun collect(): Int = withContext(Dispatchers.IO) {
        var collected = 0
        connectionStore.load().filter { it.enabled }.forEach { account ->
            collected += runCatching { collectAccount(account) }.getOrDefault(0)
        }
        collected
    }

    private suspend fun collectAccount(account: MailConnectionConfig): Int {
        val protocol = if (account.useTls) "imaps" else "imap"
        val properties = Properties().apply {
            setProperty("mail.store.protocol", protocol)
            setProperty("mail.$protocol.ssl.enable", account.useTls.toString())
            setProperty("mail.$protocol.starttls.enable", (!account.useTls).toString())
            setProperty("mail.$protocol.connectiontimeout", TIMEOUT_MS.toString())
            setProperty("mail.$protocol.timeout", TIMEOUT_MS.toString())
            setProperty("mail.$protocol.writetimeout", TIMEOUT_MS.toString())
            if (account.authMode == MailAuthMode.OAUTH2_ACCESS_TOKEN) {
                setProperty("mail.$protocol.auth.mechanisms", "XOAUTH2")
                setProperty("mail.$protocol.auth.login.disable", "true")
                setProperty("mail.$protocol.auth.plain.disable", "true")
            }
        }
        val store = Session.getInstance(properties).getStore(protocol)
        try {
            store.connect(account.host, account.port, account.username, account.secret)
            val rawFolder = store.getFolder(account.folder)
            rawFolder.open(Folder.READ_ONLY)
            try {
                val uidFolder = rawFolder as? UIDFolder ?: error("IMAP server does not expose stable UIDs")
                val accountKey = sha256(account.id)
                val validityKey = "$accountKey.uid_validity"
                val watermarkKey = "$accountKey.last_uid"
                val validity = uidFolder.uidValidity
                if (watermarks.getLong(validityKey, -1L) != validity) {
                    watermarks.edit().putLong(validityKey, validity).putLong(watermarkKey, 0L).apply()
                }
                if (rawFolder.messageCount == 0) return 0
                val lastUid = watermarks.getLong(watermarkKey, 0L)
                val firstSequence = firstSequenceAfterUid(rawFolder.messageCount, lastUid) { sequence ->
                    uidFolder.getUID(rawFolder.getMessage(sequence))
                } ?: return 0
                val lastSequence = minOf(rawFolder.messageCount, firstSequence + BATCH_SIZE - 1)
                val messages = rawFolder.getMessages(firstSequence, lastSequence)
                var inserted = 0
                var lastProcessedUid = lastUid
                messages.forEach { message ->
                    val uid = uidFolder.getUID(message)
                    if (uid <= 0) return@forEach
                    val event = toLocalEvent(account, uid, message)
                    if (dao.insertEventIfAbsent(event) != -1L) {
                        inserted += 1
                        RealtimeSyncWorker.enqueueForEvent(appContext, event)
                    }
                    lastProcessedUid = maxOf(lastProcessedUid, uid)
                }
                if (lastProcessedUid > lastUid) {
                    watermarks.edit().putLong(watermarkKey, lastProcessedUid).apply()
                }
                return inserted
            } finally {
                if (rawFolder.isOpen) rawFolder.close(false)
            }
        } finally {
            if (store.isConnected) store.close()
        }
    }

    private fun toLocalEvent(account: MailConnectionConfig, uid: Long, message: Message): LocalEventEntity {
        val extracted = extract(message)
        val occurredAt = (message.receivedDate ?: message.sentDate)?.time ?: System.currentTimeMillis()
        return LocalEventEntity(
            id = "mail-${sha256("${account.id}|$uid")}",
            source = "imap.${account.id}".take(80),
            type = "mail.received",
            occurredAt = occurredAt,
            sensitivity = "restricted",
            payloadJson = JSONObject()
                .put("account", account.emailAddress)
                .put("domain", account.domain)
                .put("uid", uid)
                .put("message_id", message.getHeader("Message-ID")?.firstOrNull())
                .put("subject", message.subject.orEmpty().take(2000))
                .put("from", JSONArray(message.from?.map { it.toString() } ?: emptyList<String>()))
                .put("to", JSONArray(message.getRecipients(Message.RecipientType.TO)?.map { it.toString() } ?: emptyList<String>()))
                .put("sent_at", message.sentDate?.time ?: JSONObject.NULL)
                .put("received_at", message.receivedDate?.time ?: JSONObject.NULL)
                .put("body", extracted.text)
                .put("body_truncated", extracted.truncated)
                .put("attachments", extracted.attachments)
                .toString(),
        )
    }

    private fun extract(part: Part): ExtractedContent {
        val text = StringBuilder()
        val attachments = JSONArray()
        var truncated = false

        fun append(value: String) {
            if (text.length >= MAX_BODY_CHARS) {
                truncated = true
                return
            }
            val remaining = MAX_BODY_CHARS - text.length
            text.append(value.take(remaining))
            if (value.length > remaining) truncated = true
        }

        fun visit(current: Part) {
            val disposition = current.disposition
            val isAttachment = disposition.equals(Part.ATTACHMENT, ignoreCase = true) || !current.fileName.isNullOrBlank()
            if (isAttachment) {
                attachments.put(
                    JSONObject()
                        .put("name", current.fileName.orEmpty().take(500))
                        .put("content_type", current.contentType.orEmpty().take(500))
                        .put("size", current.size),
                )
                return
            }
            when {
                current.isMimeType("text/plain") || current.isMimeType("text/html") -> append(current.content?.toString().orEmpty())
                current.isMimeType("multipart/*") -> {
                    val multipart = current.content as? Multipart ?: return
                    for (index in 0 until multipart.count) visit(multipart.getBodyPart(index))
                }
                current.isMimeType("message/rfc822") -> (current.content as? Part)?.let(::visit)
            }
        }

        visit(part)
        return ExtractedContent(text.toString(), attachments, truncated)
    }

    private fun sha256(value: String): String = MessageDigest.getInstance("SHA-256")
        .digest(value.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }

    private data class ExtractedContent(val text: String, val attachments: JSONArray, val truncated: Boolean)

    private companion object {
        const val TIMEOUT_MS = 30_000
        const val BATCH_SIZE = 100
        const val MAX_BODY_CHARS = 1_000_000
    }
}

internal fun firstSequenceAfterUid(
    messageCount: Int,
    lastUid: Long,
    uidAtSequence: (Int) -> Long,
): Int? {
    // IMAP sequence numbers are dense and ordered by UID, so UID gaps do not become empty batches.
    var low = 1
    var high = messageCount
    var result: Int? = null
    while (low <= high) {
        val middle = low + (high - low) / 2
        if (uidAtSequence(middle) > lastUid) {
            result = middle
            high = middle - 1
        } else {
            low = middle + 1
        }
    }
    return result
}
