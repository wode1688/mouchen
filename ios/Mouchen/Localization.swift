import Foundation

enum AppLocale: String, Codable, CaseIterable, Identifiable, Sendable {
    case zhCN = "zh-CN"
    case enUS = "en-US"

    var id: String { rawValue }
    var isEnglish: Bool { self == .enUS }

    var displayName: String {
        switch self {
        case .zhCN: "中文"
        case .enUS: "English"
        }
    }

    static var systemPreferred: AppLocale {
        let preferred = Locale.preferredLanguages.first?.lowercased() ?? ""
        return preferred.hasPrefix("zh") ? .zhCN : .enUS
    }

    static func normalized(_ value: String?) -> AppLocale {
        guard let value else { return .systemPreferred }
        let normalized = value.trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: "_", with: "-")
            .lowercased()
        if normalized == "en" || normalized.hasPrefix("en-") { return .enUS }
        if normalized == "zh" || normalized.hasPrefix("zh-") { return .zhCN }
        return .systemPreferred
    }
}

enum L10n {
    static func text(_ zh: String, _ en: String, locale: AppLocale) -> String {
        locale.isEnglish ? en : zh
    }

    static func error(_ error: Error, locale: AppLocale) -> String {
        if let mouchenError = error as? MouchenError {
            switch mouchenError {
            case .configuration(let detail):
                return text("配置错误：\(detail)", "Configuration error: \(detail)", locale: locale)
            case .transport:
                return text("网络连接失败，请检查服务器和网络后重试。", "Connection failed. Check the server and your network, then try again.", locale: locale)
            case .server(let status, _):
                return text("服务器请求失败（HTTP \(status)）。", "Server request failed (HTTP \(status)).", locale: locale)
            }
        }
        if error is AdviceNotificationError {
            return text("尚未开启通知权限。", "Notification permission is not enabled.", locale: locale)
        }
        return text("操作失败，请重试。", "The operation failed. Please try again.", locale: locale)
    }
}
