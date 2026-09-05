import Foundation
import Security

final class KeychainStore {
    private let service = "com.mouchen.ios.server-configuration"
    private let account = "owner"
    private let deviceService = "com.mouchen.ios.device-identity"
    private let deviceAccount = "installation"

    func load() -> ServerConfiguration? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return try? JSONDecoder().decode(ServerConfiguration.self, from: data)
    }

    func save(_ configuration: ServerConfiguration) throws {
        let data = try JSONEncoder().encode(configuration)
        let identity: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let update: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        let status = SecItemUpdate(identity as CFDictionary, update as CFDictionary)
        if status == errSecItemNotFound {
            var insertion = identity
            insertion.merge(update) { _, new in new }
            let addStatus = SecItemAdd(insertion as CFDictionary, nil)
            guard addStatus == errSecSuccess else { throw MouchenError.transport("无法安全保存服务器配置") }
        } else if status != errSecSuccess {
            throw MouchenError.transport("无法更新服务器配置")
        }
    }

    func delete() {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
    }

    /// This identifier is deliberately separate from the server configuration.
    /// Disconnecting an account must not make the same installation look like a
    /// new device and claim a second copy of an in-flight notification.
    func loadOrCreateDeviceID() throws -> String {
        if let existing = loadString(service: deviceService, account: deviceAccount), !existing.isEmpty {
            return existing
        }
        let value = "ios-\(UUID().uuidString.lowercased())"
        try saveString(value, service: deviceService, account: deviceAccount)
        return value
    }

    private func loadString(service: String, account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private func saveString(_ value: String, service: String, account: String) throws {
        let identity: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let update: [String: Any] = [
            kSecValueData as String: Data(value.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        let status = SecItemUpdate(identity as CFDictionary, update as CFDictionary)
        if status == errSecItemNotFound {
            var insertion = identity
            insertion.merge(update) { _, new in new }
            guard SecItemAdd(insertion as CFDictionary, nil) == errSecSuccess else {
                throw MouchenError.transport("Unable to store the installation identity securely")
            }
        } else if status != errSecSuccess {
            throw MouchenError.transport("Unable to update the installation identity")
        }
    }
}
