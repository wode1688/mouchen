package com.mouchen.app.sync

/**
 * Binds an account-locale request to the exact authenticated session that started it.
 * The token is deliberately excluded from [toString] so diagnostics cannot disclose it.
 */
internal class AccountLocaleRequestFence private constructor(
    private val generation: Long,
    private val session: AuthSession,
) {
    fun matches(currentGeneration: Long, currentSession: AuthSession?): Boolean =
        generation == currentGeneration &&
            currentSession != null &&
            accountLocaleSessionMatches(session, currentSession)

    override fun toString(): String =
        "AccountLocaleRequestFence(generation=$generation, userId=${session.userId}, accessToken=<redacted>)"

    companion object {
        fun capture(generation: Long, session: AuthSession): AccountLocaleRequestFence =
            AccountLocaleRequestFence(generation, session)
    }
}
