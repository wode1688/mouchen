package com.mouchen.app.sync

import java.util.Locale
import org.json.JSONArray
import org.json.JSONObject

internal data class CredentialSanitization(
    val payload: JSONObject,
    val redacted: Boolean,
)

/**
 * Last outbound guard for credentials and payment authentication data.
 *
 * This guard runs before both minimized and owner-approved full-context serialization. Full
 * context means complete personal context, never permission to export credentials. Collectors
 * should still avoid persisting these values; this recursive pass protects imported and malformed
 * events as well.
 */
internal object OutboundCredentialSanitizer {
    const val MARKER_KEY = "sensitive_content_excluded"
    const val MARKER = "[sensitive_content_excluded]"

    fun sanitize(source: JSONObject): CredentialSanitization {
        val state = SanitizationState()
        val payload = sanitizeObject(source, state)
        if (state.redacted) payload.put(MARKER_KEY, true)
        return CredentialSanitization(payload = payload, redacted = state.redacted)
    }

    internal fun sanitizeText(value: String): String = sanitizeText(value, SanitizationState())

    private fun sanitizeObject(source: JSONObject, state: SanitizationState): JSONObject =
        JSONObject().apply {
            val keys = source.keys()
            while (keys.hasNext()) {
                val key = keys.next()
                val value = source.opt(key)
                if (isSensitiveKey(key, value)) {
                    state.redacted = true
                    continue
                }
                put(key, sanitizeValue(value, state))
            }
        }

    private fun sanitizeValue(value: Any?, state: SanitizationState): Any = when (value) {
        null, JSONObject.NULL -> JSONObject.NULL
        is JSONObject -> sanitizeObject(value, state)
        is JSONArray -> JSONArray().apply {
            for (index in 0 until value.length()) put(sanitizeValue(value.opt(index), state))
        }
        is String -> sanitizeText(value, state)
        is Number, is Boolean -> value
        else -> sanitizeText(value.toString(), state)
    }

    private fun isSensitiveKey(key: String, value: Any?): Boolean {
        val normalized = key.lowercase(Locale.ROOT).replace(NON_ALPHANUMERIC, "")
        if (value is Boolean && normalized in SAFE_BOOLEAN_ATTESTATIONS) return false
        if (normalized in SENSITIVE_KEYS) return true
        if (SENSITIVE_KEY_SUFFIXES.any(normalized::endsWith)) return true
        return SENSITIVE_CHINESE_KEY_PARTS.any(key::contains)
    }

    private fun sanitizeText(value: String, state: SanitizationState): String {
        var result = value
        result = replace(result, PRIVATE_KEY_BLOCK, state) { MARKER }
        result = replace(result, HTTP_BASIC_AUTH, state) { match ->
            "${match.groupValues[1]}$MARKER${match.groupValues[2]}"
        }
        result = replace(result, AUTHORIZATION_SCHEME, state) { match ->
            "${match.groupValues[1]} $MARKER"
        }
        result = replace(result, JWT, state) { MARKER }
        PROVIDER_TOKENS.forEach { pattern ->
            result = replace(result, pattern, state) { MARKER }
        }
        result = replace(result, LABELED_SECRET, state) { match ->
            "${match.groupValues[1]}=$MARKER"
        }
        result = replace(result, LABELED_SHORT_CODE, state) { match ->
            "${match.groupValues[1]}$MARKER"
        }
        result = replace(result, STANDALONE_SHORT_CODE, state) { MARKER }
        result = CARD_CANDIDATE.replace(result) { match ->
            val digits = match.value.filter(Char::isDigit)
            if (digits.length in 13..19 && passesLuhn(digits)) {
                state.redacted = true
                MARKER
            } else {
                match.value
            }
        }
        return result
    }

    private fun replace(
        value: String,
        pattern: Regex,
        state: SanitizationState,
        replacement: (MatchResult) -> String,
    ): String = pattern.replace(value) { match ->
        state.redacted = true
        replacement(match)
    }

    private fun passesLuhn(digits: String): Boolean {
        var sum = 0
        var doubleDigit = false
        for (index in digits.indices.reversed()) {
            var digit = digits[index].digitToInt()
            if (doubleDigit) {
                digit *= 2
                if (digit > 9) digit -= 9
            }
            sum += digit
            doubleDigit = !doubleDigit
        }
        return sum > 0 && sum % 10 == 0
    }

    private data class SanitizationState(var redacted: Boolean = false)

    private val NON_ALPHANUMERIC = Regex("[^a-z0-9\\p{IsHan}]")
    private val SAFE_BOOLEAN_ATTESTATIONS = setOf(
        "passwordexcluded",
        "sensitivecontentexcluded",
    )
    private val SENSITIVE_KEYS = setOf(
        "password",
        "passwd",
        "pwd",
        "passcode",
        "pin",
        "pincode",
        "otp",
        "totp",
        "hotp",
        "onetimepassword",
        "verificationcode",
        "smscode",
        "mfacode",
        "apikey",
        "apisecret",
        "secret",
        "clientsecret",
        "accesskey",
        "accesskeyid",
        "secretaccesskey",
        "credential",
        "credentials",
        "auth",
        "access",
        "refresh",
        "token",
        "accesstoken",
        "authtoken",
        "authenticationtoken",
        "authorization",
        "refreshtoken",
        "idtoken",
        "bearertoken",
        "sessiontoken",
        "privatekey",
        "signingkey",
        "cardnumber",
        "creditcardnumber",
        "debitcardnumber",
        "bankcardnumber",
        "pan",
        "cvv",
        "cvc",
        "cardsecuritycode",
        "paymentpassword",
        "paymentcode",
    )
    private val SENSITIVE_KEY_SUFFIXES = setOf(
        "password",
        "passcode",
        "pincode",
        "pin",
        "otp",
        "totp",
        "hotp",
        "apikey",
        "apikeyid",
        "apisecret",
        "secret",
        "clientsecret",
        "accesskey",
        "secretaccesskey",
        "credential",
        "credentials",
        "token",
        "accesstoken",
        "authtoken",
        "refreshtoken",
        "sessiontoken",
        "privatekey",
        "signingkey",
        "cardnumber",
        "bankcard",
        "cvv",
        "cvc",
    )
    private val SENSITIVE_CHINESE_KEY_PARTS = setOf(
        "密码",
        "口令",
        "验证码",
        "动态码",
        "短信码",
        "支付码",
        "银行卡号",
        "信用卡号",
        "安全码",
        "私钥",
        "密钥",
        "令牌",
    )

    private val PRIVATE_KEY_BLOCK = Regex(
        "-----BEGIN [^-\\r\\n]*PRIVATE KEY-----.*?-----END [^-\\r\\n]*PRIVATE KEY-----",
        setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
    )
    private val HTTP_BASIC_AUTH = Regex("(?i)(https?://[^\\s/:@]+:)[^\\s@/]+(@)")
    private val AUTHORIZATION_SCHEME = Regex(
        "(?i)\\b(Bearer|Basic)\\s+[A-Za-z0-9._~+/=-]{8,}",
    )
    private val JWT = Regex(
        "\\beyJ[A-Za-z0-9_-]{5,}(?:\\.[A-Za-z0-9_-]{5,}){2,4}\\b",
    )
    private val PROVIDER_TOKENS = listOf(
        Regex("\\bsk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_-]{16,}\\b", RegexOption.IGNORE_CASE),
        Regex("\\bgithub_pat_[A-Za-z0-9_]{20,}\\b", RegexOption.IGNORE_CASE),
        Regex("\\bgh[pousr]_[A-Za-z0-9_]{20,}\\b", RegexOption.IGNORE_CASE),
        Regex("\\bglpat-[A-Za-z0-9_-]{20,}\\b", RegexOption.IGNORE_CASE),
        Regex("\\bxox[baprs]-[A-Za-z0-9-]{10,}\\b", RegexOption.IGNORE_CASE),
        Regex("\\bAIza[0-9A-Za-z_-]{20,}\\b"),
        Regex("\\bya29\\.[0-9A-Za-z_-]{10,}\\b", RegexOption.IGNORE_CASE),
        Regex("\\b(?:AKIA|ASIA)[A-Z0-9]{16}\\b"),
        Regex("\\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{12,}\\b", RegexOption.IGNORE_CASE),
        Regex("\\b(?:npm|hf)_[A-Za-z0-9]{20,}\\b", RegexOption.IGNORE_CASE),
        Regex("\\bdapi[a-f0-9]{32}\\b", RegexOption.IGNORE_CASE),
    )
    private val LABELED_SECRET = Regex(
        "(?i)(\\b(?:password|passwd|pwd|passcode|pin|otp|totp|hotp|one[-_ ]?time[-_ ]?password|" +
            "verification[-_ ]?code|sms[-_ ]?code|api[-_ ]?key|api[-_ ]?secret|secret|client[-_ ]?secret|" +
            "access[-_ ]?key|secret[-_ ]?access[-_ ]?key|access[-_ ]?token|auth(?:entication)?[-_ ]?token|" +
            "refresh[-_ ]?token|authorization|bearer[-_ ]?token|private[-_ ]?key|cvv|cvc|" +
            "card[-_ ]?number|security[-_ ]?code)\\b\\s*(?::|=|is\\b)|" +
            "(?:密码|口令|支付密码|验证码|动态码|短信码|支付码|银行卡号|信用卡号|安全码|私钥|密钥|令牌)" +
            "\\s*(?:[:：=]|是|为))\\s*(?:(?:Bearer|Basic)\\s+)?" +
            "(?:\"[^\"]*\"|'[^']*'|[^\\s,，;；]+)",
    )
    private val LABELED_SHORT_CODE = Regex(
        "(?i)((?:\\b(?:otp|totp|hotp|pin|passcode|code|verification[-_ ]?code|cvv|cvc|security[-_ ]?code)\\b|" +
            "验证码|动态码|短信码|安全码)\\s*(?:(?::|=)|是|为)?\\s*)\\d{3,8}",
    )
    private val STANDALONE_SHORT_CODE = Regex("^\\s*\\d{3,8}\\s*$")
    private val CARD_CANDIDATE = Regex("(?<!\\d)(?:\\d[ -]?){13,19}(?!\\d)")
}
