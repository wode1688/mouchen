package com.mouchen.app.models

import android.content.Context
import android.util.Base64
import com.mouchen.app.security.KeyVault

/** Stores small user-entered configuration blobs encrypted by Android Keystore. */
class SecureSettingsStore(context: Context, namespace: String) {
    private val preferences = context.applicationContext.getSharedPreferences(namespace, Context.MODE_PRIVATE)
    private val keyVault = KeyVault(context.applicationContext)

    fun put(key: String, value: String) {
        val encrypted = keyVault.encrypt(value.toByteArray(Charsets.UTF_8))
        preferences.edit().putString(key, Base64.encodeToString(encrypted, Base64.NO_WRAP)).apply()
    }

    fun putDurably(key: String, value: String): Boolean {
        val encrypted = keyVault.encrypt(value.toByteArray(Charsets.UTF_8))
        return preferences.edit()
            .putString(key, Base64.encodeToString(encrypted, Base64.NO_WRAP))
            .commit()
    }

    fun get(key: String): String? {
        val encoded = preferences.getString(key, null) ?: return null
        return runCatching {
            val encrypted = Base64.decode(encoded, Base64.NO_WRAP)
            keyVault.decrypt(encrypted).toString(Charsets.UTF_8)
        }.getOrNull()
    }

    fun remove(key: String) {
        preferences.edit().remove(key).apply()
    }

    fun removeDurably(key: String): Boolean = preferences.edit().remove(key).commit()
}
