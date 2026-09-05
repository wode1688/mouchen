package com.mouchen.app

import android.content.Intent
import android.provider.Settings
import com.mouchen.app.sync.AudioProcessingLocation
import com.mouchen.app.sync.BackendConnection

internal fun ownerContextDisclosure(connection: BackendConnection): String = when {
    !connection.enabled ->
        "全文先加密保存在手机；后端连接已关闭，当前不会上云。"
    connection.proactiveCloudEnabled && !connection.minimizedContextOnly ->
        "全文先加密保存在手机；同步时允许发送完整上下文，但密码、验证码和支付密钥仍会剔除。"
    else ->
        "全文先加密保存在手机；同步时只发送脱敏的最小片段。"
}

internal fun audioTranscriptionReady(connection: BackendConnection): Boolean =
    connection.enabled &&
        !connection.bearerToken.isNullOrBlank() &&
        !connection.sttBaseUrl.isNullOrBlank() &&
        connection.sttProcessingLocation != AudioProcessingLocation.ON_DEVICE

internal fun audioTranscriptionMode(connection: BackendConnection): String =
    if (audioTranscriptionReady(connection)) connection.sttProcessingLocation.wireValue else "disabled"

internal fun audioCaptureDisclosure(connection: BackendConnection): String {
    val prefix = "录音在手机加密分段；"
    return when {
        !connection.enabled -> prefix + "后端连接已关闭，原始录音不会上传，也不会生成转写。"
        connection.sttBaseUrl.isNullOrBlank() -> prefix + "未配置转写地址，原始录音不会上传，也不会生成转写。"
        connection.bearerToken.isNullOrBlank() -> prefix + "尚未配置访问 Token，原始录音不会上传，也不会生成转写。"
        connection.sttProcessingLocation == AudioProcessingLocation.TRUSTED_LAN ->
            prefix + "原始录音会通过 HTTPS 发送到你配置的电脑或可信局域网节点转写。"
        connection.sttProcessingLocation == AudioProcessingLocation.PRIVATE_VPS ->
            prefix + "原始录音会通过 HTTPS 发送到你配置的私人服务器转写。"
        connection.sttProcessingLocation == AudioProcessingLocation.PUBLIC_CLOUD ->
            prefix + "原始录音会通过 HTTPS 发送到你配置的公共云语音服务转写。"
        else -> prefix + "当前版本不支持手机本地转写，原始录音不会上传。"
    }
}

internal fun audioCaptureNotificationText(connection: BackendConnection): String =
    audioCaptureDisclosure(connection) + " 点按“停止”可结束。"

internal fun batteryOptimizationDescription(ignored: Boolean): String = if (ignored) {
    "系统已允许忽略电池优化，但 Android 仍可能终止进程；这不是常驻保证。"
} else {
    "系统省电可能延迟后台采集和同步。AI替身不会自动修改设置，请按需手动选择。"
}

internal fun batteryOptimizationSettingsIntent(): Intent =
    Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
