package com.mouchen.app.collectors

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlinx.coroutines.runBlocking
import okio.Buffer
import java.io.IOException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.random.Random

class ImeVoiceRecognitionTest {
    @Test
    fun candidatesAreTrimmedDeduplicatedAndKeepMatchingConfidence() {
        val candidates = normalizeVoiceCandidates(
            listOf(" 你好 ", "你好", "明天   开会", "", "备用结果"),
            floatArrayOf(0.94f, 0.91f, 0.73f, 0.4f, -1f),
        )

        assertEquals(listOf("你好", "明天 开会", "备用结果"), candidates.map { it.text })
        assertEquals(0.94f, candidates[0].confidence)
        assertEquals(0.73f, candidates[1].confidence)
        assertNull(candidates[2].confidence)
    }

    @Test
    fun invalidOrMissingConfidenceNeverDropsRecognitionText() {
        val candidates = normalizeVoiceCandidates(
            listOf("第一项", "第二项", "第三项"),
            floatArrayOf(Float.NaN, 1.5f),
        )

        assertEquals(3, candidates.size)
        assertTrue(candidates.all { it.confidence == null })
    }

    @Test
    fun candidatesRespectVisibleLimit() {
        val candidates = normalizeVoiceCandidates((1..20).map { "候选$it" }, limit = 5)
        assertEquals(listOf("候选1", "候选2", "候选3", "候选4", "候选5"), candidates.map { it.text })
    }

    @Test
    fun silenceDetectorStopsAfterMandarinPhraseAndTrailingSilence() {
        val detector = MandarinSilenceDetector()
        repeat(3) {
            assertEquals(VoiceFrameDecision.CONTINUE, detector.accept(pcmFrame(amplitude = 3_000), 3_200))
        }
        var decision = VoiceFrameDecision.CONTINUE
        repeat(11) {
            decision = detector.accept(pcmFrame(amplitude = 0), 3_200)
        }

        assertEquals(VoiceFrameDecision.STOP_WITH_SPEECH, decision)
        assertTrue(detector.hasSpeech())
    }

    @Test
    fun silenceDetectorRejectsSevenSecondsWithoutSpeech() {
        val detector = MandarinSilenceDetector()
        var decision = VoiceFrameDecision.CONTINUE
        repeat(70) {
            decision = detector.accept(pcmFrame(amplitude = 0), 3_200)
        }

        assertEquals(VoiceFrameDecision.STOP_NO_SPEECH, decision)
        assertTrue(!detector.hasSpeech())
    }

    @Test
    fun backendErrorsGiveActionableChineseMessages() {
        assertEquals("请先在AI替身连接页配置语音识别地址", voiceBackendErrorMessage("speech_to_text_not_configured"))
        assertEquals("语音识别服务正忙，请稍后重试", voiceBackendErrorMessage("voice_stt_http_429"))
        assertEquals("无法连接语音识别服务，请检查网络", voiceBackendErrorMessage("voice_stt_unreachable"))
    }

    @Test
    fun progressNeverReturnsAfterStopOrTerminal() {
        val random = Random(20260804)
        repeat(10_000) {
            val gate = VoiceSessionGate()
            var closed = false
            var terminalWins = 0
            repeat(20) {
                when (random.nextInt(4)) {
                    0 -> if (gate.requestStop()) closed = true
                    1 -> {
                        if (gate.markTerminal()) terminalWins += 1
                        closed = true
                    }
                    2 -> gate.markSpeechEnded().also { closed = true }
                    else -> if (closed) assertFalse(gate.canPublishProgress())
                }
                if (closed) assertFalse(gate.canPublishProgress())
            }
            assertTrue(terminalWins <= 1)
        }
    }

    @Test
    fun backendListeningIsPublishedOnceAndNeverAfterStopOrTerminal() {
        val active = BackendVoiceStateGate()
        assertTrue(active.markRecordingStarted())
        assertFalse(active.markRecordingStarted())

        val stopped = BackendVoiceStateGate()
        assertTrue(stopped.requestStop())
        assertFalse(stopped.markRecordingStarted())
        assertFalse(stopped.canPublishProgress())

        val terminal = BackendVoiceStateGate()
        assertTrue(terminal.markTerminal())
        assertFalse(terminal.markRecordingStarted())
        assertFalse(terminal.canPublishProgress())
    }

    @Test
    fun voiceHttpClientUsesBoundedParaformerTimeouts() {
        val client = buildImeVoiceHttpClient()

        assertEquals(8_000, client.connectTimeoutMillis)
        assertEquals(10_000, client.writeTimeoutMillis)
        assertEquals(15_000, client.readTimeoutMillis)
        assertEquals(20_000, client.callTimeoutMillis)
    }

    @Test
    fun failedVoiceCommitPreservesCandidatesUntilACommitSucceeds() {
        var clearCount = 0

        assertFalse(runVoiceCandidateCommit(commit = { false }, clearResults = { clearCount += 1 }))
        assertEquals(0, clearCount)
        assertTrue(runVoiceCandidateCommit(commit = { true }, clearResults = { clearCount += 1 }))
        assertEquals(1, clearCount)
    }

    @Test
    fun microphoneLeaseHasExactlyOneOwnerAndCloseIsIdempotent() {
        val first = ProcessMicrophoneLease.tryAcquire(MicrophoneUse.IME_DICTATION)
        assertNotNull(first)
        assertEquals(MicrophoneUse.IME_DICTATION, ProcessMicrophoneLease.currentUse())
        assertNull(ProcessMicrophoneLease.tryAcquire(MicrophoneUse.CONTINUOUS_CAPTURE))

        first!!.close()
        first.close()
        val second = ProcessMicrophoneLease.tryAcquire(MicrophoneUse.CONTINUOUS_CAPTURE)
        assertNotNull(second)
        second!!.close()
        assertNull(ProcessMicrophoneLease.currentUse())
    }

    @Test
    fun cancelBeforeRecorderStartsCannotBeReset() = runBlocking {
        val lease = requireNotNull(ProcessMicrophoneLease.tryAcquire(MicrophoneUse.IME_DICTATION))
        val recorder = ShortMandarinRecorder(lease)
        recorder.cancel()

        assertEquals(VoiceCaptureResult.Cancelled, recorder.capture())
        assertNull(ProcessMicrophoneLease.currentUse())
    }

    @Test
    fun stopBeforeRecorderStartsCannotBeReset() = runBlocking {
        val lease = requireNotNull(ProcessMicrophoneLease.tryAcquire(MicrophoneUse.IME_DICTATION))
        val recorder = ShortMandarinRecorder(lease)
        recorder.requestStop()

        assertEquals(VoiceCaptureResult.NoSpeech, recorder.capture())
        assertNull(ProcessMicrophoneLease.currentUse())
    }

    @Test
    fun uploadAuditTransitionsStayOrderedDuringCancelRace() {
        val executor = Executors.newFixedThreadPool(2)
        try {
            repeat(1_000) {
                val events = mutableListOf<Pair<String, VoiceUploadAuditSnapshot>>()
                val gate = VoiceUploadAuditGate { phase, snapshot, _ -> events += phase to snapshot }
                assertTrue(gate.markAttempted(6_400))
                val start = CountDownLatch(1)
                val upload = executor.submit {
                    start.await()
                    gate.markUploadStarted()
                }
                val cancel = executor.submit {
                    start.await()
                    gate.markTerminal("cancelled")
                }
                start.countDown()
                upload.get(2, TimeUnit.SECONDS)
                cancel.get(2, TimeUnit.SECONDS)

                assertEquals("attempted", events.first().first)
                assertEquals("cancelled", events.last().first)
                assertEquals(1, events.count { it.first == "cancelled" })
                assertTrue(events.all { (_, state) -> !state.uploadStarted || state.attempted })
                val terminal = events.last().second
                assertTrue(terminal.terminal)
                assertEquals(
                    events.any { it.first == "request_body_started" },
                    terminal.uploadStarted,
                )
                assertFalse(gate.markUploadStarted())
            }
        } finally {
            executor.shutdownNow()
        }
    }

    @Test
    fun deniedUploadWritesNoPcmAndAcceptedUploadWritesExactlyOnce() {
        val pcm = byteArrayOf(1, 2, 3, 4)
        val deniedSink = Buffer()
        var deniedCallbacks = 0
        val denied = PcmRequestBody(pcm) {
            deniedCallbacks += 1
            false
        }
        try {
            denied.writeTo(deniedSink)
            throw AssertionError("Denied upload must fail before writing PCM")
        } catch (_: IOException) {
            // Expected.
        }
        assertEquals(1, deniedCallbacks)
        assertEquals(0L, deniedSink.size)

        val acceptedSink = Buffer()
        var acceptedCallbacks = 0
        PcmRequestBody(pcm) {
            acceptedCallbacks += 1
            true
        }.writeTo(acceptedSink)
        assertEquals(1, acceptedCallbacks)
        assertTrue(pcm.contentEquals(acceptedSink.readByteArray()))
    }

    private fun pcmFrame(amplitude: Int): ByteArray {
        val frame = ByteArray(3_200)
        var index = 0
        var sign = 1
        while (index < frame.size) {
            val sample = (amplitude * sign).toShort().toInt()
            frame[index] = (sample and 0xff).toByte()
            frame[index + 1] = ((sample ushr 8) and 0xff).toByte()
            sign *= -1
            index += 2
        }
        return frame
    }
}
