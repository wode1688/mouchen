import Foundation

/// A one-slot queue used by the locale PUT worker. Replacing the slot makes
/// rapid picker changes deterministic: only the owner's newest intent remains.
struct LatestLocaleSelection {
    private var pending: AppLocale?

    var isEmpty: Bool { pending == nil }

    mutating func replace(with locale: AppLocale) {
        pending = locale
    }

    mutating func take() -> AppLocale? {
        defer { pending = nil }
        return pending
    }

    mutating func removeAll() {
        pending = nil
    }
}

/// Orders account-locale reads against a serialized locale mutation.
///
/// A read captures the current generation. Starting a mutation advances that
/// generation, which retires every older read. Reads are not admitted while a
/// mutation is active, so a GET cannot observe the server before the PUT has
/// committed. Session identity is still enforced separately by AppModel's
/// epoch/baseURL/userID snapshot.
struct LocalePreferenceFence {
    struct ReadToken: Equatable {
        fileprivate let generation: UInt64
    }

    struct MutationToken: Equatable {
        fileprivate let generation: UInt64
    }

    private var generation: UInt64 = 0
    private var activeMutationGeneration: UInt64?

    mutating func beginRead() -> ReadToken? {
        guard activeMutationGeneration == nil else { return nil }
        return ReadToken(generation: generation)
    }

    mutating func beginMutation() -> MutationToken {
        generation &+= 1
        activeMutationGeneration = generation
        return MutationToken(generation: generation)
    }

    func permits(_ token: ReadToken) -> Bool {
        activeMutationGeneration == nil && token.generation == generation
    }

    func permits(_ token: MutationToken) -> Bool {
        activeMutationGeneration == token.generation && token.generation == generation
    }

    mutating func finishMutation(_ token: MutationToken) {
        guard permits(token) else { return }
        activeMutationGeneration = nil
    }

    mutating func invalidate() {
        generation &+= 1
        activeMutationGeneration = nil
    }
}
