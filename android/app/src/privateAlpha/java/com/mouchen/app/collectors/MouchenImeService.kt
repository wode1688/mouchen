package com.mouchen.app.collectors

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.inputmethodservice.InputMethodService
import android.net.Uri
import android.provider.Settings
import android.os.SystemClock
import android.text.InputType
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.ContextCompat
import com.mouchen.app.BuildConfig
import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.ime.ImeCandidate
import com.mouchen.app.ime.ImeInputMode
import com.mouchen.app.ime.ImeLearningStat
import com.mouchen.app.ime.ImeLearningStore
import com.mouchen.app.ime.ImeModeStore
import com.mouchen.app.ime.PinyinEngine
import com.mouchen.app.localization.uiText
import com.mouchen.app.sync.effectiveCollectionConsent
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.util.ArrayDeque
import java.util.LinkedHashMap

class MouchenImeService : InputMethodService() {
    private val lifecycleJob = SupervisorJob()
    private val mainScope = CoroutineScope(lifecycleJob + Dispatchers.Main.immediate)
    private val ioScope = CoroutineScope(lifecycleJob + Dispatchers.IO)
    private val candidateRequests = Channel<CandidateRequest>(Channel.CONFLATED)
    private val learningCommands = Channel<LearningCommand>(
        capacity = Channel.CONFLATED,
        onUndeliveredElement = { command -> command.completion?.complete(false) },
    )
    private val pendingCaptures = ArrayDeque<PendingCapture>()
    private val composing = StringBuilder()
    private val letterButtons = mutableListOf<Button>()
    private val learnedStats = LinkedHashMap<String, ImeLearningStat>(
        MAX_LEARNED_ENTRIES + 1,
        0.75f,
        false,
    )

    private var captureAllowed = false
    private var learningAllowed = false
    private var voiceAllowed = false
    private var forceEnglishForEditor = false
    private var sourcePackage = "unknown"
    private var shifted = false
    private var selectedMode = ImeInputMode.FULL_PINYIN
    private var engine: PinyinEngine? = null
    private var currentCandidateSnapshot: CandidateSnapshot? = null
    private var lastCommittedChinese = ""
    private var lookupGeneration = 0L
    private var editorGeneration = 0L
    private var inputRevision = 0L
    private var learningMutationEpoch = 0L
    private val expectedSelfSelectionUpdates = ArrayDeque<ExpectedSelectionUpdate>()
    private var selectionState = SelectionState.UNKNOWN
    private var lexiconFailed = false
    private var destroyed = false
    private var voiceUiState = VoiceUiState.IDLE
    private var voicePreview = ""
    private var voiceError = ""
    private var voiceCandidates: List<VoiceCandidate> = emptyList()
    private var voiceRecognitionMetadata: VoiceRecognitionMetadata? = null
    private var voiceGeneration = 0L

    private lateinit var eventWriter: BoundedEventWriter
    private lateinit var voiceController: ImeVoiceController
    private lateinit var learningSaverJob: Job
    private lateinit var learningStore: ImeLearningStore
    private lateinit var modeStore: ImeModeStore
    private lateinit var modeButton: Button
    private lateinit var voiceButton: Button
    private lateinit var compositionView: TextView
    private lateinit var candidateRow: LinearLayout

    override fun onCreate() {
        super.onCreate()
        eventWriter = BoundedEventWriter(applicationContext, ioScope, EVENT_QUEUE_CAPACITY)
        voiceController = ImeVoiceController(
            applicationContext,
            mainScope,
            ::onVoiceUpdate,
            ::recordVoiceOutboundAudit,
        )
        learningStore = ImeLearningStore(applicationContext)
        modeStore = ImeModeStore(applicationContext)
        selectedMode = modeStore.load()

        mainScope.launch(Dispatchers.Default) {
            for (request in candidateRequests) {
                val candidates = runCatching { request.evaluate() }.getOrDefault(emptyList())
                withContext(Dispatchers.Main.immediate) {
                    if (!destroyed && request.matchesCurrentState()) {
                        val snapshot = CandidateSnapshot(
                            editorGeneration = request.editorGeneration,
                            inputRevision = request.inputRevision,
                            lookupGeneration = request.lookupGeneration,
                            raw = request.raw,
                            mode = request.mode,
                            previous = request.previous,
                            candidates = candidates,
                        )
                        currentCandidateSnapshot = snapshot
                        renderCandidates(snapshot)
                    }
                }
            }
        }
        learningSaverJob = ioScope.launch {
            for (command in learningCommands) {
                val saved = learningStore.save(command.stats)
                command.completion?.complete(saved)
            }
        }
        val loadEpoch = learningMutationEpoch
        mainScope.launch {
            val (restoredLearning, loadedEngine) = withContext(Dispatchers.IO) {
                learningStore.load() to runCatching {
                    assets.open(LEXICON_ASSET).use(PinyinEngine::load)
                }
            }
            if (!destroyed) {
                if (learningMutationEpoch == loadEpoch) {
                    learnedStats.putAll(restoredLearning)
                    trimLearnedStats()
                }
                engine = loadedEngine.getOrNull()
                lexiconFailed = loadedEngine.isFailure
                refreshCandidates()
            }
        }
    }

    override fun onStartInput(attribute: EditorInfo?, restarting: Boolean) {
        cancelVoiceInput(render = false)
        val nextSourcePackage = attribute?.packageName?.take(200).orEmpty().ifBlank { "unknown" }
        val nextCredentialEditor = attribute == null || isCredentialEditor(attribute)
        val nextPersonalizationBlocked = attribute == null ||
            attribute.imeOptions and EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING != 0
        val nextForceEnglish = attribute == null || nextCredentialEditor || shouldForceAscii(attribute)
        // InputMethodService has already switched currentInputConnection here.
        // Never finish or commit an old composition into a new/credential field.
        // A restart may also follow an application-side text rewrite, so local
        // composing state is never carried across this boundary.
        clearCompositionState()
        flushCapturedText()
        super.onStartInput(attribute, restarting)
        editorGeneration += 1
        expectedSelfSelectionUpdates.clear()
        selectionState = SelectionState(
            selectionStart = attribute?.initialSelStart ?: -1,
            selectionEnd = attribute?.initialSelEnd ?: -1,
            candidatesStart = -1,
            candidatesEnd = -1,
        )
        sourcePackage = nextSourcePackage
        val knownExternalPackage = sourcePackage != "unknown" && sourcePackage != packageName
        val collectionConsent = effectiveCollectionConsent(applicationContext)
        captureAllowed = BuildConfig.ALLOW_SENSITIVE_CAPTURE &&
            collectionConsent &&
            knownExternalPackage &&
            !nextCredentialEditor &&
            !nextPersonalizationBlocked
        learningAllowed = collectionConsent && knownExternalPackage && !nextCredentialEditor && !nextPersonalizationBlocked
        voiceAllowed = collectionConsent && attribute != null && !nextCredentialEditor &&
            !nextPersonalizationBlocked && !nextForceEnglish
        forceEnglishForEditor = nextForceEnglish
        lastCommittedChinese = ""
        invalidateCandidates(render = true)
    }

    override fun onStartInputView(info: EditorInfo?, restarting: Boolean) {
        super.onStartInputView(info, restarting)
        renderMode()
        refreshCandidates()
    }

    override fun onFinishInput() {
        cancelVoiceInput(render = false)
        if (!finishCompositionPreservingText()) clearCompositionState()
        flushCapturedText()
        captureAllowed = false
        learningAllowed = false
        voiceAllowed = false
        expectedSelfSelectionUpdates.clear()
        selectionState = SelectionState.UNKNOWN
        super.onFinishInput()
    }

    override fun onFinishInputView(finishingInput: Boolean) {
        cancelVoiceInput(render = false)
        super.onFinishInputView(finishingInput)
    }

    override fun onWindowHidden() {
        cancelVoiceInput(render = false)
        super.onWindowHidden()
    }

    override fun onUpdateSelection(
        oldSelStart: Int,
        oldSelEnd: Int,
        newSelStart: Int,
        newSelEnd: Int,
        candidatesStart: Int,
        candidatesEnd: Int,
    ) {
        super.onUpdateSelection(
            oldSelStart,
            oldSelEnd,
            newSelStart,
            newSelEnd,
            candidatesStart,
            candidatesEnd,
        )
        val selfUpdate = consumeSelfSelectionUpdate(
            newSelStart,
            newSelEnd,
            candidatesStart,
            candidatesEnd,
        )
        selectionState = SelectionState(newSelStart, newSelEnd, candidatesStart, candidatesEnd)
        if (selfUpdate) {
            if (composing.isEmpty() && lastCommittedChinese.isNotEmpty()) refreshCandidates()
            return
        }
        if (voiceUiState != VoiceUiState.IDLE || voiceController.isActive()) {
            cancelVoiceInput(render = false)
        }
        if (composing.isNotEmpty() &&
            (candidatesEnd < 0 || newSelStart != candidatesEnd || newSelEnd != candidatesEnd)
        ) {
            if (finishCompositionPreservingText()) refreshCandidates()
        } else if (composing.isEmpty()) {
            inputRevision += 1
            lastCommittedChinese = ""
            invalidateCandidates(render = true)
        }
    }

    override fun onEvaluateInputViewShown(): Boolean {
        super.onEvaluateInputViewShown()
        return true
    }

    override fun onEvaluateFullscreenMode(): Boolean {
        super.onEvaluateFullscreenMode()
        return false
    }

    override fun onCreateInputView(): View {
        letterButtons.clear()
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            )
            setPadding(dp(4), dp(4), dp(4), dp(6))
            setBackgroundColor(Color.rgb(238, 240, 243))
            addView(headerRow())
            addView(candidateStrip())
            addView(numberRow())
            addView(letterRow("qwertyuiop"))
            addView(letterRow("asdfghjkl"))
            addView(letterRow("zxcvbnm", includeShift = true, includeDelete = true))
            addView(commandRow())
        }.also {
            renderMode()
            refreshCandidates()
        }
    }

    private fun headerRow(): LinearLayout = row(HEADER_HEIGHT_DP).apply {
        modeButton = keyButton(selectedMode.label, 1.6f) { cycleMode() }
        modeButton.setOnLongClickListener {
            if (forceEnglishForEditor) return@setOnLongClickListener false
            learningMutationEpoch += 1
            learnedStats.clear()
            val completion = CompletableDeferred<Boolean>()
            val accepted = learningCommands.trySend(
                LearningCommand(emptyMap(), completion),
            ).isSuccess
            refreshCandidates()
            if (!accepted) {
                Toast.makeText(
                    this@MouchenImeService,
                    "个人词频清除失败",
                    Toast.LENGTH_SHORT,
                ).show()
            } else {
                mainScope.launch {
                    val saved = withTimeoutOrNull(LEARNING_SAVE_TIMEOUT_MS) {
                        completion.await()
                    } == true
                    if (!destroyed) {
                        Toast.makeText(
                            this@MouchenImeService,
                            if (saved) "已清除个人词频" else "个人词频清除失败",
                            Toast.LENGTH_SHORT,
                        ).show()
                    }
                }
            }
            true
        }
        addView(modeButton)
        compositionView = TextView(this@MouchenImeService).apply {
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(10), 0, dp(8), 0)
            setTextColor(Color.rgb(34, 43, 56))
            textSize = 17f
            setSingleLine(true)
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.MATCH_PARENT, 5f).apply {
                setMargins(dp(2), dp(2), dp(2), dp(2))
            }
        }
        addView(compositionView)
    }

    private fun candidateStrip(): HorizontalScrollView = HorizontalScrollView(this).apply {
        isHorizontalScrollBarEnabled = false
        isFillViewport = true
        layoutParams = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            dp(CANDIDATE_HEIGHT_DP),
        )
        candidateRow = LinearLayout(this@MouchenImeService).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.MATCH_PARENT,
            )
        }
        addView(candidateRow)
    }

    private fun numberRow(): LinearLayout = row().apply {
        "1234567890".forEach { digit ->
            addView(keyButton(digit.toString(), 1f) { onDigit(digit) })
        }
    }

    private fun letterRow(
        keys: String,
        includeShift: Boolean = false,
        includeDelete: Boolean = false,
    ): LinearLayout = row().apply {
        if (includeShift) addView(keyButton("SHIFT", 1.5f) { toggleShift() })
        keys.forEach { letter ->
            val button = keyButton(letter.toString(), 1f) { onLetter(letter) }
            letterButtons += button
            addView(button)
        }
        if (includeDelete) addView(keyButton("DEL", 1.5f) { onDelete() })
    }

    private fun commandRow(): LinearLayout = row().apply {
        addView(keyButton(",", 1f) { onPunctuation(',') })
        voiceButton = keyButton("语音", 1.25f) { toggleVoiceInput() }.apply {
            contentDescription = "普通话语音输入"
        }
        addView(voiceButton)
        addView(keyButton("SPACE", 3.25f) { onSpace() })
        addView(keyButton(".", 1f) { onPunctuation('.') })
        addView(keyButton("ENTER", 1.5f) { onEnter() })
        addView(
            keyButton("NEXT", 1f) {
                clearVoiceForManualInput()
                if (commitCompositionOrRaw()) switchToNextInputMethod(false)
            },
        )
        renderVoiceButton()
    }

    private fun onLetter(letter: Char) {
        clearVoiceForManualInput()
        val mode = activeMode()
        if (mode == ImeInputMode.ENGLISH) {
            val value = if (shifted) letter.uppercaseChar() else letter
            if (commitPlainText(value.toString()) && shifted) toggleShift()
            return
        }
        if (shifted) {
            if (!commitCompositionOrRaw()) return
            if (commitPlainText(letter.uppercaseChar().toString())) toggleShift()
            return
        }
        if (composing.length >= MAX_COMPOSING_CHARS) return
        val next = composing.toString() + letter.lowercaseChar()
        val connection = currentInputConnection ?: return
        if (!runSelfSelectionEdit(expectedSetComposingText(next)) {
                connection.setComposingText(next, 1)
            }
        ) return
        composing.clear()
        composing.append(next)
        refreshCandidates()
    }

    private fun onDigit(digit: Char) {
        if (voiceUiState == VoiceUiState.RESULTS && digit != '0') {
            voiceCandidates.getOrNull(digit.digitToInt() - 1)?.let {
                commitVoiceCandidate(it)
                return
            }
        }
        clearVoiceForManualInput()
        if (activeMode() != ImeInputMode.ENGLISH && composing.isNotEmpty() && digit != '0') {
            val index = digit.digitToInt() - 1
            candidatesForCurrentComposition().getOrNull(index)?.let {
                commitCandidate(it)
                return
            }
        }
        if (!commitCompositionOrRaw()) return
        commitPlainText(digit.toString())
    }

    private fun onDelete() {
        if (voiceUiState != VoiceUiState.IDLE) {
            cancelVoiceInput(render = true)
            return
        }
        if (composing.isNotEmpty()) {
            val next = composing.dropLast(1).toString()
            val connection = currentInputConnection ?: return
            val previous = composing.toString()
            if (next.isEmpty()) {
                // Clear local state before the editor removes its composing
                // span so a synchronous callback cannot recommit stale raw text.
                composing.clear()
            }
            val updated = runSelfSelectionEdit(expectedSetComposingText(next)) {
                connection.setComposingText(next, 1)
            }
            if (!updated) {
                if (next.isEmpty()) {
                    composing.append(previous)
                    refreshCandidates()
                }
                return
            }
            if (next.isEmpty()) {
                runSelfSelectionEdit(expectedFinishComposition()) {
                    connection.finishComposingText()
                }
            }
            composing.clear()
            composing.append(next)
            refreshCandidates()
            return
        }
        val connection = currentInputConnection ?: return
        expectedSelfSelectionUpdates.clear()
        flushCapturedText()
        val deleted = runCatching {
            connection.deleteSurroundingTextInCodePoints(1, 0)
        }.getOrDefault(false)
        if (deleted) {
            selectionState = SelectionState.UNKNOWN
            inputRevision += 1
            lastCommittedChinese = ""
            refreshCandidates()
        }
    }

    private fun onSpace() {
        if (voiceUiState == VoiceUiState.RESULTS) {
            voiceCandidates.firstOrNull()?.let {
                commitVoiceCandidate(it)
                return
            }
        }
        if (voiceUiState == VoiceUiState.STARTING || voiceUiState == VoiceUiState.LISTENING) {
            voiceController.stop()
            return
        }
        clearVoiceForManualInput()
        if (composing.isNotEmpty()) {
            val candidate = candidatesForCurrentComposition().firstOrNull()
            if (candidate != null) commitCandidate(candidate) else commitCompositionOrRaw()
        } else {
            commitPlainText(" ")
        }
    }

    private fun onPunctuation(value: Char) {
        clearVoiceForManualInput()
        if (!commitCompositionOrRaw()) return
        val text = when {
            activeMode() == ImeInputMode.ENGLISH -> value.toString()
            value == ',' -> "，"
            else -> "。"
        }
        commitPlainText(text)
    }

    private fun onEnter() {
        clearVoiceForManualInput()
        if (!commitCompositionOrRaw()) return
        flushCapturedText()
        if (sendDefaultEditorAction(true)) {
            inputRevision += 1
            lastCommittedChinese = ""
            refreshCandidates()
        } else {
            commitPlainText("\n")
        }
    }

    private fun cycleMode() {
        if (forceEnglishForEditor) return
        clearVoiceForManualInput()
        if (!commitCompositionOrRaw()) return
        selectedMode = selectedMode.next()
        modeStore.save(selectedMode)
        shifted = false
        updateLetterLabels()
        renderMode()
        lastCommittedChinese = ""
        refreshCandidates()
    }

    private fun toggleVoiceInput() {
        when (voiceUiState) {
            VoiceUiState.STARTING, VoiceUiState.LISTENING -> {
                if (!voiceController.stop()) cancelVoiceInput(render = true)
            }
            VoiceUiState.PROCESSING -> {
                cancelVoiceInput(render = true)
                Toast.makeText(this, uiText("已取消语音识别"), Toast.LENGTH_SHORT).show()
            }
            VoiceUiState.RESULTS, VoiceUiState.ERROR -> {
                cancelVoiceInput(render = false)
                startVoiceInput()
            }
            VoiceUiState.IDLE -> startVoiceInput()
        }
    }

    private fun startVoiceInput() {
        if (!voiceAllowed) {
            Toast.makeText(this, uiText("当前输入框禁止使用语音"), Toast.LENGTH_SHORT).show()
            return
        }
        if (!commitCompositionOrRaw()) return
        clearVoicePresentation()
        lastCommittedChinese = ""
        invalidateCandidates(render = false)
        when (val result = voiceController.start(editorGeneration, voiceBiasingStrings())) {
            is VoiceStartResult.Started -> {
                if (voiceUiState == VoiceUiState.IDLE) {
                    voiceUiState = VoiceUiState.STARTING
                    voiceGeneration += 1
                    renderVoiceUi()
                }
            }
            is VoiceStartResult.Rejected -> {
                voiceUiState = VoiceUiState.ERROR
                voiceError = result.message
                voiceGeneration += 1
                renderVoiceUi()
                Toast.makeText(this, uiText(result.message), Toast.LENGTH_SHORT).show()
                if (result.permissionMissing) openMicrophonePermissionSettings()
            }
        }
    }

    private fun onVoiceUpdate(key: VoiceSessionKey, update: VoiceUpdate) {
        if (destroyed || key.editorGeneration != editorGeneration || !voiceAllowed) return
        voiceGeneration += 1
        when (update) {
            is VoiceUpdate.Starting -> {
                voiceUiState = VoiceUiState.STARTING
                voicePreview = "正在启动${update.engineLabel}…"
                voiceError = ""
                voiceCandidates = emptyList()
                voiceRecognitionMetadata = null
            }
            is VoiceUpdate.Listening -> {
                voiceUiState = VoiceUiState.LISTENING
                voicePreview = update.partialText
                voiceError = ""
                voiceCandidates = emptyList()
                voiceRecognitionMetadata = null
            }
            VoiceUpdate.Processing -> {
                voiceUiState = VoiceUiState.PROCESSING
                voiceError = ""
                voiceCandidates = emptyList()
                voiceRecognitionMetadata = null
            }
            is VoiceUpdate.Results -> {
                voiceUiState = VoiceUiState.RESULTS
                voicePreview = ""
                voiceError = ""
                voiceCandidates = update.candidates
                voiceRecognitionMetadata = update.metadata
                recordVoiceRecognitionAudit(update.metadata, update.candidates.size)
            }
            is VoiceUpdate.Error -> {
                voiceUiState = VoiceUiState.ERROR
                voicePreview = ""
                voiceError = update.message
                voiceCandidates = emptyList()
                voiceRecognitionMetadata = null
            }
        }
        renderVoiceUi()
    }

    private fun commitVoiceCandidate(candidate: VoiceCandidate): Boolean {
        if (voiceUiState != VoiceUiState.RESULTS || candidate !in voiceCandidates) return false
        val connection = currentInputConnection ?: return false
        if (!runVoiceCandidateCommit(
                commit = {
                    runSelfSelectionEdit(expectedCommitText(candidate.text)) {
                        connection.commitText(candidate.text, 1)
                    }
                },
                clearResults = ::clearVoicePresentation,
            )
        ) {
            Toast.makeText(this, uiText("语音结果未能写入，请重新说一次"), Toast.LENGTH_SHORT).show()
            return false
        }
        rememberText(candidate.text)
        inputRevision += 1
        lastCommittedChinese = candidate.text
        refreshCandidates()
        return true
    }

    private fun clearVoiceForManualInput() {
        if (voiceUiState != VoiceUiState.IDLE || voiceController.isActive()) {
            cancelVoiceInput(render = true)
        }
    }

    private fun cancelVoiceInput(render: Boolean) {
        if (::voiceController.isInitialized) voiceController.cancel()
        clearVoicePresentation()
        if (render && !destroyed && ::candidateRow.isInitialized) refreshCandidates()
    }

    private fun clearVoicePresentation() {
        voiceUiState = VoiceUiState.IDLE
        voicePreview = ""
        voiceError = ""
        voiceCandidates = emptyList()
        voiceRecognitionMetadata = null
        voiceGeneration += 1
        if (::voiceButton.isInitialized) renderVoiceButton()
    }

    private fun renderVoiceUi() {
        if (::compositionView.isInitialized) renderComposition()
        if (::candidateRow.isInitialized) renderCandidates(currentCandidateSnapshot)
        renderVoiceButton()
    }

    private fun renderVoiceButton() {
        if (!::voiceButton.isInitialized) return
        voiceButton.text = uiText(when (voiceUiState) {
            VoiceUiState.STARTING, VoiceUiState.LISTENING -> "停止"
            VoiceUiState.PROCESSING -> "取消"
            else -> "语音"
        })
        voiceButton.isEnabled = voiceAllowed && !forceEnglishForEditor
        voiceButton.contentDescription = uiText(when (voiceUiState) {
            VoiceUiState.STARTING, VoiceUiState.LISTENING -> "停止普通话录音"
            VoiceUiState.PROCESSING -> "取消普通话识别"
            else -> "普通话语音输入"
        })
    }

    private fun renderVoiceCandidates() {
        candidateRow.removeAllViews()
        if (voiceUiState == VoiceUiState.RESULTS && voiceCandidates.isNotEmpty()) {
            val generation = voiceGeneration
            voiceCandidates.forEachIndexed { index, candidate ->
                candidateRow.addView(
                    candidateButton("${index + 1} ${candidate.text}") {
                        if (voiceUiState == VoiceUiState.RESULTS && generation == voiceGeneration) {
                            commitVoiceCandidate(candidate)
                        }
                    },
                )
            }
            return
        }
        candidateRow.addView(TextView(this).apply {
            text = uiText(when (voiceUiState) {
                VoiceUiState.STARTING -> "正在打开麦克风…"
                VoiceUiState.LISTENING -> "请说普通话；说完后点“停止”"
                VoiceUiState.PROCESSING -> "正在识别普通话…"
                VoiceUiState.ERROR -> voiceError
                else -> ""
            })
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(12), 0, dp(12), 0)
            setTextColor(Color.rgb(85, 93, 105))
            textSize = 14f
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.MATCH_PARENT,
            )
        })
    }

    private fun voiceBiasingStrings(): List<String> = learnedStats.keys.asSequence()
        .map { it.substringAfter('\t', "") }
        .map(String::trim)
        .filter { word -> word.isNotEmpty() && word.length <= 24 && word.any { it.code in 0x3400..0x9fff } }
        .distinct()
        .take(MAX_VOICE_BIASING_STRINGS)
        .toList()

    private fun openMicrophonePermissionSettings() {
        val intent = Intent(
            Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
            Uri.parse("package:$packageName"),
        ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        runCatching { startActivity(intent) }
    }

    private fun recordVoiceRecognitionAudit(metadata: VoiceRecognitionMetadata, candidateCount: Int) {
        val payload = JSONObject()
            .put("engine", metadata.engine.take(80))
            .put("duration_ms", metadata.durationMs)
            .put("processing_location", metadata.processingLocation.take(40))
            .put("raw_audio_left_device", metadata.rawAudioLeftDevice)
            .put("candidate_count", candidateCount)
            .put("audio_persisted", false)
            .put("language", "zh-CN")
        eventWriter.offer(
            LocalEventEntity(
                source = "android.ime.voice",
                type = "ime.voice_recognized",
                sensitivity = "restricted",
                payloadJson = payload.toString(),
            ),
        )
    }

    private fun recordVoiceOutboundAudit(key: VoiceSessionKey, audit: VoiceOutboundAudit) {
        val payload = JSONObject()
            .put("session_id", key.sessionId)
            .put("phase", audit.phase.take(24))
            .put("engine", audit.engine.take(80))
            .put("processing_location", audit.processingLocation.take(40))
            .put("raw_audio_left_device", audit.rawAudioLeftDevice)
            .put("raw_audio_reached_cloud", audit.rawAudioReachedCloud ?: JSONObject.NULL)
            .put("payload_bytes", audit.payloadBytes.coerceAtLeast(0))
            .put("destination_fingerprint", audit.destinationFingerprint ?: JSONObject.NULL)
            .put("reason", audit.reason?.take(80) ?: JSONObject.NULL)
            .put("audio_persisted", false)
            .put("language", "zh-CN")
        eventWriter.offer(
            LocalEventEntity(
                source = "android.ime.voice",
                type = "ime.voice_outbound",
                sensitivity = "restricted",
                payloadJson = payload.toString(),
            ),
        )
    }

    private fun commitCompositionOrRaw(): Boolean {
        if (composing.isEmpty()) return true
        val candidate = candidatesForCurrentComposition().firstOrNull()
        if (candidate != null) return commitCandidate(candidate)
        if (engine == null && !lexiconFailed) {
            renderCandidates(currentCandidateSnapshot)
            Toast.makeText(this, uiText("离线中文词库正在加载，请稍候"), Toast.LENGTH_SHORT).show()
            return false
        }
        val raw = composing.toString()
        val connection = currentInputConnection ?: return false
        clearCompositionBeforeCommit()
        if (!runSelfSelectionEdit(expectedCommitText(raw)) { connection.commitText(raw, 1) }) {
            restoreCompositionAfterFailedCommit(raw)
            return false
        }
        rememberText(raw)
        inputRevision += 1
        lastCommittedChinese = ""
        refreshCandidates()
        return true
    }

    private fun commitCandidate(candidate: ImeCandidate): Boolean {
        val connection = currentInputConnection ?: return false
        val raw = composing.toString()
        clearCompositionBeforeCommit()
        if (!runSelfSelectionEdit(expectedCommitText(candidate.text)) {
                connection.commitText(candidate.text, 1)
            }
        ) {
            restoreCompositionAfterFailedCommit(raw)
            return false
        }
        rememberText(candidate.text)
        if (learningAllowed) learn(candidate)
        inputRevision += 1
        lastCommittedChinese = candidate.text
        refreshCandidates()
        return true
    }

    private fun commitPlainText(text: String): Boolean {
        if (text.isEmpty()) return false
        val connection = currentInputConnection ?: return false
        val committed = runSelfSelectionEdit(expectedCommitText(text)) {
            connection.commitText(text, 1)
        }
        if (committed) {
            rememberText(text)
            inputRevision += 1
            lastCommittedChinese = ""
            refreshCandidates()
        }
        return committed
    }

    private fun learn(candidate: ImeCandidate) {
        val key = candidate.learningKey
        val previous = learnedStats[key]
        learnedStats[key] = ImeLearningStat(
            count = (previous?.count ?: 0).plus(1).coerceAtMost(MAX_LEARN_COUNT),
            lastCommittedAt = System.currentTimeMillis(),
        )
        trimLearnedStats()
        learningMutationEpoch += 1
        learningCommands.trySend(LearningCommand(learnedStats.toMap()))
    }

    private fun refreshCandidates() {
        if (!::candidateRow.isInitialized || !::compositionView.isInitialized) return
        if (voiceUiState != VoiceUiState.IDLE) {
            renderVoiceUi()
            return
        }
        renderComposition()
        val generation = ++lookupGeneration
        val raw = composing.toString()
        val mode = activeMode()
        val previous = lastCommittedChinese
        currentCandidateSnapshot = CandidateSnapshot(
            editorGeneration = editorGeneration,
            inputRevision = inputRevision,
            lookupGeneration = generation,
            raw = raw,
            mode = mode,
            previous = previous,
            candidates = emptyList(),
        )
        renderCandidates(currentCandidateSnapshot)
        val loadedEngine = engine ?: return
        candidateRequests.trySend(
            CandidateRequest(
                editorGeneration = editorGeneration,
                inputRevision = inputRevision,
                lookupGeneration = generation,
                raw = raw,
                mode = mode,
                previous = previous,
                engine = loadedEngine,
                learning = learnedStats.toMap(),
            ),
        )
    }

    /** Explicit selection must never use candidates produced for the previous keystroke. */
    private fun candidatesForCurrentComposition(): List<ImeCandidate> {
        if (composing.isEmpty() || activeMode() == ImeInputMode.ENGLISH) return emptyList()
        return engine?.candidates(
            composing.toString(),
            activeMode(),
            learnedStats,
            MAX_VISIBLE_CANDIDATES,
        ).orEmpty()
    }

    private fun renderCandidates(snapshot: CandidateSnapshot?) {
        if (!::candidateRow.isInitialized) return
        if (voiceUiState != VoiceUiState.IDLE) {
            renderVoiceCandidates()
            return
        }
        candidateRow.removeAllViews()
        val candidates = snapshot?.candidates.orEmpty()
        if (candidates.isEmpty()) {
            candidateRow.addView(TextView(this).apply {
                text = uiText(when {
                    lexiconFailed -> "离线词库加载失败，可切换 EN"
                    engine == null -> "正在加载离线中文词库…"
                    activeMode() == ImeInputMode.ENGLISH -> "英文直接输入"
                    activeMode() == ImeInputMode.NATURAL_CODE -> "输入自然码双拼，空格上屏"
                    else -> "输入全拼，空格上屏"
                })
                gravity = Gravity.CENTER_VERTICAL
                setPadding(dp(12), 0, dp(12), 0)
                setTextColor(Color.rgb(85, 93, 105))
                textSize = 14f
                layoutParams = LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    LinearLayout.LayoutParams.MATCH_PARENT,
                )
            })
            return
        }
        candidates.forEachIndexed { index, candidate ->
            candidateRow.addView(
                candidateButton("${index + 1} ${candidate.text}") {
                    if (snapshot != null && snapshot === currentCandidateSnapshot && snapshot.matchesCurrentState()) {
                        commitCandidate(candidate)
                    }
                },
            )
        }
    }

    private fun renderComposition() {
        if (!::compositionView.isInitialized) return
        compositionView.text = uiText(when {
            voiceUiState == VoiceUiState.STARTING -> voicePreview.ifBlank { "正在启动普通话语音…" }
            voiceUiState == VoiceUiState.LISTENING -> voicePreview.ifBlank { "正在听普通话…" }
            voiceUiState == VoiceUiState.PROCESSING -> "正在识别普通话…"
            voiceUiState == VoiceUiState.RESULTS -> "语音结果：请选择后上屏"
            voiceUiState == VoiceUiState.ERROR -> voiceError
            composing.isNotEmpty() && activeMode() == ImeInputMode.NATURAL_CODE -> "双拼  ${composing}"
            composing.isNotEmpty() -> composing.toString()
            forceEnglishForEditor -> "隐私或专用字段：英文模式"
            activeMode() == ImeInputMode.NATURAL_CODE -> "自然码双拼"
            activeMode() == ImeInputMode.FULL_PINYIN -> "全拼"
            else -> "English"
        })
    }

    private fun renderMode() {
        if (!::modeButton.isInitialized) return
        modeButton.text = uiText(if (forceEnglishForEditor) "EN·隐私" else selectedMode.label)
        modeButton.isEnabled = !forceEnglishForEditor
        renderComposition()
        renderVoiceButton()
    }

    private fun activeMode(): ImeInputMode =
        if (forceEnglishForEditor) ImeInputMode.ENGLISH else selectedMode

    /** Finishes the editor span without deleting the user's not-yet-converted pinyin. */
    private fun finishCompositionPreservingText(): Boolean {
        val raw = composing.toString()
        if (raw.isEmpty()) {
            clearCompositionState()
            return true
        }
        val connection = currentInputConnection ?: return false
        clearCompositionBeforeCommit()
        val preserved = runSelfSelectionEdit(expectedFinishComposition()) {
            connection.finishComposingText()
        } || runSelfSelectionEdit(expectedCommitText(raw)) {
            connection.commitText(raw, 1)
        }
        if (!preserved) {
            restoreCompositionAfterFailedCommit(raw)
            return false
        }
        rememberText(raw)
        inputRevision += 1
        lastCommittedChinese = ""
        refreshCandidates()
        return true
    }

    private fun clearCompositionBeforeCommit() {
        composing.clear()
        invalidateCandidates(render = true)
        if (::compositionView.isInitialized) renderComposition()
    }

    private fun restoreCompositionAfterFailedCommit(raw: String) {
        composing.clear()
        composing.append(raw)
        refreshCandidates()
    }

    private fun clearCompositionState() {
        composing.clear()
        invalidateCandidates(render = true)
        if (::compositionView.isInitialized) renderComposition()
    }

    private fun runSelfSelectionEdit(
        expected: ExpectedSelectionUpdate?,
        action: () -> Boolean,
    ): Boolean {
        val previousState = selectionState
        if (expected != null) {
            purgeExpiredSelfSelectionUpdates()
            expectedSelfSelectionUpdates.addLast(expected)
            selectionState = expected.asSelectionState()
        }
        val succeeded = runCatching(action).getOrDefault(false)
        if (!succeeded && expected != null && expectedSelfSelectionUpdates.remove(expected)) {
            selectionState = previousState
        }
        return succeeded
    }

    private fun consumeSelfSelectionUpdate(
        selectionStart: Int,
        selectionEnd: Int,
        candidatesStart: Int,
        candidatesEnd: Int,
    ): Boolean {
        purgeExpiredSelfSelectionUpdates()
        while (expectedSelfSelectionUpdates.isNotEmpty()) {
            val expected = expectedSelfSelectionUpdates.removeFirst()
            if (expected.matches(selectionStart, selectionEnd, candidatesStart, candidatesEnd)) return true
        }
        return false
    }

    private fun purgeExpiredSelfSelectionUpdates() {
        val now = SystemClock.elapsedRealtime()
        while (expectedSelfSelectionUpdates.peekFirst()?.expiresAt?.let { it < now } == true) {
            expectedSelfSelectionUpdates.removeFirst()
        }
    }

    private fun expectedSetComposingText(text: String): ExpectedSelectionUpdate? {
        val start = selectionState.replacementStart() ?: return null
        val end = start + text.length
        return ExpectedSelectionUpdate(start = end, end = end, candidatesStart = start, candidatesEnd = end)
    }

    private fun expectedCommitText(text: String): ExpectedSelectionUpdate? {
        val start = selectionState.replacementStart() ?: return null
        val cursor = start + text.length
        return ExpectedSelectionUpdate(start = cursor, end = cursor, candidatesStart = -1, candidatesEnd = -1)
    }

    private fun expectedFinishComposition(): ExpectedSelectionUpdate? {
        if (!selectionState.hasSelection()) return null
        return ExpectedSelectionUpdate(
            start = selectionState.selectionStart,
            end = selectionState.selectionEnd,
            candidatesStart = -1,
            candidatesEnd = -1,
        )
    }

    private fun invalidateCandidates(render: Boolean) {
        lookupGeneration += 1
        currentCandidateSnapshot = null
        if (render && ::candidateRow.isInitialized) renderCandidates(null)
    }

    private fun row(heightDp: Int = KEY_HEIGHT_DP): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER
        layoutParams = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            dp(heightDp),
        )
    }

    private fun keyButton(label: String, weight: Float, action: () -> Unit): Button = Button(this).apply {
        text = uiText(label)
        textSize = if (label.length > 2) 11f else 17f
        isAllCaps = false
        minWidth = 0
        minimumWidth = 0
        setPadding(dp(2), 0, dp(2), 0)
        layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.MATCH_PARENT, weight).apply {
            setMargins(dp(2), dp(2), dp(2), dp(2))
        }
        setOnClickListener { action() }
    }

    private fun candidateButton(label: String, action: () -> Unit): Button = Button(this).apply {
        text = label
        textSize = 17f
        isAllCaps = false
        minWidth = 0
        minimumWidth = 0
        setPadding(dp(10), 0, dp(10), 0)
        layoutParams = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT,
            LinearLayout.LayoutParams.MATCH_PARENT,
        ).apply { setMargins(dp(2), dp(2), dp(2), dp(2)) }
        setOnClickListener { action() }
    }

    private fun toggleShift() {
        shifted = !shifted
        updateLetterLabels()
    }

    private fun updateLetterLabels() {
        letterButtons.forEach { button ->
            button.text = if (shifted) {
                button.text.toString().uppercase()
            } else {
                button.text.toString().lowercase()
            }
        }
    }

    private fun rememberText(value: String) {
        if (!captureAllowed || value.isEmpty()) return
        var remaining = value
        while (remaining.isNotEmpty()) {
            var tail = pendingCaptures.peekLast()
            if (tail == null || tail.sourcePackage != sourcePackage || tail.text.length >= MAX_EVENT_CHARS) {
                if (pendingCaptures.size >= MAX_PENDING_CAPTURE_CHUNKS) flushCapturedText()
                if (pendingCaptures.size >= MAX_PENDING_CAPTURE_CHUNKS) return
                tail = PendingCapture(sourcePackage, StringBuilder())
                pendingCaptures.addLast(tail)
            }
            val available = MAX_EVENT_CHARS - tail.text.length
            val part = remaining.take(available)
            tail.text.append(part)
            remaining = remaining.drop(part.length)
        }
        if (pendingCaptures.peekFirst()?.text?.length == MAX_EVENT_CHARS) flushCapturedText()
    }

    private fun flushCapturedText() {
        while (pendingCaptures.isNotEmpty()) {
            val pending = pendingCaptures.peekFirst() ?: break
            val text = pending.text.toString()
            if (text.isBlank()) {
                pendingCaptures.removeFirst()
                continue
            }
            val payload = JSONObject()
                .put("package", pending.sourcePackage)
                .put("text", text)
                .put("password_excluded", true)
            val accepted = eventWriter.offer(
                LocalEventEntity(
                    source = "android.ime",
                    type = "ime.text_committed",
                    sensitivity = "restricted",
                    payloadJson = payload.toString(),
                ),
            )
            if (!accepted) return
            pendingCaptures.removeFirst()
        }
    }

    private fun trimLearnedStats() {
        while (learnedStats.size > MAX_LEARNED_ENTRIES) {
            val oldest = learnedStats.minByOrNull { it.value.lastCommittedAt }?.key ?: break
            learnedStats.remove(oldest)
        }
    }

    private fun isCredentialEditor(info: EditorInfo): Boolean {
        if (info.inputType == InputType.TYPE_NULL) return true
        val inputClass = info.inputType and InputType.TYPE_MASK_CLASS
        val variation = info.inputType and InputType.TYPE_MASK_VARIATION
        return when (inputClass) {
            InputType.TYPE_CLASS_TEXT -> variation == InputType.TYPE_TEXT_VARIATION_PASSWORD ||
                variation == InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD ||
                variation == InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD
            InputType.TYPE_CLASS_NUMBER -> variation == InputType.TYPE_NUMBER_VARIATION_PASSWORD
            else -> false
        }
    }

    private fun shouldForceAscii(info: EditorInfo): Boolean {
        if (info.imeOptions and EditorInfo.IME_FLAG_FORCE_ASCII != 0) return true
        val inputClass = info.inputType and InputType.TYPE_MASK_CLASS
        if (inputClass != InputType.TYPE_CLASS_TEXT) return true
        val variation = info.inputType and InputType.TYPE_MASK_VARIATION
        return variation == InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS ||
            variation == InputType.TYPE_TEXT_VARIATION_WEB_EMAIL_ADDRESS ||
            variation == InputType.TYPE_TEXT_VARIATION_URI
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    override fun onDestroy() {
        cancelVoiceInput(render = false)
        if (!finishCompositionPreservingText()) clearCompositionState()
        flushCapturedText()
        destroyed = true
        candidateRequests.close()
        val finalLearningStats = learnedStats.toMap()
        val finalLearningSave = CompletableDeferred<Boolean>()
        val finalSaveAccepted = learningCommands.trySend(
            LearningCommand(finalLearningStats, finalLearningSave),
        ).isSuccess
        learningCommands.close()
        val drained = runBlocking {
            withTimeoutOrNull(SHUTDOWN_DRAIN_TIMEOUT_MS) {
                val saved = finalSaveAccepted && finalLearningSave.await()
                if (!saved) {
                    val retried = withContext(Dispatchers.IO) {
                        learningStore.save(finalLearningStats)
                    }
                    if (!retried) Log.w(TAG, "Unable to persist IME learning state during shutdown")
                }
                if (::learningSaverJob.isInitialized) learningSaverJob.join()
                if (::eventWriter.isInitialized) eventWriter.closeAndDrain()
            }
        } != null
        if (!drained) Log.w(TAG, "Timed out draining IME state during shutdown")
        lifecycleJob.cancel()
        super.onDestroy()
    }

    private fun CandidateRequest.evaluate(): List<ImeCandidate> = when {
        raw.isNotEmpty() && mode != ImeInputMode.ENGLISH ->
            engine.candidates(raw, mode, learning, MAX_VISIBLE_CANDIDATES)
        raw.isEmpty() && mode != ImeInputMode.ENGLISH && previous.isNotEmpty() ->
            engine.associations(previous, learning, MAX_VISIBLE_CANDIDATES)
        else -> emptyList()
    }

    private fun CandidateRequest.matchesCurrentState(): Boolean =
        editorGeneration == this@MouchenImeService.editorGeneration &&
            inputRevision == this@MouchenImeService.inputRevision &&
            lookupGeneration == this@MouchenImeService.lookupGeneration &&
            raw == composing.toString() &&
            mode == activeMode() &&
            previous == lastCommittedChinese

    private fun CandidateSnapshot.matchesCurrentState(): Boolean =
        editorGeneration == this@MouchenImeService.editorGeneration &&
            inputRevision == this@MouchenImeService.inputRevision &&
            lookupGeneration == this@MouchenImeService.lookupGeneration &&
            raw == composing.toString() &&
            mode == activeMode() &&
            previous == lastCommittedChinese

    private data class CandidateRequest(
        val editorGeneration: Long,
        val inputRevision: Long,
        val lookupGeneration: Long,
        val raw: String,
        val mode: ImeInputMode,
        val previous: String,
        val engine: PinyinEngine,
        val learning: Map<String, ImeLearningStat>,
    )

    private data class CandidateSnapshot(
        val editorGeneration: Long,
        val inputRevision: Long,
        val lookupGeneration: Long,
        val raw: String,
        val mode: ImeInputMode,
        val previous: String,
        val candidates: List<ImeCandidate>,
    )

    private data class PendingCapture(
        val sourcePackage: String,
        val text: StringBuilder,
    )

    private data class LearningCommand(
        val stats: Map<String, ImeLearningStat>,
        val completion: CompletableDeferred<Boolean>? = null,
    )

    private enum class VoiceUiState {
        IDLE,
        STARTING,
        LISTENING,
        PROCESSING,
        RESULTS,
        ERROR,
    }

    private data class SelectionState(
        val selectionStart: Int,
        val selectionEnd: Int,
        val candidatesStart: Int,
        val candidatesEnd: Int,
    ) {
        fun hasSelection(): Boolean = selectionStart >= 0 && selectionEnd >= 0

        fun replacementStart(): Int? = when {
            candidatesStart >= 0 && candidatesEnd >= candidatesStart -> candidatesStart
            hasSelection() -> minOf(selectionStart, selectionEnd)
            else -> null
        }

        companion object {
            val UNKNOWN = SelectionState(-1, -1, -1, -1)
        }
    }

    private data class ExpectedSelectionUpdate(
        val start: Int,
        val end: Int,
        val candidatesStart: Int,
        val candidatesEnd: Int,
        val expiresAt: Long = SystemClock.elapsedRealtime() + SELF_SELECTION_TIMEOUT_MS,
    ) {
        fun matches(start: Int, end: Int, candidatesStart: Int, candidatesEnd: Int): Boolean =
            this.start == start &&
                this.end == end &&
                this.candidatesStart == candidatesStart &&
                this.candidatesEnd == candidatesEnd

        fun asSelectionState(): SelectionState = SelectionState(
            selectionStart = start,
            selectionEnd = end,
            candidatesStart = candidatesStart,
            candidatesEnd = candidatesEnd,
        )
    }

    private companion object {
        const val LEXICON_ASSET = "ime/pinyin_lexicon_v1.tsv"
        const val MAX_EVENT_CHARS = 500
        const val MAX_COMPOSING_CHARS = 64
        const val MAX_VISIBLE_CANDIDATES = 9
        const val MAX_LEARN_COUNT = 1_000
        const val MAX_LEARNED_ENTRIES = 500
        const val MAX_PENDING_CAPTURE_CHUNKS = 8
        const val MAX_VOICE_BIASING_STRINGS = 20
        const val EVENT_QUEUE_CAPACITY = 32
        const val KEY_HEIGHT_DP = 52
        const val HEADER_HEIGHT_DP = 46
        const val CANDIDATE_HEIGHT_DP = 48
        const val SELF_SELECTION_TIMEOUT_MS = 1_000L
        const val LEARNING_SAVE_TIMEOUT_MS = 2_000L
        const val SHUTDOWN_DRAIN_TIMEOUT_MS = 2_000L
        const val TAG = "MouchenIme"
    }
}
