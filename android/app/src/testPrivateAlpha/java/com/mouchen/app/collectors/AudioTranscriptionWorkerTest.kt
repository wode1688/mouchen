package com.mouchen.app.collectors

import com.mouchen.app.data.AudioTranscriptionJobEntity
import com.mouchen.app.security.BlobRetentionEntry
import com.mouchen.app.sync.AudioProcessingLocation
import com.mouchen.app.sync.BackendConnection
import com.mouchen.app.sync.isPrivateNodeUrl
import java.io.IOException
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.Buffer
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AudioTranscriptionWorkerTest {
    @Test
    fun destinationFingerprintChangesWithUrlLocationOrOwnerButNotTokenRotation() {
        val original = BackendConnection(
            baseUrl = "https://api.example.com",
            userId = "owner-1",
            bearerToken = "token-a",
            sttBaseUrl = "https://speech.example.com/private",
            sttProcessingLocation = AudioProcessingLocation.PRIVATE_VPS,
        )
        val fingerprint = audioDestinationFingerprint(original)!!

        assertTrue(validAudioDestinationFingerprint(fingerprint))
        assertEquals(fingerprint, audioDestinationFingerprint(original.copy(bearerToken = "token-b")))
        assertFalse(fingerprint == audioDestinationFingerprint(original.copy(userId = "owner-2")))
        assertFalse(fingerprint == audioDestinationFingerprint(original.copy(sttBaseUrl = "https://other.example.com")))
        assertFalse(
            fingerprint == audioDestinationFingerprint(
                original.copy(sttProcessingLocation = AudioProcessingLocation.PUBLIC_CLOUD),
            ),
        )
    }

    @Test
    fun productionAudioClientNeverFollowsRedirects() {
        val client = buildLocalSttHttpClient()

        assertFalse(client.followRedirects)
        assertFalse(client.followSslRedirects)
    }

    @Test
    fun staleEncryptedAudioWithoutLedgerIsSelectedForRecovery() {
        val now = 1_700_000_600_000L
        val session = 1_700_000_000_000L
        val selected = selectOrphanAudioSegments(
            listOf(
                BlobRetentionEntry("audio-$session-segment-0002.pcm.mch", now - 180_000L, 64_000L),
                BlobRetentionEntry("audio-$session-segment-0003.pcm.mch", now - 30_000L, 64_000L),
                BlobRetentionEntry("screen-private.mch", now - 180_000L, 64_000L),
                BlobRetentionEntry("audio-$session-segment-0004.pcm.mch", now - 180_000L, 128L),
            ),
            now,
            maxEntries = 10,
        )

        assertEquals(1, selected.size)
        assertEquals("audio-$session-segment-0002.pcm.mch", selected.single().blobName)
        assertEquals(session + 60_000L, selected.single().startedAt)
        assertEquals(now - 180_000L, selected.single().endedAt)
    }

    @Test
    fun knownRecentSegmentsCannotStarveAnOlderOrphan() {
        val now = 1_700_100_000_000L
        val session = now - 20_000_000L
        val knownEntries = (1..256).map { index ->
            BlobRetentionEntry(
                "audio-$session-segment-${index.toString().padStart(4, '0')}.pcm.mch",
                now - 180_000L - index,
                64_000L,
            )
        }
        val orphan = BlobRetentionEntry(
            "audio-$session-segment-0000.pcm.mch",
            now - 181_000L,
            64_000L,
        )

        val selected = selectOrphanAudioSegments(
            knownEntries + orphan,
            now,
            maxEntries = 1,
            knownBlobNames = knownEntries.mapTo(mutableSetOf()) { it.name },
        )

        assertEquals(listOf(orphan.name), selected.map { it.blobName })
    }

    @Test
    fun authenticatedPrivateNodeReceivesRawPcmAndReturnsTranscript() = runBlocking {
        val captured = AtomicReference<Request>()
        val http = OkHttpClient.Builder()
            .addInterceptor { chain ->
                val request = chain.request()
                captured.set(request)
                Response.Builder()
                    .request(request)
                    .protocol(Protocol.HTTP_1_1)
                    .code(200)
                    .message("ok")
                    .body(
                        """{"transcript":"我需要处理支付失败","language":"zh","duration_ms":1000,"engine":"local-ffmpeg-whisper.cpp"}"""
                            .toResponseBody(),
                    )
                    .build()
            }
            .build()
        val client = LocalSttClient(
            connectionProvider = {
                BackendConnection(
                    baseUrl = "https://192.168.50.10:8788",
                    userId = "owner-1",
                    bearerToken = "secret-token",
                    sttBaseUrl = "https://192.168.50.10:8788",
                    sttProcessingLocation = AudioProcessingLocation.TRUSTED_LAN,
                )
            },
            client = http,
        )
        val pcm = byteArrayOf(1, 2, 3, 4)

        val attempt = client.transcribe(pcm, "segment-1")

        assertTrue(attempt is LocalSttAttempt.Completed)
        assertEquals("我需要处理支付失败", (attempt as LocalSttAttempt.Completed).transcript.text)
        assertEquals(AudioProcessingLocation.TRUSTED_LAN, attempt.processingLocation)
        with(captured.get()) {
            assertEquals("/v1/audio/transcribe", url.encodedPath)
            assertEquals("16000", url.queryParameter("sample_rate"))
            assertEquals("Bearer secret-token", header("Authorization"))
            assertEquals("owner-1", header("X-User-Id"))
            assertEquals("true", header("X-Local-STT-Only"))
            val uploaded = Buffer().also { body!!.writeTo(it) }.readByteArray()
            assertArrayEquals(pcm, uploaded)
        }
    }

    @Test
    fun publicDestinationIsBlockedBeforeNetworkRequest() = runBlocking {
        val calls = AtomicInteger()
        val http = OkHttpClient.Builder()
            .addInterceptor { chain ->
                calls.incrementAndGet()
                chain.proceed(chain.request())
            }
            .build()
        val client = LocalSttClient(
            connectionProvider = {
                BackendConnection(
                    baseUrl = "https://api.example.com",
                    bearerToken = "secret-token",
                    sttBaseUrl = "https://speech.example.com",
                    sttProcessingLocation = AudioProcessingLocation.TRUSTED_LAN,
                )
            },
            client = http,
        )

        val attempt = client.transcribe(byteArrayOf(0, 0), "segment-2")

        assertTrue(attempt is LocalSttAttempt.PermanentFailure)
        assertEquals(0, calls.get())
    }

    @Test
    fun privateNodeAddressPolicyIncludesLanEmulatorAndTailscaleOnly() {
        assertTrue(isPrivateNodeUrl("https://10.0.2.2:8788"))
        assertTrue(isPrivateNodeUrl("https://172.20.1.2:8788"))
        assertTrue(isPrivateNodeUrl("https://100.64.0.20"))
        assertTrue(isPrivateNodeUrl("https://mouchen-pc.local:8788"))
        assertTrue(isPrivateNodeUrl("https://[fd00::1]:8788"))
        assertTrue(isPrivateNodeUrl("https://[fe80::1]:8788"))
        assertFalse(isPrivateNodeUrl("http://192.168.50.10:8788"))
        assertFalse(isPrivateNodeUrl("https://8.8.8.8"))
        assertFalse(isPrivateNodeUrl("https://api.openai.com"))
        assertFalse(isPrivateNodeUrl("https://10.0.0.1.evil.example"))
        assertFalse(isPrivateNodeUrl("https://fcdn.example.com"))
        assertFalse(isPrivateNodeUrl("https://fdnode.example.com"))
        assertFalse(isPrivateNodeUrl("https://[2001:4860:4860::8888]"))
    }

    @Test
    fun remoteProcessingLocationsReportThatRawAudioReachedCloud() {
        assertFalse(rawAudioReachedCloud(AudioProcessingLocation.ON_DEVICE))
        assertFalse(rawAudioReachedCloud(AudioProcessingLocation.TRUSTED_LAN))
        assertTrue(rawAudioReachedCloud(AudioProcessingLocation.PRIVATE_VPS))
        assertTrue(rawAudioReachedCloud(AudioProcessingLocation.PUBLIC_CLOUD))
    }

    @Test
    fun privateVpsCannotUseSeparateOriginAndOnDeviceClaimIsBlocked() {
        val privateVps = BackendConnection(
            baseUrl = "https://api.example.com",
            bearerToken = "secret-token",
            sttBaseUrl = "https://private-speech.example.com",
            sttProcessingLocation = AudioProcessingLocation.PRIVATE_VPS,
        )
        val falseOnDeviceClaim = privateVps.copy(
            sttProcessingLocation = AudioProcessingLocation.ON_DEVICE,
        )

        assertEquals("speech_to_text_origin_mismatch", localSttConfigurationFailure(privateVps))
        assertEquals("on_device_stt_not_implemented", localSttConfigurationFailure(falseOnDeviceClaim))
    }

    @Test
    fun disabledMissingTokenAndPublicDestinationArePermanentWithoutNetwork() = runBlocking {
        val configurations = listOf(
            BackendConnection(
                baseUrl = "https://192.168.50.10:8788",
                bearerToken = "secret-token",
                enabled = false,
                sttBaseUrl = "https://192.168.50.10:8788",
                sttProcessingLocation = AudioProcessingLocation.TRUSTED_LAN,
            ),
            BackendConnection(
                baseUrl = "https://api.example.com",
                bearerToken = null,
                sttBaseUrl = "https://192.168.50.10:8788",
                sttProcessingLocation = AudioProcessingLocation.TRUSTED_LAN,
            ),
            BackendConnection(
                baseUrl = "https://api.example.com",
                bearerToken = "secret-token",
                sttBaseUrl = "https://speech.example.com",
                sttProcessingLocation = AudioProcessingLocation.TRUSTED_LAN,
            ),
            BackendConnection(
                baseUrl = "https://api.example.com",
                bearerToken = "secret-token",
                sttBaseUrl = "http://192.168.50.10:8788",
                sttProcessingLocation = AudioProcessingLocation.TRUSTED_LAN,
            ),
        )
        configurations.forEach { connection ->
            val calls = AtomicInteger()
            val client = LocalSttClient(
                connectionProvider = { connection },
                client = OkHttpClient.Builder()
                    .addInterceptor { chain ->
                        calls.incrementAndGet()
                        chain.proceed(chain.request())
                    }
                    .build(),
            )

            assertTrue(client.transcribe(byteArrayOf(0, 0), "config") is LocalSttAttempt.PermanentFailure)
            assertEquals(0, calls.get())
        }
    }

    @Test
    fun authNotFoundAndUnavailableResponsesNeverRetry() = runBlocking {
        listOf(401, 403, 404, 503).forEach { status ->
            assertTrue(attemptForHttpStatus(status) is LocalSttAttempt.PermanentFailure)
        }
    }

    @Test
    fun rateLimitAndTransientServerResponsesRetry() = runBlocking {
        listOf(429, 500, 502, 504, 599).forEach { status ->
            assertTrue(attemptForHttpStatus(status) is LocalSttAttempt.Retryable)
        }
    }

    @Test
    fun networkFailureRetriesButMalformedSuccessDoesNot() = runBlocking {
        val connection = BackendConnection(
            baseUrl = "https://192.168.50.10:8788",
            bearerToken = "secret-token",
            sttBaseUrl = "https://192.168.50.10:8788",
            sttProcessingLocation = AudioProcessingLocation.TRUSTED_LAN,
        )
        val networkFailure = LocalSttClient(
            connectionProvider = { connection },
            client = OkHttpClient.Builder()
                .addInterceptor { throw IOException("offline") }
                .build(),
        ).transcribe(byteArrayOf(0, 0), "offline")
        assertTrue(networkFailure is LocalSttAttempt.Retryable)

        val malformedSuccess = LocalSttClient(
            connectionProvider = { connection },
            client = OkHttpClient.Builder()
                .addInterceptor { chain ->
                    Response.Builder()
                        .request(chain.request())
                        .protocol(Protocol.HTTP_1_1)
                        .code(200)
                        .message("ok")
                        .body("not-json".toResponseBody())
                        .build()
                }
                .build(),
        ).transcribe(byteArrayOf(0, 0), "malformed")
        assertTrue(malformedSuccess is LocalSttAttempt.PermanentFailure)
    }

    @Test
    fun retryBudgetAllowsFiveTotalAttemptsOnly() {
        assertTrue(canRetryLocalStt(0))
        assertTrue(canRetryLocalStt(1))
        assertTrue(canRetryLocalStt(2))
        assertTrue(canRetryLocalStt(3))
        assertFalse(canRetryLocalStt(4))
        assertFalse(canRetryLocalStt(5))
    }

    @Test
    fun pendingCapPreservesExistingQueueAndRejectsOnlyNewSegment() {
        assertTrue(canQueueLocalStt(11, 12))
        assertFalse(canQueueLocalStt(12, 12))
        assertFalse(canQueueLocalStt(13, 12))
    }

    @Test
    fun recoveryScanRefillsSegmentsRejectedWhileQueueWasCongested() {
        val now = 10_000L
        val jobs = (0 until 14).map { index -> audioJob("segment-$index", index.toLong()) }

        val initiallyAccepted = selectAudioRecoveryJobs(jobs, now, maxJobs = 12)

        assertEquals((0 until 12).map { "segment-$it" }, initiallyAccepted.map { it.blobName })
        val afterInitialDispatch = jobs.map { job ->
            if (job.blobName in initiallyAccepted.map(AudioTranscriptionJobEntity::blobName)) {
                job.copy(status = AUDIO_JOB_ENQUEUED)
            } else {
                job
            }
        }
        val afterCapacityReturns = selectAudioRecoveryJobs(afterInitialDispatch, now + 1, maxJobs = 12)
        assertEquals(listOf("segment-12", "segment-13"), afterCapacityReturns.map { it.blobName })
    }

    @Test
    fun recoverySelectionIsDuplicateSafeAndSkipsFinishedOrActiveJobs() {
        val now = 20_000L
        val pending = audioJob("pending", 1)
        val selected = selectAudioRecoveryJobs(
            listOf(
                pending,
                pending.copy(updatedAt = 2),
                audioJob("active", 2).copy(status = AUDIO_JOB_ENQUEUED),
                audioJob("done", 3).copy(status = AUDIO_JOB_COMPLETED),
                audioJob("abandoned", 4).copy(status = AUDIO_JOB_ABANDONED),
            ),
            now,
            maxJobs = 12,
        )

        assertEquals(listOf("pending"), selected.map { it.blobName })
        assertEquals(stableWorkSuffix("pending"), stableWorkSuffix("pending"))
    }

    @Test
    fun exhaustedFailureBecomesDueForBoundedRecoveryScan() {
        val failedAt = 100_000L
        val firstRecovery = nextAudioFailureRecoveryState(
            currentRecoveryCycles = 0,
            now = failedAt,
            recoverable = true,
        )
        val deferred = audioJob("failed", 1).copy(
            status = firstRecovery.status,
            recoveryCycles = firstRecovery.recoveryCycles,
            nextAttemptAt = firstRecovery.nextAttemptAt,
        )

        assertEquals(AUDIO_JOB_PENDING, firstRecovery.status)
        assertEquals(1, firstRecovery.recoveryCycles)
        assertTrue(selectAudioRecoveryJobs(listOf(deferred), firstRecovery.nextAttemptAt - 1, 1).isEmpty())
        assertEquals(
            listOf("failed"),
            selectAudioRecoveryJobs(listOf(deferred), firstRecovery.nextAttemptAt, 1).map { it.blobName },
        )

        val exhausted = nextAudioFailureRecoveryState(
            currentRecoveryCycles = AUDIO_MAX_RECOVERY_CYCLES,
            now = failedAt,
            recoverable = true,
        )
        val unreadable = nextAudioFailureRecoveryState(
            currentRecoveryCycles = 0,
            now = failedAt,
            recoverable = shouldRecoverAudioFailure("encrypted_segment_unreadable"),
        )
        assertEquals(AUDIO_JOB_ABANDONED, exhausted.status)
        assertEquals(AUDIO_JOB_ABANDONED, unreadable.status)
        assertTrue(exhausted.nextAttemptAt == 0L)
    }

    private suspend fun attemptForHttpStatus(status: Int): LocalSttAttempt {
        val http = OkHttpClient.Builder()
            .addInterceptor { chain ->
                Response.Builder()
                    .request(chain.request())
                    .protocol(Protocol.HTTP_1_1)
                    .code(status)
                    .message("test")
                    .body("{}".toResponseBody())
                    .build()
            }
            .build()
        return LocalSttClient(
            connectionProvider = {
                BackendConnection(
                    baseUrl = "https://192.168.50.10:8788",
                    bearerToken = "secret-token",
                    sttBaseUrl = "https://192.168.50.10:8788",
                    sttProcessingLocation = AudioProcessingLocation.TRUSTED_LAN,
                )
            },
            client = http,
        ).transcribe(byteArrayOf(0, 0), "status-$status")
    }

    private fun audioJob(blobName: String, order: Long): AudioTranscriptionJobEntity =
        AudioTranscriptionJobEntity(
            blobName = blobName,
            segmentStartedAt = order + 1,
            segmentEndedAt = order + 2,
            nextAttemptAt = 0,
            updatedAt = order,
        )
}
