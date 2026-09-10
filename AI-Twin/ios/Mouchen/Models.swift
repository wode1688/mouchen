import Foundation

enum JSONValue: Codable, Hashable, Sendable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let decoded = try? value.decode(Bool.self) { self = .bool(decoded) }
        else if let decoded = try? value.decode(Double.self) { self = .number(decoded) }
        else if let decoded = try? value.decode(String.self) { self = .string(decoded) }
        else if let decoded = try? value.decode([String: JSONValue].self) { self = .object(decoded) }
        else if let decoded = try? value.decode([JSONValue].self) { self = .array(decoded) }
        else { throw DecodingError.dataCorruptedError(in: value, debugDescription: "Unsupported JSON value") }
    }

    func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case .string(let item): try value.encode(item)
        case .number(let item): try value.encode(item)
        case .bool(let item): try value.encode(item)
        case .object(let item): try value.encode(item)
        case .array(let item): try value.encode(item)
        case .null: try value.encodeNil()
        }
    }
}

struct ServerConfiguration: Codable, Equatable, Sendable {
    var baseURL: String
    var userID: String
    var username: String? = nil
    var bearerToken: String
    var proactiveCloudEnabled: Bool
    var uploadFullContext: Bool
    var locale: String? = nil

    var appLocale: AppLocale { AppLocale.normalized(locale) }

    func validated() throws -> ServerConfiguration {
        let normalized = baseURL.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: normalized), let host = url.host, !host.isEmpty,
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil else {
            throw MouchenError.configuration(L10n.text("服务器地址无效", "Invalid server address", locale: appLocale))
        }
        let loopback = ["127.0.0.1", "localhost", "::1"].contains(host.lowercased())
        guard url.scheme?.lowercased() == "https" || loopback else {
            throw MouchenError.configuration(L10n.text("真机只允许可信 HTTPS 服务器", "A physical device requires a trusted HTTPS server", locale: appLocale))
        }
        guard !userID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw MouchenError.configuration(L10n.text("用户 ID 不能为空", "User ID cannot be empty", locale: appLocale))
        }
        guard !bearerToken.isEmpty || loopback else {
            throw MouchenError.configuration(L10n.text("远程服务器必须填写访问令牌", "A remote server requires an access token", locale: appLocale))
        }
        var copy = self
        copy.baseURL = normalized
        copy.userID = userID.trimmingCharacters(in: .whitespacesAndNewlines)
        return copy
    }
}

struct AuthSession: Codable, Equatable, Sendable {
    let userID: String
    let username: String
    let deviceID: String
    let scopes: [String]
    let expiresAt: String?
    let locale: String?

    enum CodingKeys: String, CodingKey {
        case username, scopes, locale
        case userID = "user_id"
        case deviceID = "device_id"
        case expiresAt = "expires_at"
    }
}

struct AccountPreferences: Codable, Equatable, Sendable {
    let locale: String
    let updatedAt: String?

    enum CodingKeys: String, CodingKey {
        case locale
        case updatedAt = "updated_at"
    }
}

struct AccountPreferencesUpdate: Codable, Sendable {
    let locale: String
}

struct AuthResponse: Codable, Sendable {
    let accessToken: String
    let tokenType: String
    let expiresAt: String?
    let session: AuthSession

    enum CodingKeys: String, CodingKey {
        case session
        case accessToken = "access_token"
        case tokenType = "token_type"
        case expiresAt = "expires_at"
    }
}

struct HealthResponse: Codable, Sendable {
    let status: String
    let version: String
    let mode: String
}

struct Goal: Codable, Identifiable, Hashable, Sendable {
    let id: UUID
    let domain: String
    let title: String
    let quote: String
    let target: [String: JSONValue]
    let isRedline: Bool
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, domain, title, quote, target
        case isRedline = "is_redline"
        case createdAt = "created_at"
    }
}

struct GoalCreate: Codable, Sendable {
    let domain: String
    let title: String
    let quote: String
    let target: [String: JSONValue]
    let isRedline: Bool

    enum CodingKeys: String, CodingKey {
        case domain, title, quote, target
        case isRedline = "is_redline"
    }
}

enum AdviceLevel: Int, Codable, Comparable, Sendable {
    case l1 = 1, l2 = 2, l3 = 3, l4 = 4

    static func < (lhs: AdviceLevel, rhs: AdviceLevel) -> Bool { lhs.rawValue < rhs.rawValue }

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if let number = try? value.decode(Int.self), let level = AdviceLevel(rawValue: number) {
            self = level
            return
        }
        if let label = try? value.decode(String.self),
           let number = Int(label.lowercased().replacingOccurrences(of: "l", with: "")),
           let level = AdviceLevel(rawValue: number) {
            self = level
            return
        }
        throw DecodingError.dataCorruptedError(in: value, debugDescription: "Invalid advice level")
    }
}

struct Prediction: Codable, Hashable, Sendable {
    let outcome: String
    let deadline: String
    let confidence: Double
}

struct Advice: Codable, Identifiable, Hashable, Sendable {
    let id: UUID
    let domain: String
    let requestedLevel: AdviceLevel
    let effectiveLevel: AdviceLevel
    let goalQuote: String
    let action: String
    let firstStep: String
    let alternative: String?
    let prediction: Prediction
    let delivery: String
    let status: String
    let createdAt: String

    enum CodingKeys: String, CodingKey {
        case id, domain, action, alternative, prediction, delivery, status
        case requestedLevel = "requested_level"
        case effectiveLevel = "effective_level"
        case goalQuote = "goal_quote"
        case firstStep = "first_step"
        case createdAt = "created_at"
    }
}

struct AdviceAttentionClaimResponse: Codable, Sendable {
    let status: String
    let claimToken: UUID?
    let leaseExpiresAt: String?
    let deliveryNumber: Int?
    let advice: Advice?

    enum CodingKeys: String, CodingKey {
        case status, advice
        case claimToken = "claim_token"
        case leaseExpiresAt = "lease_expires_at"
        case deliveryNumber = "delivery_number"
    }

    var claimed: PendingAdviceClaim? {
        guard status == "claimed",
              let claimToken,
              let leaseExpiresAt,
              let deliveryNumber,
              let advice else { return nil }
        return PendingAdviceClaim(
            advice: advice,
            claimToken: claimToken,
            leaseExpiresAt: leaseExpiresAt,
            deliveryNumber: deliveryNumber,
            notificationScheduled: false
        )
    }
}

struct PendingAdviceClaim: Codable, Equatable, Sendable {
    let advice: Advice
    let claimToken: UUID
    let leaseExpiresAt: String
    let deliveryNumber: Int
    var notificationScheduled: Bool
}

struct EventPayload: Codable, Sendable {
    let source: String
    let type: String
    let occurredAt: String
    let facts: [String: JSONValue]
    let entities: [String]
    let confidence: Double
    let sensitivity: String
    let consentScope: String
    let evidenceRef: String

    enum CodingKeys: String, CodingKey {
        case source, type, facts, entities, confidence, sensitivity
        case occurredAt = "occurred_at"
        case consentScope = "consent_scope"
        case evidenceRef = "evidence_ref"
    }
}

struct ModelReply: Codable, Sendable {
    let provider: String
    let model: String
    let content: String
    let secondOpinion: String?
    let degraded: Bool

    enum CodingKeys: String, CodingKey {
        case provider, model, content, degraded
        case secondOpinion = "second_opinion"
    }
}

enum MouchenError: LocalizedError {
    case configuration(String)
    case transport(String)
    case server(Int, String)

    var errorDescription: String? {
        switch self {
        case .configuration(let value), .transport(let value): value
        case .server(let status, let value): "Server error \(status): \(value)"
        }
    }
}

enum ISO8601Text {
    static func now() -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: Date())
    }

    static func string(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }
}
