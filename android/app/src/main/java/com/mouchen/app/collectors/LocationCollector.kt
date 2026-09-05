package com.mouchen.app.collectors

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.os.CancellationSignal
import android.os.Looper
import androidx.core.content.ContextCompat
import com.mouchen.app.data.LocalEventEntity
import com.mouchen.app.data.MouchenDao
import java.security.MessageDigest
import kotlin.coroutines.resume
import kotlin.math.ceil
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.round
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONObject

/** Collects one bounded, coarse location snapshot during an existing collection scan. */
class LocationCollector(
    private val context: Context,
    private val dao: MouchenDao,
) {
    suspend fun collect(): Int {
        if (!hasLocationPermission(context)) return 0
        val manager = context.getSystemService(LocationManager::class.java) ?: return 0
        val now = System.currentTimeMillis()
        val cached = loadCachedCandidates(manager)
        val recent = selectLocationCandidate(cached, now, FRESH_CACHE_MAX_AGE_MS)
        val current = if (recent == null) requestLowPowerLocation(manager) else null
        val selected = current?.toCandidate()
            ?.takeIf { isUsableLocationCandidate(it, now, MAX_LOCATION_AGE_MS) }
            ?: recent
            ?: selectLocationCandidate(cached, now, MAX_LOCATION_AGE_MS)
            ?: return 0
        val minimized = minimizeLocation(selected, now)
        val inserted = dao.insertEventIfAbsent(
            LocalEventEntity(
                id = locationEventId(minimized, selected.timestampMs),
                source = "android.location",
                type = "location.snapshot",
                occurredAt = selected.timestampMs,
                sensitivity = "sensitive",
                payloadJson = JSONObject()
                    .put("grid_latitude", minimized.gridLatitude)
                    .put("grid_longitude", minimized.gridLongitude)
                    .put("grid_size_degrees", minimized.gridSizeDegrees)
                    .put("grid_longitude_size_degrees", minimized.gridLongitudeSizeDegrees)
                    .put("precision_m", minimized.precisionMeters)
                    .put("age_ms", minimized.ageMs)
                    .toString(),
            ),
        )
        return if (inserted == -1L) 0 else 1
    }

    @SuppressLint("MissingPermission")
    private fun loadCachedCandidates(manager: LocationManager): List<LocationCandidate> {
        if (!hasLocationPermission(context)) return emptyList()
        return runCatching { manager.getProviders(false) }
            .getOrDefault(emptyList())
            .mapNotNull { provider ->
                runCatching { manager.getLastKnownLocation(provider)?.toCandidate() }.getOrNull()
            }
    }

    @SuppressLint("MissingPermission")
    private suspend fun requestLowPowerLocation(manager: LocationManager): Location? {
        if (!hasLocationPermission(context)) return null
        val provider = lowPowerProvider(manager) ?: return null
        return withTimeoutOrNull(LOCATION_TIMEOUT_MS) {
            suspendCancellableCoroutine { continuation ->
                try {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                        val cancellationSignal = CancellationSignal()
                        continuation.invokeOnCancellation { cancellationSignal.cancel() }
                        manager.getCurrentLocation(
                            provider,
                            cancellationSignal,
                            ContextCompat.getMainExecutor(context),
                        ) { location ->
                            if (continuation.isActive) continuation.resume(location)
                        }
                    } else {
                        val listener = object : LocationListener {
                            override fun onLocationChanged(location: Location) {
                                manager.removeUpdates(this)
                                if (continuation.isActive) continuation.resume(location)
                            }

                            @Deprecated("Legacy LocationListener callback")
                            override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) = Unit

                            override fun onProviderEnabled(provider: String) = Unit

                            override fun onProviderDisabled(provider: String) {
                                manager.removeUpdates(this)
                                if (continuation.isActive) continuation.resume(null)
                            }
                        }
                        continuation.invokeOnCancellation { runCatching { manager.removeUpdates(listener) } }
                        @Suppress("DEPRECATION")
                        manager.requestSingleUpdate(provider, listener, Looper.getMainLooper())
                    }
                } catch (_: SecurityException) {
                    if (continuation.isActive) continuation.resume(null)
                } catch (_: IllegalArgumentException) {
                    if (continuation.isActive) continuation.resume(null)
                } catch (_: IllegalStateException) {
                    if (continuation.isActive) continuation.resume(null)
                }
            }
        }
    }

    private fun lowPowerProvider(manager: LocationManager): String? =
        listOf(LocationManager.NETWORK_PROVIDER, LocationManager.PASSIVE_PROVIDER)
            .firstOrNull { provider -> runCatching { manager.isProviderEnabled(provider) }.getOrDefault(false) }

    private fun Location.toCandidate(): LocationCandidate = LocationCandidate(
        latitude = latitude,
        longitude = longitude,
        accuracyMeters = accuracy.toDouble().takeIf { hasAccuracy() && it.isFinite() && it >= 0.0 },
        timestampMs = time,
    )
}

internal data class LocationCandidate(
    val latitude: Double,
    val longitude: Double,
    val accuracyMeters: Double?,
    val timestampMs: Long,
)

internal data class MinimizedLocation(
    val gridLatitude: Double,
    val gridLongitude: Double,
    val gridSizeDegrees: Double,
    val precisionMeters: Int,
    val ageMs: Long,
    val gridLongitudeSizeDegrees: Double = gridSizeDegrees,
)

internal fun selectLocationCandidate(
    candidates: List<LocationCandidate>,
    nowMs: Long,
    maxAgeMs: Long,
): LocationCandidate? = candidates
    .asSequence()
    .filter { isUsableLocationCandidate(it, nowMs, maxAgeMs) }
    .sortedWith(
        compareByDescending<LocationCandidate> { it.timestampMs }
            .thenBy { it.accuracyMeters ?: Double.MAX_VALUE },
    )
    .firstOrNull()

internal fun isUsableLocationCandidate(candidate: LocationCandidate, nowMs: Long, maxAgeMs: Long): Boolean {
    if (!candidate.latitude.isFinite() || candidate.latitude !in -90.0..90.0) return false
    if (!candidate.longitude.isFinite() || candidate.longitude !in -180.0..180.0) return false
    if (candidate.timestampMs <= 0L || candidate.timestampMs > nowMs + MAX_CLOCK_SKEW_MS) return false
    return nowMs - candidate.timestampMs <= maxAgeMs
}

internal fun minimizeLocation(candidate: LocationCandidate, nowMs: Long): MinimizedLocation {
    val accuracy = candidate.accuracyMeters?.takeIf { it.isFinite() && it >= 0.0 } ?: 0.0
    val gridLatitude = quantizeCoordinate(
        candidate.latitude,
        -90.0,
        90.0,
        LOCATION_LATITUDE_GRID_DEGREES,
    )
    // Derive longitude width only from the already-coarsened latitude. Exporting a width based on
    // the exact latitude would itself become a precision side channel.
    val longitudeGridSize = longitudeGridSizeDegrees(gridLatitude)
    return MinimizedLocation(
        gridLatitude = gridLatitude,
        gridLongitude = quantizeCoordinate(
            candidate.longitude,
            -180.0,
            180.0,
            longitudeGridSize,
        ),
        gridSizeDegrees = LOCATION_LATITUDE_GRID_DEGREES,
        precisionMeters = (ceil(max(accuracy, MIN_GRID_PRECISION_METERS) / 100.0) * 100.0).toInt(),
        ageMs = (nowMs - candidate.timestampMs).coerceAtLeast(0L),
        gridLongitudeSizeDegrees = longitudeGridSize,
    )
}

/** Longitude degrees shrink toward the poles, so use a latitude-aware cell width. */
internal fun longitudeGridSizeDegrees(latitude: Double): Double {
    val polewardEdge = (abs(latitude) + LOCATION_LATITUDE_GRID_DEGREES / 2.0)
        .coerceAtMost(MAX_LONGITUDE_GRID_LATITUDE)
    val metersPerDegree = METERS_PER_LATITUDE_DEGREE * cos(Math.toRadians(polewardEdge))
    if (metersPerDegree <= 0.0) return 360.0
    val requiredDegrees = (MIN_GRID_PRECISION_METERS / metersPerDegree)
        .coerceIn(LOCATION_LATITUDE_GRID_DEGREES, 360.0)
    // Equal-width cells must divide the full longitude span. A rounded arbitrary step leaves a
    // narrow remainder next to +180 degrees and silently reveals a finer location there.
    val cellCount = floor(360.0 / requiredDegrees).toLong().coerceAtLeast(1L)
    return 360.0 / cellCount.toDouble()
}

internal fun locationEventId(location: MinimizedLocation, timestampMs: Long): String {
    val value = "${location.gridLatitude}|${location.gridLongitude}|${location.gridSizeDegrees}|" +
        "${location.gridLongitudeSizeDegrees}|$timestampMs"
    val digest = MessageDigest.getInstance("SHA-256")
        .digest(value.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }
    return "location-$digest"
}

private fun quantizeCoordinate(value: Double, minimum: Double, maximum: Double, gridDegrees: Double): Double {
    val cellCount = round((maximum - minimum) / gridDegrees).toLong().coerceAtLeast(1L)
    val cell = floor((value - minimum) / gridDegrees).toLong().coerceIn(0L, cellCount - 1L)
    val center = minimum + cell * gridDegrees + gridDegrees / 2.0
    return round(center.coerceIn(minimum, maximum) * 10_000.0) / 10_000.0
}

private fun hasLocationPermission(context: Context): Boolean =
    ContextCompat.checkSelfPermission(
        context,
        Manifest.permission.ACCESS_COARSE_LOCATION,
    ) == PackageManager.PERMISSION_GRANTED

private const val FRESH_CACHE_MAX_AGE_MS = 30L * 60 * 1000
private const val MAX_LOCATION_AGE_MS = 6L * 60 * 60 * 1000
private const val MAX_CLOCK_SKEW_MS = 5L * 60 * 1000
private const val LOCATION_TIMEOUT_MS = 7_000L
private const val LOCATION_LATITUDE_GRID_DEGREES = 0.05
private const val MIN_GRID_PRECISION_METERS = 5_500.0
private const val METERS_PER_LATITUDE_DEGREE = 111_320.0
private const val MAX_LONGITUDE_GRID_LATITUDE = 89.999
