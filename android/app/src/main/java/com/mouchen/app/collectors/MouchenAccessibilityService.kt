package com.mouchen.app.collectors

import android.accessibilityservice.AccessibilityService
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import com.mouchen.app.BuildConfig
import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.sync.effectiveCollectionConsent
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicLong

class MouchenAccessibilityService : AccessibilityService() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val lastCapture = ConcurrentHashMap<String, Long>()
    private val lastContent = ConcurrentHashMap<String, Pair<Int, Long>>()
    private val lastGlobalCapture = AtomicLong(0L)
    private lateinit var eventWriter: BoundedEventWriter
    private lateinit var privacyGuard: ScreenCapturePrivacyGuard

    override fun onCreate() {
        super.onCreate()
        eventWriter = BoundedEventWriter(applicationContext, scope, EVENT_QUEUE_CAPACITY)
        privacyGuard = ScreenCapturePrivacyGuard(applicationContext)
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        // A newly connected service must observe the current foreground again. Reusing a package
        // signal from a previous service process would weaken the screen-capture gate.
        privacyGuard.clearForegroundPackageSignal()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (!BuildConfig.ALLOW_EXPERIMENTAL_SERVICES || event == null) return
        if (!effectiveCollectionConsent(applicationContext)) return
        val sourcePackage = event.packageName?.toString() ?: return
        val now = System.currentTimeMillis()
        val root = rootInActiveWindow
        val foregroundPackage = root?.packageName?.toString()?.takeIf { it.isNotBlank() } ?: sourcePackage
        // Every Accessibility event refreshes the package signal before any collection throttle.
        // Sensitive packages are recorded too, so MediaProjection can fail closed immediately.
        privacyGuard.markForegroundPackageSeen(foregroundPackage, now)
        if (
            foregroundPackage == packageName ||
            foregroundPackage.startsWith("com.android.systemui") ||
            isSensitiveCapturePackage(foregroundPackage, packageName)
        ) return
        if (event.isPassword || containsVisiblePasswordField(root)) {
            privacyGuard.markPasswordFieldSeen(now)
            return
        }
        if (now - (lastCapture[foregroundPackage] ?: 0) < 2_000) return
        if (!claimGlobalCaptureSlot(now)) return
        lastCapture[foregroundPackage] = now

        val text = JSONArray()
        val counter = Counter()
        collectVisibleText(root, text, counter)
        if (counter.passwordFieldSeen) {
            privacyGuard.markPasswordFieldSeen(now)
            return
        }
        if (text.length() == 0) return
        val fingerprint = text.toString().hashCode()
        val previousContent = lastContent[foregroundPackage]
        if (previousContent?.first == fingerprint && now - previousContent.second < REPEAT_WINDOW_MS) return
        lastContent[foregroundPackage] = fingerprint to now
        val payload = JSONObject()
            .put("package", foregroundPackage)
            .put("trigger_package", sourcePackage)
            .put("event_type", event.eventType)
            .put("visible_text", text)
        eventWriter.offer(
            LocalEventEntity(
                source = "android.accessibility",
                type = "ui.visible_text",
                sensitivity = "restricted",
                payloadJson = payload.toString(),
            ),
        )
    }

    private fun claimGlobalCaptureSlot(now: Long): Boolean {
        while (true) {
            val previous = lastGlobalCapture.get()
            if (now - previous < GLOBAL_CAPTURE_INTERVAL_MS) return false
            if (lastGlobalCapture.compareAndSet(previous, now)) return true
        }
    }

    private fun collectVisibleText(node: AccessibilityNodeInfo?, output: JSONArray, counter: Counter) {
        if (node == null || counter.value >= MAX_NODES) return
        counter.value++
        if (node.isPassword && node.isVisibleToUser) {
            counter.passwordFieldSeen = true
        } else if (node.isVisibleToUser) {
            node.text?.toString()?.trim()?.takeIf { it.isNotEmpty() }?.let { output.put(it.take(1000)) }
            node.contentDescription?.toString()?.trim()?.takeIf { it.isNotEmpty() }?.let { output.put(it.take(500)) }
        }
        for (index in 0 until node.childCount) collectVisibleText(node.getChild(index), output, counter)
    }

    private fun containsVisiblePasswordField(node: AccessibilityNodeInfo?): Boolean {
        if (node == null) return false
        val pending = ArrayDeque<AccessibilityNodeInfo>()
        pending.add(node)
        var inspected = 0
        while (pending.isNotEmpty() && inspected < MAX_NODES) {
            val current = pending.removeFirst()
            inspected++
            if (current.isVisibleToUser && current.isPassword) return true
            for (index in 0 until current.childCount) {
                current.getChild(index)?.let(pending::addLast)
            }
        }
        return false
    }

    override fun onInterrupt() {
        if (::privacyGuard.isInitialized) privacyGuard.clearForegroundPackageSignal()
    }

    override fun onDestroy() {
        if (::privacyGuard.isInitialized) privacyGuard.clearForegroundPackageSignal()
        if (::eventWriter.isInitialized) eventWriter.close()
        scope.cancel()
        super.onDestroy()
    }

    private class Counter(
        var value: Int = 0,
        var passwordFieldSeen: Boolean = false,
    )

    private companion object {
        const val MAX_NODES = 500
        const val EVENT_QUEUE_CAPACITY = 16
        const val GLOBAL_CAPTURE_INTERVAL_MS = 500L
        const val REPEAT_WINDOW_MS = 10L * 60 * 1000
    }
}
