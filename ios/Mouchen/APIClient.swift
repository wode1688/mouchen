import Foundation

final class OriginBoundSessionDelegate: NSObject, URLSessionTaskDelegate {
    private let origin: URL

    init(origin: URL) {
        self.origin = origin
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        guard let target = request.url,
              Self.sameOrigin(origin, target),
              !Self.isHTTPSDowngrade(origin, target) else {
            completionHandler(nil)
            return
        }
        completionHandler(request)
    }

    static func sameOrigin(_ lhs: URL, _ rhs: URL) -> Bool {
        lhs.scheme?.lowercased() == rhs.scheme?.lowercased()
            && lhs.host?.lowercased() == rhs.host?.lowercased()
            && effectivePort(lhs) == effectivePort(rhs)
    }

    static func isHTTPSDowngrade(_ source: URL, _ target: URL) -> Bool {
        source.scheme?.lowercased() == "https" && target.scheme?.lowercased() != "https"
    }

    private static func effectivePort(_ url: URL) -> Int? {
        if let port = url.port { return port }
        switch url.scheme?.lowercased() {
        case "https": return 443
        case "http": return 80
        default: return nil
        }
    }
}

enum OriginBoundSession {
    static func make(origin: URL, timeout: TimeInterval) -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = timeout
        configuration.timeoutIntervalForResource = timeout
        configuration.httpShouldSetCookies = false
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(
            configuration: configuration,
            delegate: OriginBoundSessionDelegate(origin: origin),
            delegateQueue: nil
        )
    }
}

actor APIClient {
    private let configuration: ServerConfiguration
    private let session: URLSession
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()

    init(configuration: ServerConfiguration, session: URLSession? = nil) {
        self.configuration = configuration
        let origin = URL(string: configuration.baseURL)!
        self.session = session ?? OriginBoundSession.make(origin: origin, timeout: 75)
    }

    func health() async throws -> HealthResponse {
        try await request(path: "/health", method: "GET", body: Optional<String>.none)
    }

    func sessionInfo() async throws -> AuthSession {
        try await request(path: "/v1/session", method: "GET", body: Optional<String>.none)
    }

    func accountPreferences() async throws -> AccountPreferences {
        try await request(path: "/v1/account/preferences", method: "GET", body: Optional<String>.none)
    }

    func updateAccountLocale(_ locale: AppLocale) async throws -> AccountPreferences {
        try await request(
            path: "/v1/account/preferences",
            method: "PUT",
            body: AccountPreferencesUpdate(locale: locale.rawValue)
        )
    }

    func logout() async throws {
        let _: EmptyResponse = try await request(
            path: "/v1/auth/logout",
            method: "POST",
            body: EmptyBody(),
            tolerateAnyObject: true
        )
    }

    func goals() async throws -> [Goal] {
        try await request(path: "/v1/goals", method: "GET", body: Optional<String>.none)
    }

    func createGoal(_ goal: GoalCreate) async throws -> Goal {
        try await request(path: "/v1/goals", method: "POST", body: goal)
    }

    func advice(limit: Int = 200) async throws -> [Advice] {
        try await request(path: "/v1/advice?limit=\(min(500, max(1, limit)))", method: "GET", body: Optional<String>.none)
    }

    func claimAdviceAttention(deviceID: String, appVersion: String?) async throws -> AdviceAttentionClaimResponse {
        try await request(
            path: "/v1/advice-attention/claim",
            method: "POST",
            body: AdviceAttentionClaimBody(deviceID: deviceID, platform: "ios", appVersion: appVersion)
        )
    }

    func completeAdviceAttention(adviceID: UUID, deviceID: String, claimToken: UUID) async throws {
        let _: EmptyResponse = try await request(
            path: "/v1/advice-attention/\(adviceID.uuidString)/complete",
            method: "POST",
            body: AdviceAttentionCompleteBody(deviceID: deviceID, claimToken: claimToken),
            tolerateAnyObject: true
        )
    }

    func failAdviceAttention(adviceID: UUID, deviceID: String, claimToken: UUID, reason: String) async throws {
        let _: EmptyResponse = try await request(
            path: "/v1/advice-attention/\(adviceID.uuidString)/fail",
            method: "POST",
            body: AdviceAttentionFailBody(deviceID: deviceID, claimToken: claimToken, reason: String(reason.prefix(240))),
            tolerateAnyObject: true
        )
    }

    func send(event: EventPayload) async throws {
        let cloud = configuration.proactiveCloudEnabled ? "true" : "false"
        let _: EmptyResponse = try await request(
            path: "/v1/events",
            method: "POST",
            body: event,
            extraHeaders: ["X-Proactive-Cloud-Approved": cloud],
            tolerateAnyObject: true
        )
    }

    func runReview() async throws {
        let cloud = configuration.proactiveCloudEnabled ? "true" : "false"
        let _: EmptyResponse = try await request(
            path: "/v1/reviews/run",
            method: "POST",
            body: EmptyBody(),
            extraHeaders: ["X-Proactive-Cloud-Approved": cloud],
            tolerateAnyObject: true
        )
    }

    func feedback(adviceID: UUID, kind: String, note: String? = nil) async throws {
        let _: EmptyResponse = try await request(
            path: "/v1/advice/\(adviceID.uuidString)/feedback",
            method: "POST",
            body: FeedbackBody(kind: kind, note: note),
            tolerateAnyObject: true
        )
    }

    func outcome(adviceID: UUID, status: String, actualResult: String) async throws {
        let _: EmptyResponse = try await request(
            path: "/v1/advice/\(adviceID.uuidString)/outcome",
            method: "POST",
            body: OutcomeBody(status: status, actualResult: actualResult, utility: nil, timingQuality: nil),
            tolerateAnyObject: true
        )
    }

    func ask(_ prompt: String) async throws -> ModelReply {
        let body = ModelAnalyzeBody(
            level: "L2",
            purpose: "user_question",
            prompt: String(prompt.prefix(8_000)),
            redactedContext: ["surface": .string("ios")],
            outboundApproved: configuration.proactiveCloudEnabled,
            forcePrivate7B: false
        )
        return try await request(path: "/v1/model/analyze", method: "POST", body: body)
    }

    private func request<Response: Decodable, Body: Encodable>(
        path: String,
        method: String,
        body: Body?,
        extraHeaders: [String: String] = [:],
        tolerateAnyObject: Bool = false
    ) async throws -> Response {
        guard let url = URL(string: configuration.baseURL + path) else {
            throw MouchenError.configuration(L10n.text("服务器地址无效", "Invalid server address", locale: configuration.appLocale))
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 75
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if !configuration.bearerToken.isEmpty {
            request.setValue("Bearer \(configuration.bearerToken)", forHTTPHeaderField: "Authorization")
        }
        extraHeaders.forEach { request.setValue($0.value, forHTTPHeaderField: $0.key) }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try encoder.encode(body)
        }

        let data: Data
        let response: URLResponse
        do { (data, response) = try await session.data(for: request) }
        catch { throw MouchenError.transport(error.localizedDescription) }
        guard let http = response as? HTTPURLResponse else {
            throw MouchenError.transport("服务器没有返回 HTTP 响应")
        }
        guard let responseURL = http.url,
              let origin = URL(string: configuration.baseURL),
              OriginBoundSessionDelegate.sameOrigin(origin, responseURL),
              !OriginBoundSessionDelegate.isHTTPSDowngrade(origin, responseURL) else {
            throw MouchenError.transport("Server redirected outside the configured origin")
        }
        guard (200..<300).contains(http.statusCode) else {
            let message = (try? JSONDecoder().decode(ServerError.self, from: data).detail)
                ?? String(data: data, encoding: .utf8)
                ?? "请求失败"
            throw MouchenError.server(http.statusCode, String(message.prefix(500)))
        }
        if tolerateAnyObject, Response.self == EmptyResponse.self {
            return EmptyResponse() as! Response
        }
        do { return try decoder.decode(Response.self, from: data) }
        catch { throw MouchenError.transport("服务器数据格式不兼容：\(error.localizedDescription)") }
    }
}

actor AuthClient {
    private let baseURL: String
    private let locale: AppLocale
    private let session: URLSession
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()

    init(baseURL: String, locale: AppLocale = .systemPreferred, session: URLSession? = nil) throws {
        let normalized = baseURL
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let url = URL(string: normalized), let host = url.host, !host.isEmpty,
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil else {
            throw MouchenError.configuration(L10n.text("服务器地址无效", "Invalid server address", locale: locale))
        }
        let loopback = ["127.0.0.1", "localhost", "::1"].contains(host.lowercased())
        guard url.scheme?.lowercased() == "https" || loopback else {
            throw MouchenError.configuration(L10n.text("真机只允许可信 HTTPS 服务器", "A physical device requires a trusted HTTPS server", locale: locale))
        }
        self.baseURL = normalized
        self.locale = locale
        self.session = session ?? OriginBoundSession.make(origin: url, timeout: 30)
    }

    func login(
        username: String,
        password: String,
        deviceID: String,
        deviceName: String?
    ) async throws -> AuthResponse {
        try await authenticate(
            path: "/v1/auth/login",
            body: AuthRequest(
                username: username,
                password: password,
                deviceID: deviceID,
                deviceName: deviceName,
                registrationCode: nil,
                locale: nil
            )
        )
    }

    func register(
        username: String,
        password: String,
        deviceID: String,
        deviceName: String?,
        registrationCode: String?
    ) async throws -> AuthResponse {
        try await authenticate(
            path: "/v1/auth/register",
            body: AuthRequest(
                username: username,
                password: password,
                deviceID: deviceID,
                deviceName: deviceName,
                registrationCode: registrationCode,
                locale: locale.rawValue
            )
        )
    }

    private func authenticate(path: String, body: AuthRequest) async throws -> AuthResponse {
        guard let url = URL(string: baseURL + path) else {
            throw MouchenError.configuration(L10n.text("服务器地址无效", "Invalid server address", locale: locale))
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try encoder.encode(body)

        let data: Data
        let response: URLResponse
        do { (data, response) = try await session.data(for: request) }
        catch { throw MouchenError.transport(error.localizedDescription) }
        guard let http = response as? HTTPURLResponse else {
            throw MouchenError.transport("服务器没有返回 HTTP 响应")
        }
        guard let responseURL = http.url,
              let origin = URL(string: baseURL),
              OriginBoundSessionDelegate.sameOrigin(origin, responseURL),
              !OriginBoundSessionDelegate.isHTTPSDowngrade(origin, responseURL) else {
            throw MouchenError.transport("Login redirected outside the configured origin")
        }
        guard (200..<300).contains(http.statusCode) else {
            let message = (try? decoder.decode(ServerError.self, from: data).detail)
                ?? "登录失败"
            throw MouchenError.server(http.statusCode, String(message.prefix(240)))
        }
        do { return try decoder.decode(AuthResponse.self, from: data) }
        catch { throw MouchenError.transport("登录响应格式不兼容") }
    }
}
private struct EmptyBody: Codable {}
private struct EmptyResponse: Codable { init() {} }
private struct ServerError: Codable { let detail: String }
private struct AuthRequest: Codable {
    let username: String
    let password: String
    let deviceID: String
    let deviceName: String?
    let registrationCode: String?
    let locale: String?

    enum CodingKeys: String, CodingKey {
        case username, password, locale
        case deviceID = "device_id"
        case deviceName = "device_name"
        case registrationCode = "registration_code"
    }
}
private struct AdviceAttentionClaimBody: Codable {
    let deviceID: String
    let platform: String
    let appVersion: String?
    enum CodingKeys: String, CodingKey {
        case platform
        case deviceID = "device_id"
        case appVersion = "app_version"
    }
}
private struct AdviceAttentionCompleteBody: Codable {
    let deviceID: String
    let claimToken: UUID
    enum CodingKeys: String, CodingKey {
        case deviceID = "device_id"
        case claimToken = "claim_token"
    }
}
private struct AdviceAttentionFailBody: Codable {
    let deviceID: String
    let claimToken: UUID
    let reason: String
    enum CodingKeys: String, CodingKey {
        case reason
        case deviceID = "device_id"
        case claimToken = "claim_token"
    }
}
private struct FeedbackBody: Codable { let kind: String; let note: String? }
private struct OutcomeBody: Codable {
    let status: String
    let actualResult: String
    let utility: Double?
    let timingQuality: Double?
    enum CodingKeys: String, CodingKey {
        case status, utility
        case actualResult = "actual_result"
        case timingQuality = "timing_quality"
    }
}
private struct ModelAnalyzeBody: Codable {
    let level: String
    let purpose: String
    let prompt: String
    let redactedContext: [String: JSONValue]
    let outboundApproved: Bool
    let forcePrivate7B: Bool
    enum CodingKeys: String, CodingKey {
        case level, purpose, prompt
        case redactedContext = "redacted_context"
        case outboundApproved = "outbound_approved"
        case forcePrivate7B = "force_private_7b"
    }
}
